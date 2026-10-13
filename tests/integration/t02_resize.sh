#!/bin/sh
# t02: 运行中改变窗口尺寸 —— 子进程 (stty size) 应看到新尺寸。
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/bin/uya-ssh"
TMP="$ROOT/tests/tmp"
mkdir -p "$TMP"

cat > "$TMP/t02.json" <<'EOF'
{
  "cmd": ["@BIN@", "--program=/bin/sh", "--", "-c", "stty size; read line; stty size; exit 0"],
  "rows": 30, "cols": 90,
  "timeout": 20,
  "steps": [
    {"expect": "30 90"},
    {"resize": [44, 132]},
    {"sleep": 0.4},
    {"send": "\n"},
    {"expect": "44 132"}
  ],
  "expect_exit": 0,
  "expect_contains": ["30 90", "44 132"]
}
EOF
sed -i "s|@BIN@|$BIN|" "$TMP/t02.json"
exec python3 "$ROOT/tests/integration/ptydrive.py" "$TMP/t02.json"
