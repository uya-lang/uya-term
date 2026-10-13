#!/usr/bin/env python3
"""t06 闭环测试：uya-ssh 作为 5522 终端侧，让假 TUI 应用粘贴到预置图片。

断言：
  * 应用能拿到 DECRPM 应答（说明我们在扮演终端）
  * 应用启用粘贴模式后，本地热键（前缀键 + p）能触发一次粘贴事件，且带一次性口令
  * 应用用口令读回的字节与仓库中预置的图片完全一致
"""

import base64
import fcntl
import os
import pty
import select
import shutil
import struct
import sys
import tempfile
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "bin", "uya-ssh")
APP = os.path.join(ROOT, "tests", "integration", "fake_tui_app.py")
IMAGE = b"\x89PNG\r\n\x1a\nT06_PRETTY_IMAGE_BYTES\x00\xff"

PREFIX = b"\x1d"  # Ctrl-]：默认前缀键


def run():
    home = tempfile.mkdtemp(prefix="uyassh_t06_")
    img_path = os.path.join(home, "image.png")
    with open(img_path, "wb") as fh:
        fh.write(IMAGE)

    env = dict(os.environ)
    env["HOME"] = home

    # 先把图片放进仓库
    add = os.spawnve(os.P_WAIT, BIN, [BIN, "clip", "add", img_path], env)
    if add != 0:
        print("FAIL: clip add 退出码 %d" % add, file=sys.stderr)
        return 1

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["HOME"] = home
        os.execve(BIN, [BIN, "--program=" + sys.executable, "--", APP], env)

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
        pump(2.0)
        os.write(fd, PREFIX + b"p")          # 触发粘贴事件
        pump(5.0)
        os.write(fd, b"\x1d" + b"q")         # 退出会话
        pump(1.0)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        shutil.rmtree(home, ignore_errors=True)

    text = bytes(out).decode(errors="replace")
    ok = True
    for key in ("DECRPM_SEEN=True", "PASTE_EVENT_SEEN=True", "PW_PRESENT=True"):
        if key not in text:
            print("FAIL: 缺少 %s" % key, file=sys.stderr)
            ok = False

    expected_b64 = base64.b64encode(IMAGE).decode()
    if ("IMAGE_B64=" + expected_b64) not in text:
        got = ""
        for line in text.splitlines():
            if line.strip().startswith("IMAGE_B64="):
                got = line.strip()
        print("FAIL: 图片字节不一致\n  期望 IMAGE_B64=%s\n  实际 %s" % (expected_b64, got or "(无)"),
              file=sys.stderr)
        ok = False

    if not ok:
        print("---- 实际输出 ----", file=sys.stderr)
        print(text.replace("\r\n", "\n"), file=sys.stderr)
        return 1
    print("OK: t06 5522 闭环（%d 字节图片原样读回）" % len(IMAGE))
    return 0


if __name__ == "__main__":
    sys.exit(run())
