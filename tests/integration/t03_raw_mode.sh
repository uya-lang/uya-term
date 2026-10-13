#!/bin/sh
# t03: 终端模式 —— 运行期间外层 tty 处于 raw（关闭 icanon/echo），退出后必须恢复。
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/bin/uya-ssh"
TMP="$ROOT/tests/tmp"
mkdir -p "$TMP"

BIN="$BIN" python3 - <<'PY'
import os, pty, select, termios, time

BIN = os.environ["BIN"]
pid, fd = pty.fork()
if pid == 0:
    os.execv(BIN, [BIN, "--program=/bin/sh", "--", "-c", "sleep 1.5; exit 0"])

time.sleep(0.5)
mid = termios.tcgetattr(fd)
while True:
    r, _, _ = select.select([fd], [], [], 0.3)
    if r:
        try:
            os.read(fd, 4096)
        except OSError:
            pass
    w, _ = os.waitpid(pid, os.WNOHANG)
    if w == pid:
        break
time.sleep(0.3)
after = termios.tcgetattr(fd)

raw_entered = not (mid[3] & termios.ICANON) and not (mid[3] & termios.ECHO)
restored = bool(after[3] & termios.ICANON) and bool(after[3] & termios.ECHO)
if raw_entered and restored:
    print("OK: t03 raw 模式进入并恢复")
    raise SystemExit(0)
print("FAIL: raw_entered=%s restored=%s" % (raw_entered, restored), file=__import__("sys").stderr)
raise SystemExit(1)
PY
