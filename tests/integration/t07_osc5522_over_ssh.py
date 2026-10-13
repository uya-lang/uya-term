#!/usr/bin/env python3
"""t07 真实 ssh + 5522：在远端会话中粘贴本机图片。

用临时 sshd 建连，远端运行一个会发 OSC 5522 粘贴请求的应用；
本机侧 uya-ssh 负责应答（终端侧），断言远端应用拿到的字节与本机仓库里的图片一致。
需要 sudo -n 起 sshd；不可用时打印 SKIP 并返回 0。
"""

import base64
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "bin", "uya-ssh")
APP = os.path.join(ROOT, "tests", "integration", "fake_tui_app.py")
PORT = os.environ.get("UYA_SSH_TEST_PORT", "2223")
IMAGE = b"\x89PNG\r\n\x1a\nT07_OVER_SSH_IMAGE\x00\x01\x02\xff"
PREFIX = b"\x1d"


def sudo_ok():
    return subprocess.call(["sudo", "-n", "true"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def main():
    if not sudo_ok():
        print("SKIP: 需要 sudo -n 才能启动临时 sshd")
        return 0

    work = tempfile.mkdtemp(prefix="uyassh_t07_")
    home = os.path.join(work, "home")
    os.makedirs(home)
    user = os.environ.get("USER") or subprocess.check_output(["id", "-un"]).decode().strip()

    def run(cmd, **kw):
        return subprocess.call(cmd, **kw)

    # 主机密钥与用户密钥
    hs = os.path.join(work, "host_rsa")
    us = os.path.join(work, "user_key")
    run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", hs], stdout=subprocess.DEVNULL)
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", us], stdout=subprocess.DEVNULL)
    auth = os.path.join(work, "authorized_keys")
    shutil.copyfile(us + ".pub", auth)
    os.chmod(auth, 0o600)
    os.chmod(us, 0o600)

    conf = os.path.join(work, "sshd_config")
    with open(conf, "w") as fh:
        fh.write(
            "Port %s\nListenAddress 127.0.0.1\nHostKey %s\nPidFile %s\n"
            "AuthorizedKeysFile %s\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\nUsePAM no\nPermitRootLogin no\n"
            "StrictModes no\nAllowUsers %s\n" % (PORT, hs, os.path.join(work, "sshd.pid"),
                                                 auth, user)
        )

    if run(["sudo", "-n", "/usr/sbin/sshd", "-f", conf]) != 0:
        print("FAIL: 无法启动临时 sshd", file=sys.stderr)
        shutil.rmtree(work, ignore_errors=True)
        return 1
    time.sleep(1)

    img = os.path.join(home, "img.png")
    with open(img, "wb") as fh:
        fh.write(IMAGE)
    env = dict(os.environ)
    env["HOME"] = home
    if run([BIN, "clip", "add", img], env=env) != 0:
        print("FAIL: clip add 失败", file=sys.stderr)
        return 1

    remote_cmd = "%s %s" % (sys.executable, APP)
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["HOME"] = home
        os.execve(BIN, [BIN, "-tt", "-i", us, "-p", PORT,
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=" + os.path.join(work, "known_hosts"),
                        "-o", "BatchMode=yes",
                        "%s@127.0.0.1" % user, remote_cmd], env)

    out = bytearray()

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if data:
                    out.extend(data)

    try:
        pump(4.0)                    # 等 ssh 建连、远端应用启动并发出 DECRQM
        os.write(fd, PREFIX + b"p")  # 触发粘贴
        pump(6.0)
        os.write(fd, PREFIX + b"q")
        pump(1.0)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(os.path.join(work, "sshd.pid")):
            try:
                with open(os.path.join(work, "sshd.pid")) as fh:
                    subprocess.call(["sudo", "-n", "kill", fh.read().strip()])
            except Exception:
                pass
        subprocess.call(["sudo", "-n", "pkill", "-f", "sshd -f " + conf],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(work, ignore_errors=True)

    text = bytes(out).decode(errors="replace")
    ok = True
    for key in ("DECRPM_SEEN=True", "PASTE_EVENT_SEEN=True", "PW_PRESENT=True"):
        if key not in text:
            print("FAIL: 远端应用未看到 %s" % key, file=sys.stderr)
            ok = False
    expected = base64.b64encode(IMAGE).decode()
    if ("IMAGE_B64=" + expected) not in text:
        got = [ln.strip() for ln in text.splitlines() if "IMAGE_B64=" in ln]
        print("FAIL: 跨 ssh 的图片字节不一致\n  期望 %s\n  实际 %s"
              % (expected, got[0] if got else "(无)"), file=sys.stderr)
        ok = False
    if not ok:
        print("---- 实际输出 ----", file=sys.stderr)
        print(text.replace("\r\n", "\n"), file=sys.stderr)
        return 1
    print("OK: t07 真实 ssh 会话中粘贴图片（%d 字节原样读回）" % len(IMAGE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
