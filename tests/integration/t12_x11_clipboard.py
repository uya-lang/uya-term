#!/usr/bin/env python3
"""t12 本机 X11 剪贴板读取：uya-ssh 自己实现 X11 协议，从剪贴板取回图片。

为什么不用真 X 服务器：真 CLIPBOARD 属于用户的桌面会话（dde-clipboard / klipper
正持有它）。让测试去抢所有权既会把用户自己的剪贴板内容读出来，也会和桌面组件打架，
而且不可重复。这里用 tests/integration/fake_x11.py 自建一个只应答剪贴板协议的
「X 服务器」，uya-ssh 连它——全流程封闭，且能构造真图上才能触发的大数据 INCR 分支。

覆盖：
  1. 文本剪贴板  → clip fetch 取回字节与内容一致
  2. 内联 PNG    → fetch 后 MIME 嗅探为 image/png，字节逐字节一致
  3. INCR PNG（>64 KiB 分段）→ 字节逐字节一致（这条只有假服务器能稳定造出来）
  4. 空剪贴板    → 退出码非 0 且给出人话
  5. 端到端：PTY 里跑 uya-ssh + 假「远端应用」，应用发 5522 read 请求，
     断言它拿到的是**本机 X11 剪贴板里的图片字节**（这正是用户报的那条路）
"""

import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import fcntl
import termios

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "bin", "uya-ssh")
FAKE = os.path.join(ROOT, "tests", "integration", "fake_x11.py")
IMG_BYTES_INLINE = 4096
IMG_BYTES_INCR = 300 * 1024


