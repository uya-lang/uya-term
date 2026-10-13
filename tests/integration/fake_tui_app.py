#!/usr/bin/env python3
"""假 TUI 应用：通过 OSC 5522 粘贴协议读回剪贴板里的图片。

流程（与真实编码助手一致）：
  1. 打开粘贴事件私有模式       CSI ? 5522 h
  2. 查询支持情况               CSI ? 5522 $ p   （期待 CSI ? 5522 ; Ps $ y）
  3. 等待用户触发粘贴，收到 OK/DATA(mime=Lg==)/DONE 三段事件，并取得一次性口令 pw
  4. 用 pw 发起 read，请求 image/png
  5. 把得到的原始字节以 base64 打印成 IMAGE_B64=<...> 供测试断言
"""

import base64
import os
import sys
import time
import tty

ST = "\x1b\\"


def osc(body):
    """构造 OSC 5522 报文（不含 ESC ] 5522; 前缀的重复部分）。"""
    return "\x1b]5522;" + body + ST


def read_until(deadline, needle):
    """读到出现 needle 或超时为止。"""
    buf = b""
    while time.time() < deadline:
        try:
            chunk = os.read(0, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if needle.encode() in buf:
            return buf
    return buf


def main():
    tty.setraw(0)
    os.write(1, b"\x1b[?5522h")
    os.write(1, b"\x1b[?5522$p")
    resp = read_until(time.time() + 3, "$y")
    print("DECRPM_SEEN=%s" % (b"$y" in resp), flush=True)

    event = read_until(time.time() + 20, "status=DONE")
    text = event.decode(errors="replace")
    has_ok = "type=read:status=OK" in text
    has_list = "DATA:mime=Lg==" in text
    print("PASTE_EVENT_SEEN=%s" % (has_ok and has_list), flush=True)

    pw = ""
    for part in text.split(":"):
        if part.startswith("pw="):
            pw = part[3:].split("\x1b")[0]
    print("PW_PRESENT=%s" % (len(pw) > 0), flush=True)

    want = base64.b64encode(b"image/png").decode()
    os.write(1, osc("type=read:pw=%s:name=UGFzdGUgZXZlbnQ=;%s" % (pw, want)).encode())
    data = read_until(time.time() + 5, "status=DONE")

    payload = b""
    marker = b"status=DATA:mime="
    idx = data.find(marker)
    if idx >= 0:
        rest = data[idx + len(marker):]
        semi = rest.find(b";")
        end = rest.find(b"\x1b\\")
        if 0 <= semi < end:
            payload = base64.b64decode(rest[semi + 1:end])
    print("IMAGE_BYTES=%d" % len(payload), flush=True)
    print("IMAGE_B64=%s" % base64.b64encode(payload).decode(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
