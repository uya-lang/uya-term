#!/bin/sh
# t01: PTY 包装透传 —— 窗口尺寸下发、输出转发、退出码原样返回。
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/bin/uya-ssh"
TMP="$ROOT/tests/tmp"
mkdir -p "$TMP"

cat > "$TMP/t01.json" <<'EOF'
{
  "cmd": ["@BIN@", "--program=/bin/sh", "--", "-c", "stty size; echo MARK_T01; exit 7"],
  "rows": 33, "cols": 111,
  "timeout": 15,
  "expect_exit": 7,
  "expect_contains": ["33 111", "MARK_T01"]
}
EOF
sed -i "s|@BIN@|$BIN|" "$TMP/t01.json"
exec python3 "$ROOT/tests/integration/ptydrive.py" "$TMP/t01.json"
