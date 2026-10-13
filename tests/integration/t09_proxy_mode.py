#!/usr/bin/env python3
"""t09：代理模式（本地终端支持 5522）与 serve 模式的模式选择。

用 python 驱动一个"支持 5522 的假终端"：设置 KITTY_WINDOW_ID 让 uya-ssh 进入代理模式，
断言 uya-ssh 会向终端索取剪贴板（发出 type=read;Lg==），而不会把请求误发给子进程。
随后假终端回应 MIME 列表 → uya-ssh 应继续请求数据 → 假终端给出 PNG → 子进程应用
应收到一次完整的粘贴事件且能读到该图片。
"""

import base64
import os
import pty
import select
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "bin", "uya-ssh")
APP = os.path.join(ROOT, "tests", "integration", "fake_tui_app.py")
PREFIX = b"\x1d"
IMAGE = b"\x89PNG\r\n\x1a\nT09_TERMINAL_CLIPBOARD\x00"
ST = b"\x1b\\"


def main():
    env = dict(os.environ)
    env["KITTY_WINDOW_ID"] = "1"          # 让 uya-ssh 认为本地终端支持 5522
    env["HOME"] = os.environ.get("HOME", "/tmp")

    pid, fd = pty.fork()
    if pid == 0:
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

    seen_mime_request = False
    seen_data_request = False
    ok = True

    try:
        pump(2.0)
        os.write(fd, PREFIX + b"p")       # 热键：触发粘贴
        pump(1.0)

        body = bytes(out)
        marker = b"\x1b]5522;type=read"
        if marker in body:
            seen_mime_request = b";Lg==" in body or b"Lg==" in body
            seen_data_request = b"aW1hZ2UvcG5n" in body or b"image/png" in body
            # 回应"假终端"：MIME 列表 + 数据
            mime_list = base64.b64encode(b"image/png").decode()
            os.write(fd, b"\x1b]5522;type=read:status=OK" + ST)
            resp_mime = base64.b64encode(b".").decode()
            os.write(fd, ("\x1b]5522;type=read:status=DATA:mime=%s;%s" % (resp_mime, mime_list)).encode() + ST)
            os.write(fd, b"\x1b]5522;type=read:status=DONE" + ST)
            pump(2.0)
            # 回应数据请求
            data_b64 = base64.b64encode(IMAGE).decode()
            os.write(fd, b"\x1b]5522;type=read:status=OK" + ST)
            os.write(fd, ("\x1b]5522;type=read:status=DATA:mime=%s;%s" % (mime_list, data_b64)).encode() + ST)
            os.write(fd, b"\x1b]5522;type=read:status=DONE" + ST)
            pump(3.0)
        else:
            print("FAIL: 代理模式下 uya-ssh 未向终端索取剪贴板", file=sys.stderr)
            ok = False

        os.write(fd, PREFIX + b"q")
        pump(1.0)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    text = bytes(out).decode(errors="replace")
    if not seen_mime_request:
        print("FAIL: 未看到向终端请求 MIME 列表（type=read;Lg==）", file=sys.stderr)
        ok = False
    if "PASTE_EVENT_SEEN=True" not in text:
        print("FAIL: 子进程应用未收到粘贴事件", file=sys.stderr)
        ok = False
    expected = base64.b64encode(IMAGE).decode()
    if ("IMAGE_B64=" + expected) not in text:
        got = [ln.strip() for ln in text.splitlines() if "IMAGE_B64=" in ln]
        print("FAIL: 应用读到的图片与终端剪贴板不一致\n  期望 %s\n  实际 %s"
              % (expected, got[0] if got else "(无)"), file=sys.stderr)
        ok = False

    if not ok:
        print("---- 实际输出 ----", file=sys.stderr)
        print(text.replace("\r\n", "\n"), file=sys.stderr)
        return 1
    print("OK: t09 代理模式（向本地终端取剪贴板并转交应用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
