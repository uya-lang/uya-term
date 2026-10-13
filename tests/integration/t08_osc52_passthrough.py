#!/usr/bin/env python3
"""t08：OSC 52 透传与 5522 应答不泄漏。

断言三件事：
  1. 子进程发出的 OSC 52（文本剪贴板）原样出现在本地输出里（不被吞掉）；
  2. 子进程的 DECRQM 查询（CSI ? 5522 $ p）由 uya-ssh 应答，且该应答不会跑到本地屏幕；
  3. 普通输出（含 ESC 开头的其他序列）保持原样。
"""

import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "bin", "uya-ssh")

# 子进程：
#  1. 发 OSC 52（应被 uya-ssh 原样透传到本地屏幕）
#  2. 发 CSI ? 5522 $ p（应由 uya-ssh 应答，且应答只作为"键盘输入"回到本进程）
#  3. 把读到的应答以十六进制打印出来，供测试断言
SCRIPT = (
    "stty -echo -icanon min 1 time 0;"
    "printf '\\033]52;c;aGVsbG8=\\007';"
    "printf 'ESC52_DONE\\n';"
    "printf '\\033[?5522$p';"
    "REPLY=$(head -c 11 </dev/tty | od -An -tx1 | tr -d ' \\n');"
    "printf 'DECRPM_HEX=%s\\n' \"$REPLY\";"
    "printf '\\033[1;31mRED\\033[0m\\n';"
    "sleep 0.5;"
    "printf 'TAIL\\n';"
    "exit 0"
)


def main():
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(BIN, [BIN, "--program=/bin/sh", "--", "-c", SCRIPT])

    out = bytearray()
    end = time.time() + 12
    status = None
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                data = os.read(fd, 65536)
                if data:
                    out.extend(data)
            except OSError:
                pass
        try:
            w, st = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            break
        if w == pid:
            status = st
            # 读净残余
            for _ in range(10):
                r, _, _ = select.select([fd], [], [], 0.1)
                if not r:
                    break
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                out.extend(data)
            break

    text = bytes(out).decode(errors="replace")
    ok = True

    # 1. OSC 52 必须原样透传
    if "\x1b]52;c;aGVsbG8=\x07" not in text:
        print("FAIL: OSC 52 未原样透传到本地终端", file=sys.stderr)
        ok = False
    # 2. DECRQM 应答必须由我们产生并回写到子进程输入：ESC [ ? 5 5 2 2 ; 2 $ y = 1b5b3f353532323b322479
    if "DECRPM_HEX=1b5b3f353532323b322479" not in text:
        got = [ln.strip() for ln in text.splitlines() if "DECRPM_HEX=" in ln]
        print("FAIL: 子进程未收到预期的 DECRPM 应答\n  期望 DECRPM_HEX=1b5b3f353532323b322479\n  实际 %s"
              % (got[0] if got else "(无)"), file=sys.stderr)
        ok = False
    # 应答不应出现在本地屏幕上（那是回显错误）
    if "\x1b[?5522;2$y" in text:
        print("FAIL: DECRPM 应答泄漏到了本地屏幕", file=sys.stderr)
        ok = False
    # 3. 其他转义序列保持原样
    if "\x1b[1;31mRED\x1b[0m" not in text:
        print("FAIL: 普通 SGR 序列被破坏", file=sys.stderr)
        ok = False
    for marker in ("ESC52_DONE", "TAIL"):
        if marker not in text:
            print("FAIL: 缺少普通输出 %s" % marker, file=sys.stderr)
            ok = False

    try:
        os.close(fd)
    except OSError:
        pass

    if not ok:
        print("---- 实际输出 ----", file=sys.stderr)
        print(repr(text), file=sys.stderr)
        return 1
    print("OK: t08 OSC 52 透传、5522 DECRQM 应答、SGR 原样")
    return 0


if __name__ == "__main__":
    sys.exit(main())
