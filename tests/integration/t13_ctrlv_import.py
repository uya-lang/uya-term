#!/usr/bin/env python3
"""t13：Ctrl-V 先入库、再把按键交给 ssh（推前置）。

被测语义（这是本轮的核心）：用户按 Ctrl-V 时，uya-ssh 先把它**自己**能拿到的
本机剪贴板图片收进仓库（成为当前项），然后才把 Ctrl-V 字节原样转发给子进程。
远端应用随后自己发起的 5522 read 就能立刻从仓库里拿到那张图。

为什么顺序值得专门测：光断言"最后入过库"是证明不了顺序的 —— 拉模式（等应用问了
才去读 X11）同样能让"最终有图"成立，而那正是要避免的行为（读取落在应用的读超时
窗口里）。所以 t13 让假 X 服务器与假应用各自打时间戳，断言
**X11 交付时刻 < 子进程收到 Ctrl-V 的时刻**；这条断言在拉模式下必然失败。

覆盖：
  1. 顺序 + 入库：Ctrl-V → 仓库出现该图 + 子进程恰好收到一个 0x16，且交付在前
  2. 应用随后读得到：read 拿到的字节与假 X11 逐字节一致，且**没有第二次 X11 取数**
     （证明它读的是仓库，不是又去问了 X11）
  3. 不吞键：没有 DISPLAY 时连按两次 Ctrl-V，子进程收到 1616
  4. 前缀优先：--prefix=^v 时 ^v 仍是前缀键（^v q 结束会话，子进程收不到字节）
  5. 关得掉：--ctrl-v-import=no / 配置 ctrl-v-import = no 时不入库，但按键照转发
  6. 去重：连按两次同一个图只产生一条条目，而子进程收到两个 0x16
"""

import base64
import importlib.util
import os
import pty
import select
import shutil
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
ECHO_HEX = os.path.join(ROOT, "tests", "integration", "echo_hex.py")
CTRL_V = b"\x16"
PREFIX = b"\x1d"