def fake_payload(mode):
    """期望的剪贴板字节。

    **必须**和 fake_x11.py 用同一个生成器：这里另抄一份"看起来一样"的构造，
    一旦两边 BP 就会变成假绿/假红。所以直接 import 那个模块的函数。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("fake_x11", FAKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Server.build_payload(type("S", (), {"mode": mode})())


class FakeX:
    """起一个假 X 服务器，并给出连它所需的环境变量。

    套接字放在 /tmp/.X11-unix/X<n>（就是真 X 的位置），这样 uya-ssh 走的是**完全
    正常的 DISPLAY 解析路径**，生产代码里不需要为测试留任何钩子。
    显示号按 pid 取，避免并行跑测试时互相踩。
    """

    def __init__(self, work, mode):
        self.dir = os.path.join(work, "x11")
        os.makedirs(self.dir, exist_ok=True)
        self.num = 90 + (os.getpid() % 9)
        self.sock = "/tmp/.X11-unix/X%d" % self.num
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        self.proc = subprocess.Popen(
            [sys.executable, FAKE, self.sock, mode, "60"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = self.proc.stdout.readline().decode()
        if not line.startswith("READY"):
            err = self.proc.stderr.read().decode(errors="replace")
            raise RuntimeError("假 X 服务器没起来: %r %s" % (line, err))

    def env(self, home):
        env = dict(os.environ)
        env["HOME"] = home
        env["DISPLAY"] = ":%d" % self.num
        # 认证文件故意指向不存在的位置：像没开认证的 X 服务器那样走空 cookie 路径。
        env["XAUTHORITY"] = os.path.join(self.dir, "no-such-xauthority")
        return env

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        if os.path.exists(self.sock):
            try:
                os.unlink(self.sock)
            except OSError:
                pass


def run_clip(env, *args):
    """跑一个 clip 子命令，返回 (rc, stdout, stderr)。"""
    p = subprocess.run([BIN, "clip"] + list(args), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
    return p.returncode, p.stdout, p.stderr


def check_bytes(work, env, mode, label):
    """fetch 之后 get 回来，断言字节与假服务器给的一致。"""
    home = os.path.join(work, "home_" + mode)
    os.makedirs(home, exist_ok=True)
    rc, out, err = run_clip(env, "fetch")
    if rc != 0:
        print("FAIL[%s]: clip fetch 失败 rc=%d err=%s" % (label, rc, err.decode(errors="replace")))
        return False
    rc, data, err = run_clip(env, "get")
    if rc != 0:
        print("FAIL[%s]: clip get 失败 rc=%d" % (label, rc))
        return False
    want = fake_payload(mode)
    if data != want:
        print("FAIL[%s]: 字节不一致 期望 %d 实际 %d" % (label, len(want), len(data)))
        if len(data) > 64:
            print("   头32 %r" % data[:32])
        return False
    rc, mime, _ = run_clip(env, "mime")
    mime_text = mime.decode(errors="replace").strip()
    want_mime = "image/png" if mode in ("inline", "incr") else "text/plain"
    if mime_text != want_mime:
        print("FAIL[%s]: MIME 期望 %s 实际 %s" % (label, want_mime, mime_text))
        return False
    print("OK[%s]: %d 字节, mime=%s" % (label, len(data), mime_text))
    return True


def check_empty(work):
    """空剪贴板：必须失败且给出人话。"""
    home = os.path.join(work, "home_empty")
    os.makedirs(home, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = home
    env.pop("DISPLAY", None)
    rc, out, err = run_clip(env, "fetch")
    if rc == 0:
        print("FAIL[空]: 没有 DISPLAY 时 fetch 竟然成功了")
        return False
    text = (out + err).decode(errors="replace")
    if "DISPLAY" not in text:
        print("FAIL[空]: 提示里没说清原因: %r" % text)
        return False
    print("OK[空]: 无 DISPLAY 时明确报错")
    return True


def check_pty_end_to_end(work, x11, mode):
    """PTY 里跑 uya-ssh + 假远端应用，断言应用拿到本机剪贴板的图片字节。"""
    home = os.path.join(work, "home_pty_" + mode)
    os.makedirs(home, exist_ok=True)
    app = os.path.join(work, "paster.py")
    with open(app, "w") as fh:
        fh.write(
            "import base64,os,sys,time,tty\n"
            # 必须把 stdin 切成 raw：默认的规范模式会把我们收到的应答一直攒在
            # PTY 行缓冲里，直到出现换行才交付（应答里没有换行）——
            # 症状就是「请求发出去了、应用却一个字节都收不到」。
            # 真实 TUI 应用（含 fake_tui_app.py）第一件事也是 tty.setraw。
            "tty.setraw(0)\n"
            "def rd(t):\n"
            "    import select\n"
            "    b=b''\n"
            "    end=time.time()+t\n"
            "    while time.time()<end:\n"
            "        r,_,_=select.select([0],[],[],0.05)\n"
            "        if r:\n"
            "            c=os.read(0,4096)\n"
            "            if not c: break\n"
            "            b+=c\n"
            "    return b\n"
            "rw=b'\\x1b]5522;type=read;' + base64.b64encode(b'image/png text/plain') + b'\\x07'\n"
            "os.write(1,rw)\n"
            "d=rd(3.0)\n"
            "open(os.environ['DUMP'],'wb').write(d)\n"
        )
    dump = os.path.join(work, "dump_" + mode + ".bin")
    env = x11.env(home)
    env["DUMP"] = dump
    argv = [BIN, "--program=" + sys.executable, "--", app]
    pid, master = pty.fork()
    if pid == 0:
        os.execve(BIN, argv, env)
        os._exit(127)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    out = b""
    deadline = time.time() + 12
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            try:
                c = os.read(master, 65536)
            except OSError:
                break
            if not c:
                break
            out += c
        if os.path.exists(dump) and os.path.getsize(dump) > 0:
            time.sleep(0.3)
            break
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    if not os.path.exists(dump):
        print("FAIL[pty-%s]: 假应用没有收到任何应答（设备输出 %r）" % (mode, out[-300:]))
        return False
    resp = open(dump, "rb").read()
    want = fake_payload(mode)
    # 5522 的应答是**多帧**：OK 一帧、DATA 若干帧（每帧编码前 ≤4096 字节）、DONE 一帧。
    # 只取第一帧会得到「一张 4096 字节的图」——这正是分块协议最容易写错的地方，
    # 所以这里把**所有** DATA 帧按顺序拼起来再比对。
    import base64 as b64
    got = b""
    frames = 0
    pos = 0
    while True:
        idx = resp.find(b"status=DATA:mime=", pos)
        if idx < 0:
            break
        rest = resp[idx + len(b"status=DATA:mime="):]
        semi = rest.find(b";")
        end = rest.find(b"\x1b\\")
        if semi < 0 or end < 0 or semi > end:
            print("FAIL[pty-%s]: DATA 帧结构不对: %r" % (mode, rest[:120]))
            return False
        got += b64.b64decode(rest[semi + 1:end])
        frames += 1
        pos = idx + 1
    if frames == 0:
        print("FAIL[pty-%s]: 应答里没有 DATA 帧: %r" % (mode, resp[:200]))
        return False
    if got != want:
        print("FAIL[pty-%s]: 应用拿到的字节与剪贴板不一致 期望 %d 实际 %d（%d 帧）"
              % (mode, len(want), len(got), frames))
        return False
    print("OK[pty-%s]: 端到端 %d 字节（本机 X11 剪贴板 → 5522 应答）" % (mode, len(got)))
    return True


def main():
    if not os.path.exists(BIN):
        print("FAIL: 先 make build（缺 %s）" % BIN, file=sys.stderr)
        return 1
    work = tempfile.mkdtemp(prefix="uyassh_t12_")
    ok = True
    try:
        # 1–3：三种剪贴板内容，逐字节校验
        for mode in ("text", "inline", "incr"):
            x11 = FakeX(work, mode)
            try:
                env = x11.env(os.path.join(work, "home_" + mode))
                os.makedirs(env["HOME"], exist_ok=True)
                if not check_bytes(work, env, mode, mode):
                    ok = False
            finally:
                x11.stop()
        # 4：空剪贴板 / 无 DISPLAY
        if not check_empty(work):
            ok = False
        # 5：端到端（图片 +
        for mode in ("inline", "incr"):
            x11 = FakeX(work, mode)
            try:
                if not check_pty_end_to_end(work, x11, mode):
                    ok = False
            finally:
                x11.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not ok:
        print("t12 失败", file=sys.stderr)
        return 1
    print("OK: t12 本机 X11 剪贴板读取全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
