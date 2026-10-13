#!/bin/sh
# t04: 参数原样透传 —— 包含空格、引号、重定向等特殊字符的参数必须逐字送达子进程。
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/bin/uya-ssh"
TMP="$ROOT/tests/tmp"
mkdir -p "$TMP"

cat > "$TMP/mock_ssh.sh" <<'EOF'
#!/bin/sh
# 模拟 ssh：逐行打印收到的参数，便于断言原样透传。
i=0
for a in "$@"; do
  printf 'ARG[%d]=<%s>\n' "$i" "$a"
  i=$((i + 1))
done
printf 'TTY=%s\n' "$(tty 2>/dev/null || echo none)"
exit 0
EOF
chmod +x "$TMP/mock_ssh.sh"

cat > "$TMP/t04.json" <<'EOF'
{
  "cmd": ["@BIN@", "--program=@MOCK@", "--", "-p", "2222", "-o", "StrictHostKeyChecking=no",
          "user@host", "echo hi && ls -l", "a b  c", "quote'\"double", "$HOME/*.txt"],
  "timeout": 15,
  "expect_exit": 0,
  "expect_contains": ["ARG[0]=<-p>", "ARG[1]=<2222>", "ARG[3]=<StrictHostKeyChecking=no>",
                      "ARG[4]=<user@host>", "ARG[5]=<echo hi && ls -l>", "ARG[6]=<a b  c>",
                      "ARG[7]=<quote'\"double>", "TTY=/dev/pts/"]
}
EOF
sed -i "s|@BIN@|$BIN|; s|@MOCK@|$TMP/mock_ssh.sh|" "$TMP/t04.json"
exec python3 "$ROOT/tests/integration/ptydrive.py" "$TMP/t04.json"