def fake_payload(mode):
    """期望的剪贴板字节（与假服务器共用同一个生成器，避免两边各抄一份）。"""
    spec = importlib.util.spec_from_file_location("fake_x11", FAKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Server.build_payload(type("S", (), {"mode": mode})())


class FakeX:
    """起一个假 X 服务器，并给出连它所需的环境变量与事件轨迹文件。"""

    def __init__(self, work, mode, tag):
        self.dir = os.path.join(work, "x11_" + tag)
        os.makedirs(self.dir, exist_ok=True)
        # 显示号按 pid 偏移，避免和并行跑的其它测试（或真 X）撞号。
        self.num = 90 + (os.getpid() % 9)
        self.sock = "/tmp/.X11-unix/X%d" % self.num
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        self.trace = os.path.join(self.dir, "trace.log")
        env = dict(os.environ)
        env["FAKE_X11_TRACE"] = self.trace
        self.proc = subprocess.Popen(
            [sys.executable, FAKE, self.sock, mode, "60"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        line = self.proc.stdout.readline().decode()
        if not line.startswith("READY"):
            err = self.proc.stderr.read().decode(errors="replace")
            raise RuntimeError("假 X 服务器没起来: %r %s" % (line, err))

    def env(self, home):
        env = base_env(home)
        env["DISPLAY"] = ":%d" % self.num
        # 指向不存在的认证文件：像没开认证的 X 服务器那样走空 cookie 路径。
        env["XAUTHORITY"] = os.path.join(self.dir, "no-such-xauthority")
        env["FAKE_X11_TRACE"] = self.trace
        return env

    def deliveries(self):
        """读到过几次「数据交付完毕」。"""
        try:
            with open(self.trace) as fh:
                return [ln.split()[0] for ln in fh if ln.strip()]
        except OSError:
            return []

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


def read_trace_stamps(path):
    """把轨迹文件读成 {事件: 首次时间戳}。"""
    out = {}
    try:
        with open(path) as fh:
            for ln in fh:
                parts = ln.split()
                if len(parts) == 2 and parts[0] not in out:
                    out[parts[0]] = float(parts[1])
    except OSError:
        pass
    return out


def run_clip(env, *args):
    p = subprocess.run([BIN, "clip"] + list(args), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    return p.returncode, p.stdout, p.stderr


def clip_count(env):
    """仓库里当前有几个条目（读 clip list 的行数）。"""
    rc, out, _ = run_clip(env, "list")
    if rc != 0:
        return -1
    text = out.decode(errors="replace")
    if "为空" in text:
        return 0
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return max(0, len(lines) - 1)          # 减去表头


def base_env(home):
    """干净的环境：清掉宿主的 DISPLAY/XAUTHORITY/XDG。

    为什么要清 XDG_CONFIG_HOME：它优先于 HOME，宿主设了它就等于**所有**
    "-- 用 HOME 里的配置" 的用例都会去读宿主真实的 ~/.config/uyassh/config；
    于是"配置里写了 no"这种断言会假绿或假红（本机 XDG_CONFIG_HOME 确实设了）。
    """
    env = dict(os.environ)
    env["HOME"] = home
    env.pop("DISPLAY", None)
    env.pop("XAUTHORITY", None)
    env.pop("XDG_CONFIG_HOME", None)
    return env


def write_app(path, body):
    with open(path, "w") as fh:
        fh.write(body)


def run_pty(env, argv, feed, settle=1.2, total=12.0):
    """在 PTY 里跑一条命令，按脚本喂输入，收集全部输出。"""
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    out = b""
    end = time.time() + total
    step = 0
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if step < len(feed) and time.time() > feed[step][0]:
            try:
                os.write(master, feed[step][1])
            except OSError:
                pass
            step += 1
    time.sleep(settle)
    try:
        os.close(master)
    except OSError:
        pass
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 1 + 2：顺序与读回

def check_order_and_read(work):
    """核心：Ctrl-V 先入库再转发；应用随后从仓库读到同一个图。"""
    home = os.path.join(work, "home_order")
    os.makedirs(home, exist_ok=True)
    x11 = FakeX(work, "inline", "order")
    try:
        env = x11.env(home)
        app = os.path.join(work, "app_order.py")
        # 假应用：收到 Ctrl-V 的那一刻打时间戳（证明按键是什么时候到的），
        # 然后发一次真正的 5522 read（image/png），把读到的字节写进 dump 文件。
        write_app(app, (
            "import base64,os,sys,time,tty\n"
            "tty.setraw(0)\n"
            "def rd(t):\n"
            "    import select\n"
            "    b=b''\n"
            "    end=time.time()+t\n"
            "    while time.time()<end:\n"
            "        r,_,_=select.select([0],[],[],0.05)\n"
            "        if r:\n"
            "            c=os.read(0,65536)\n"
            "            if not c: break\n"
            "            b+=c\n"
            "    return b\n"
            "trace=os.environ['TRACE']\n"
            "buf=b''\n"
            "while True:\n"
            "    c=os.read(0,1)\n"
            "    if not c: break\n"
            "    buf+=c\n"
            "    if c==b'\\x16':\n"
            "        open(trace,'a').write('app_saw_ctrl_v %.6f\\n'%time.time())\n"
            "        break\n"
            "want=base64.b64encode(b'image/png')\n"
            "os.write(1, b'\\x1b]5522;type=read;'+want+b'\\x07')\n"
            "d=rd(4.0)\n"
            "open(os.environ['DUMP'],'wb').write(d)\n"
        ))
        trace = os.path.join(work, "order_trace.log")
        dump = os.path.join(work, "order_dump.bin")
        env["TRACE"] = trace
        env["DUMP"] = dump
        argv = [BIN, "--program=" + sys.executable, "--", app]
        out = run_pty(env, argv, [(1.5, CTRL_V)], settle=1.0)

        ok = True
        # (a) 子进程收到了那个按键
        if not os.path.exists(trace):
            print("FAIL[顺序]: 子进程根本没收到 Ctrl-V（设备输出 %r）" % out[-200:])
            return False
        # (b) 仓库里出现了图片条目
        rc, mime, _ = run_clip(env, "mime")
        got_mime = mime.decode(errors="replace").strip()
        if rc != 0 or got_mime != "image/png":
            print("FAIL[顺序]: Ctrl-V 之后仓库的当前项不是 image/png（rc=%d mime=%r）"
                  % (rc, got_mime))
            ok = False
        # (c) 顺序：X11 交付必须**早于**子进程看到按键
        stamps = read_trace_stamps(x11.trace)
        app_stamps = read_trace_stamps(trace)
        if "delivered" not in stamps:
            print("FAIL[顺序]: 假 X 服务器没有交付过数据（说明压根没去读）")
            ok = False
        elif "app_saw_ctrl_v" not in app_stamps:
            print("FAIL[顺序]: 假应用没记录到收到按键的时刻")
            ok = False
        elif not (stamps["delivered"] < app_stamps["app_saw_ctrl_v"]):
            print("FAIL[顺序]: X11 交付(%.6f) 不早于子进程收到 Ctrl-V(%.6f) —— "
                  "这正是拉模式的行为（先转发、等应用问了才去读）"
                  % (stamps["delivered"], app_stamps["app_saw_ctrl_v"]))
            ok = False
        else:
            print("OK[顺序]: X11 交付 %.6fs 早于按键到达子进程 %.6fs（差 %.1f ms）"
                  % (stamps["delivered"], app_stamps["app_saw_ctrl_v"],
                     (app_stamps["app_saw_ctrl_v"] - stamps["delivered"]) * 1000))
        # (d) 应用随后读到的字节与假 X11 逐字节一致
        if os.path.exists(dump):
            resp = open(dump, "rb").read()
            got = b""
            pos = 0
            while True:
                idx = resp.find(b"status=DATA:mime=", pos)
                if idx < 0:
                    break
                rest = resp[idx + len(b"status=DATA:mime="):]
                semi = rest.find(b";")
                stop = rest.find(b"\x1b\\")
                if semi < 0 or stop < 0 or semi > stop:
                    break
                got += base64.b64decode(rest[semi + 1:stop])
                pos = idx + 1
            want = fake_payload("inline")
            if got != want:
                print("FAIL[读回]: 应用读到的字节与剪贴板不一致 期望 %d 实际 %d"
                      % (len(want), len(got)))
                ok = False
            else:
                print("OK[读回]: 应用经 5522 读回 %d 字节，与假 X11 剪贴板一致" % len(got))
        else:
            print("FAIL[读回]: 应用没有发出 read 或没收到应答")
            ok = False
        # (e) 只读过一次剪贴板：应用那次 read 是从**仓库**拿的，不是又去问了 X11
        dels = [e for e in x11.deliveries() if e == "delivered"]
        if len(dels) != 1:
            print("FAIL[读回]: 假 X 服务器被读了 %d 次（期望 1 次：预取那一次；"
                  "应用那次应当直接命中仓库）" % len(dels))
            ok = False
        else:
            print("OK[读回]: 剪贴板只被读了 1 次（应用那次命中的是仓库）")
        return ok
    finally:
        x11.stop()


# ---------------------------------------------------------------- 3：不吞键

def check_never_swallows(work):
    """没有 DISPLAY（没有 X11）时，Ctrl-V 仍然必须原样到达子进程。"""
    home = os.path.join(work, "home_nox")
    os.makedirs(home, exist_ok=True)
    env = base_env(home)
    # 子进程把收到的前两个字节以十六进制打印出来（1616 = 两个 Ctrl-V）。
    argv = [BIN, "--program=" + sys.executable, "--", ECHO_HEX, "2"]
    out = run_pty(env, argv, [(1.0, CTRL_V), (1.4, CTRL_V)], settle=1.0)
    text = out.decode(errors="replace")
    if "GOT_HEX=1616" not in text:
        print("FAIL[不吞键]: 预取不可用时 Ctrl-V 未原样转发（输出 %r）" % text[-200:])
        print("  说明: 预取失败绝不能吃掉用户的按键 —— 这比贴不上图严重得多")
        return False
    print("OK[不吞键]: 无 X11 时两个 Ctrl-V 都原样到达子进程（1616）")
    return True


# ---------------------------------------------------------------- 4：前缀优先

def check_prefix_wins(work):
    """--prefix=^v 时，^v 是前缀键（不做预取、不转发）。"""
    home = os.path.join(work, "home_prefix")
    os.makedirs(home, exist_ok=True)
    env = base_env(home)
    argv = [BIN, "--prefix=^v", "--program=" + sys.executable, "--", ECHO_HEX, "2"]
    # ^v 之后按 q：应当结束会话（子进程一个字节都收不到，因此永远不会打印 GOT_HEX）
    out = run_pty(env, argv, [(1.0, CTRL_V), (1.3, b"q")], settle=1.2)
    text = out.decode(errors="replace")
    if "GOT_HEX" in text:
        print("FAIL[前缀优先]: --prefix=^v 时 ^v 被转发给了子进程（输出 %r）" % text[-200:])
        return False
    print("OK[前缀优先]: --prefix=^v 时 ^v q 仍是前缀组合（子进程收不到字节）")
    return True


# ---------------------------------------------------------------- 5：关得掉

def check_disable(work):
    """--ctrl-v-import=no 与配置 ctrl-v-import = no 都不入库，但按键照转发。"""
    ok = True

    # (a) 命令行关掉
    home = os.path.join(work, "home_off")
    os.makedirs(home, exist_ok=True)
    x11 = FakeX(work, "inline", "off")
    try:
        env = x11.env(home)
        argv = [BIN, "--ctrl-v-import=no", "--program=" + sys.executable, "--", ECHO_HEX, "1"]
        out = run_pty(env, argv, [(1.2, CTRL_V)], settle=1.0)
        text = out.decode(errors="replace")
        if "GOT_HEX=16" not in text:
            print("FAIL[关掉a]: 关掉预取后 Ctrl-V 没有被转发（输出 %r）" % text[-200:])
            ok = False
        elif clip_count(env) != 0:
            print("FAIL[关掉a]: --ctrl-v-import=no 之后仓库仍然进了条目")
            ok = False
        elif x11.deliveries():
            print("FAIL[关掉a]: 关掉后仍然去读了本机剪贴板")
            ok = False
        else:
            print("OK[关掉a]: --ctrl-v-import=no 不入库、不读 X11，按键照转发")
    finally:
        x11.stop()

    # (b) 配置文件关掉（命令行没提这件事 → 配置文件说了算）
    home2 = os.path.join(work, "home_off_cfg")
    os.makedirs(os.path.join(home2, ".config", "uyassh"), exist_ok=True)
    with open(os.path.join(home2, ".config", "uyassh", "config"), "w") as fh:
        fh.write("# t13\nctrl-v-import = no\n")
    x11b = FakeX(work, "inline", "offcfg")
    try:
        env = x11b.env(home2)
        argv = [BIN, "--program=" + sys.executable, "--", ECHO_HEX, "1"]
        out = run_pty(env, argv, [(1.2, CTRL_V)], settle=1.0)
        text = out.decode(errors="replace")
        if "GOT_HEX=16" not in text:
            print("FAIL[关掉b]: 配置关掉后 Ctrl-V 没有被转发（输出 %r）" % text[-200:])
            ok = False
        elif clip_count(env) != 0:
            print("FAIL[关掉b]: 配置 ctrl-v-import = no 之后仓库仍然进了条目")
            ok = False
        else:
            print("OK[关掉b]: 配置 ctrl-v-import = no 生效（不入库，按键照转发）")
    finally:
        x11b.stop()

    # (c) 命令行优先于配置文件：配置说 no，命令行说 yes → 应当入库
    home3 = os.path.join(work, "home_cli_wins")
    os.makedirs(os.path.join(home3, ".config", "uyassh"), exist_ok=True)
    with open(os.path.join(home3, ".config", "uyassh", "config"), "w") as fh:
        fh.write("ctrl-v-import = no\n")
    x11c = FakeX(work, "inline", "cliwins")
    try:
        env = x11c.env(home3)
        argv = [BIN, "--ctrl-v-import=yes", "--program=" + sys.executable, "--", ECHO_HEX, "1"]
        out = run_pty(env, argv, [(1.2, CTRL_V)], settle=1.2)
        text = out.decode(errors="replace")
        if "GOT_HEX=16" not in text:
            print("FAIL[关掉c]: 命令行说 yes 时 Ctrl-V 没转发（输出 %r）" % text[-200:])
            ok = False
        elif clip_count(env) != 1:
            print("FAIL[关掉c]: 命令行 --ctrl-v-import=yes 没能盖过配置里的 no（条目数 %d）"
                  % clip_count(env))
            ok = False
        else:
            print("OK[关掉c]: 命令行 --ctrl-v-import=yes 优先于配置文件的 no")
    finally:
        x11c.stop()
    return ok


# ---------------------------------------------------------------- 6：去抖与去重

def check_dedupe(work):
    """连按两次同一个图：只产生一条条目，但两个 Ctrl-V 都到达子进程。"""
    home = os.path.join(work, "home_dupe")
    os.makedirs(home, exist_ok=True)
    x11 = FakeX(work, "inline", "dupe")
    try:
        env = x11.env(home)
        argv = [BIN, "--program=" + sys.executable, "--", ECHO_HEX, "2"]
        # 两次间隔 0.6s：大于去抖窗口（250ms），所以两次都会真的去读一次剪贴板，
        # 内容相同 → 第二次应当只切当前项、不新建条目。
        out = run_pty(env, argv, [(1.2, CTRL_V), (1.8, CTRL_V)], settle=1.2)
        text = out.decode(errors="replace")
        ok = True
        if "GOT_HEX=1616" not in text:
            print("FAIL[去重]: 两个 Ctrl-V 没有都到达子进程（输出 %r）" % text[-200:])
            ok = False
        n = clip_count(env)
        if n != 1:
            print("FAIL[去重]: 同一张图按两次产生了 %d 条条目（期望 1 条）" % n)
            ok = False
        if ok:
            print("OK[去重]: 两次 Ctrl-V 都转发、同一张图只入库一条")
        return ok
    finally:
        x11.stop()


def main():
    if not os.path.exists(BIN):
        print("FAIL: 先 make build（缺 %s）" % BIN, file=sys.stderr)
        return 1
    work = tempfile.mkdtemp(prefix="uyassh_t13_")
    ok = True
    try:
        if not check_order_and_read(work):
            ok = False
        if not check_never_swallows(work):
            ok = False
        if not check_prefix_wins(work):
            ok = False
        if not check_disable(work):
            ok = False
        if not check_dedupe(work):
            ok = False
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not ok:
        print("t13 失败", file=sys.stderr)
        return 1
    print("OK: t13 Ctrl-V 预取（先入库、再交给 ssh）全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
