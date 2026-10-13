#!/bin/sh
# t11: 零依赖静态产物 —— 无 INTERP/NEEDED、无未定义符号、最小环境可运行、
#      clip 二进制往返、PTY 端到端与热键路径。
# 说明：默认构建（STATIC=1）必须产出静态可执行文件；make check STATIC=0 时本测试跳过。
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/bin/uya-ssh"
TMP="$ROOT/tests/tmp"
mkdir -p "$TMP"

if [ "${STATIC:-1}" = "0" ]; then
    echo "SKIP: STATIC=0（动态链接构建，本用例只验证静态产物）"
    exit 0
fi

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

[ -x "$BIN" ] || fail "找不到可执行文件 $BIN"

# 1) ELF 层面：没有解释器（不依赖 ld.so）、没有动态段 NEEDED（不依赖 libc.so）
if command -v readelf >/dev/null 2>&1; then
    readelf -l "$BIN" | grep -q 'INTERP' && fail "产物仍带 ELF 解释器（应静态链接）"
    readelf -d "$BIN" 2>/dev/null | grep -q '(NEEDED)' && fail "产物仍有动态段 NEEDED（应零依赖）"
else
    file "$BIN" | grep -q 'statically linked' || fail "产物不是静态链接"
fi

# 2) 未定义符号：零依赖产物不应残留动态符号需求
if command -v nm >/dev/null 2>&1; then
    if nm -u "$BIN" 2>/dev/null | grep -q ' U '; then
        echo "FAIL: 产物存在未定义符号：" >&2
        nm -u "$BIN" 2>/dev/null | grep ' U ' | head >&2
        exit 1
    fi
fi

# 3) 最小环境（env -i，无 PATH/HOME）下可运行
out=$(env -i "$BIN" --version) || fail "env -i 下 --version 退出码非 0"
case "$out" in
    *uya-ssh*) ;;
    *) fail "env -i 下 --version 输出异常：$out" ;;
esac
env -i "$BIN" --help >/dev/null || fail "env -i 下 --help 退出码非 0"

# 4) 最小环境 + 仅 HOME：剪贴板仓库二进制往返（图片字节不被破坏）
HOME_DIR="$TMP/t11_home"
rm -rf "$HOME_DIR"
mkdir -p "$HOME_DIR"
IMG="$TMP/t11_clip.png"
printf '\211PNG\r\n\032\nT11_ZERO_DEP_IMAGE\000\377' > "$IMG"
env -i HOME="$HOME_DIR" "$BIN" clip add "$IMG" >/dev/null || fail "clip add 失败"
env -i HOME="$HOME_DIR" "$BIN" clip list | grep -q 'image/png' || fail "clip list 未识别 image/png"
env -i HOME="$HOME_DIR" "$BIN" clip get > "$TMP/t11_out.bin" || fail "clip get 失败"
cmp -s "$IMG" "$TMP/t11_out.bin" || fail "clip get 与 clip add 的字节不一致"
printf 'plain text' | env -i HOME="$HOME_DIR" "$BIN" clip add >/dev/null || fail "clip add(stdin) 失败"
env -i HOME="$HOME_DIR" "$BIN" clip list | grep -q 'text/plain' || fail "clip list 未识别 text/plain"
env -i HOME="$HOME_DIR" "$BIN" clip clear >/dev/null || fail "clip clear 失败"

# 5) 最小环境下的 PTY 端到端：尺寸下发、输出转发、退出码透传
cat > "$TMP/t11.json" <<'EOF'
{
  "cmd": ["@BIN@", "--program=/bin/sh", "--", "-c", "stty size; echo MARK_T11_ZERO_DEP; exit 7"],
  "rows": 29, "cols": 103,
  "timeout": 15,
  "expect_exit": 7,
  "expect_contains": ["29 103", "MARK_T11_ZERO_DEP"]
}
EOF
sed -i "s|@BIN@|$BIN|" "$TMP/t11.json"
env -i PATH=/usr/bin:/bin HOME="$HOME_DIR" \
    python3 "$ROOT/tests/integration/ptydrive.py" "$TMP/t11.json" \
    || fail "最小环境下 PTY 端到端失败"

# 6) 最小环境下的热键路径：前缀键 + l 就地列出本地仓库（不依赖外部命令）
cat > "$TMP/t11_hotkey.json" <<'EOF'
{
  "cmd": ["@BIN@", "--program=/bin/sh", "--", "-c", "echo READY_T11; read x; exit 0"],
  "rows": 24, "cols": 80,
  "timeout": 15,
  "expect_exit": 0,
  "steps": [
    {"expect": "READY_T11"},
    {"sleep": 0.3},
    {"send_raw": "\\x1d"},
    {"send": "l"},
    {"sleep": 0.5},
    {"send_raw": "\\x1d"},
    {"send": "q"}
  ],
  "expect_contains": ["剪贴板仓库"]
}
EOF
sed -i "s|@BIN@|$BIN|" "$TMP/t11_hotkey.json"
env -i PATH=/usr/bin:/bin HOME="$HOME_DIR" \
    python3 "$ROOT/tests/integration/ptydrive.py" "$TMP/t11_hotkey.json" \
    || fail "最小环境下剪贴板热键失败"

echo "OK: t11 零依赖静态产物（无 INTERP/NEEDED，env -i 下可运行）"
