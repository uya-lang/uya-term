#!/bin/sh
# t14: 原生 SSH 传输端到端 —— 用临时 sshd 验证 uya-ssh 自己的 SSH 实现
#      （不经过系统 ssh）能完成握手、主机密钥校验、公钥认证、开通道并取回输出。
#
# 与 t05 的区别：t05 验的是「包住系统 ssh」，本用例验的是**我们自己的协议栈**。
# 因此这里**不传任何 -i/-o 选项**（那是给系统 ssh 的），而是靠环境变量把
# uyassh 指向临时目录里的私钥与 known_hosts。
#
# 需要 sudo -n 可用；不可用时打印 SKIP 并以 0 退出。

set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/bin/uya-ssh"
TMP="$ROOT/tests/tmp"
SSHD_DIR="$TMP/sshd_native"
PORT=${UYA_NATIVE_TEST_PORT:-2262}

rm -rf "$SSHD_DIR"
mkdir -p "$SSHD_DIR"
chmod 755 "$SSHD_DIR"

if ! sudo -n true 2>/dev/null; then
    echo "SKIP: 需要 sudo -n 才能启动临时 sshd"
    exit 0
fi

USER_NAME=$(id -un)
# 主机密钥用 ed25519（与我们实现的主机密钥算法一致）。
ssh-keygen -q -t ed25519 -N "" -f "$SSHD_DIR/host_key"
# 用户密钥用 RSA-2048：这条路径会走 rsassa-pkcs1 + SHA-256 的完整实现。
ssh-keygen -q -t rsa -b 2048 -N "" -f "$SSHD_DIR/user_key"
cat "$SSHD_DIR/user_key.pub" > "$SSHD_DIR/authorized_keys"
chmod 600 "$SSHD_DIR/authorized_keys" "$SSHD_DIR/user_key" "$SSHD_DIR/host_key"

cat > "$SSHD_DIR/sshd_config" <<EOF
Port $PORT
ListenAddress 127.0.0.1
HostKey $SSHD_DIR/host_key
PidFile $SSHD_DIR/sshd.pid
AuthorizedKeysFile $SSHD_DIR/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
StrictModes no
AllowUsers $USER_NAME
EOF

cleanup() {
    if [ -f "$SSHD_DIR/sshd.pid" ]; then
        sudo -n kill "$(sudo -n cat "$SSHD_DIR/sshd.pid")" 2>/dev/null || true
    fi
    sudo -n pkill -f "sshd -f $SSHD_DIR/sshd_config" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sudo -n /usr/sbin/sshd -f "$SSHD_DIR/sshd_config" -E "$SSHD_DIR/sshd.log"
sleep 1

if ! ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    echo "FAIL: 临时 sshd 未在端口 $PORT 监听" >&2
    cat "$SSHD_DIR/sshd.log" >&2 || true
    exit 1
fi

# uyassh 从 ~/.ssh 读私钥与 known_hosts，因此用 HOME 指向临时目录来隔离。
mkdir -p "$SSHD_DIR/home/.ssh"
cp "$SSHD_DIR/user_key" "$SSHD_DIR/home/.ssh/id_rsa"
chmod 600 "$SSHD_DIR/home/.ssh/id_rsa"
# known_hosts 用 ssh-keyscan 生成（明文条目），覆盖真实格式
ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 > "$SSHD_DIR/home/.ssh/known_hosts" 2>/dev/null
chmod 600 "$SSHD_DIR/home/.ssh/known_hosts"

fail() {
    echo "FAIL: $1" >&2
    [ -f "$SSHD_DIR/sshd.log" ] && tail -20 "$SSHD_DIR/sshd.log" >&2 || true
    exit 1
}

# --- 1) 原生 exec：应取回输出与退出码 ---
# 退出码必须**如实**传回来：不解析 exit-status 的话 `exit 7` 到本地会变成 0，
# 而调用方（脚本、CI）正是靠它判断成败的——「看起来成功」比报错危险得多。
OUT=$(HOME="$SSHD_DIR/home" timeout 60 "$BIN" --transport=native --no-pty \
        "$USER_NAME@127.0.0.1:$PORT" "echo NATIVE_MARK; exit 7" 2>&1) || RC=$?
RC=${RC:-0}
echo "$OUT" | grep -q "NATIVE_MARK" || fail "原生 exec 输出缺少 NATIVE_MARK（实际：$OUT）"
[ "$RC" -eq 7 ] || fail "原生 exec 退出码是 $RC，期望 7（exit-status 没被解析？）"

# --- 1b) 大输出必须完整 ---
# 收包缓冲曾经只有 8 KiB，而 sshd 会把输出按最多 32 KiB 打包：一个「大包」
# 就被整包丢掉，表现为**输出凭空消失、退出码还是 0**。这里用远超单包上限的
# 输出量逐行比对（只数行数不够——中间丢一段也能凑够行数）。
BIG_LINES=5000
BIG=$(HOME="$SSHD_DIR/home" timeout 60 "$BIN" --transport=native --no-pty \
        "$USER_NAME@127.0.0.1:$PORT" "seq 1 $BIG_LINES" 2>/dev/null) || true
BIG_N=$(printf '%s\n' "$BIG" | grep -c '^[0-9]' || true)
[ "$BIG_N" -eq "$BIG_LINES" ] || fail "大输出被截断：收到 $BIG_N 行，期望 $BIG_LINES 行"
# 内容也要逐行对：首行/末行/中间抽样
echo "$BIG" | sed -n '1p' | grep -qx '1' || fail "大输出首行不是 1"
echo "$BIG" | sed -n "${BIG_LINES}p" | grep -qx "$BIG_LINES" || fail "大输出末行不是 $BIG_LINES"

# --- 1c) stdin 必须转发，且 EOF 后远端能收尾（不能永久挂住）---
# 这条覆盖两个曾经的致命缺陷：poll 的 pollfd 没填（events=0）导致 stdin
# 永远不被读；以及本地 EOF 后不发 CHANNEL_EOF，读 stdin 的命令（cat 等）
# 就永远等不到输入结束。用 timeout 兜底：挂住 = 失败。
STDIN_OUT=$(printf 'STDIN_PAYLOAD\n' | HOME="$SSHD_DIR/home" timeout 45 "$BIN" \
        --transport=native --no-pty "$USER_NAME@127.0.0.1:$PORT" "cat" 2>&1) || RC2=$?
RC2=${RC2:-0}
[ "$RC2" -ne 124 ] || fail "stdin 转发后 cat 挂死（超时）——EOF 没送达远端？"
echo "$STDIN_OUT" | grep -q "STDIN_PAYLOAD" || fail "stdin 内容没有到达远端：$STDIN_OUT"

# --- 2) 未知主机必须拒绝（不回落系统 ssh、更不执行命令）---
# 要点：必须**同端口**、只清空 known_hosts，才真的走到「主机未知」这个分支。
# 若换成另一个没在监听的端口，测到的其实是「连不上」——
# 那样即使主机校验被写坏，用例也照样是绿的（我第一版就犯了这个错）。
cp "$SSHD_DIR/home/.ssh/known_hosts" "$SSHD_DIR/home/.ssh/known_hosts.keep"
: > "$SSHD_DIR/home/.ssh/known_hosts"
chmod 600 "$SSHD_DIR/home/.ssh/known_hosts"
OUT2=$(HOME="$SSHD_DIR/home" timeout 60 "$BIN" --transport=native --no-pty \
        "$USER_NAME@127.0.0.1:$PORT" "echo SHOULD_NOT_RUN" 2>&1) || true
if echo "$OUT2" | grep -q "SHOULD_NOT_RUN"; then
    fail "未知主机居然执行了命令：$OUT2"
fi
echo "$OUT2" | grep -q "主机密钥未通过校验" || fail "未知主机未给出明确拒绝信息：$OUT2"
# 同一目标加 --accept-new 后应能连上（证明拒绝的确实是「未知」而非别的）
OUT2B=$(HOME="$SSHD_DIR/home" timeout 60 "$BIN" --transport=native --no-pty \
        --accept-new "$USER_NAME@127.0.0.1:$PORT" "echo ACCEPT_NEW_OK" 2>&1) || true
echo "$OUT2B" | grep -q "ACCEPT_NEW_OK" || fail "--accept-new 未能接受未知主机：$OUT2B"
mv "$SSHD_DIR/home/.ssh/known_hosts.keep" "$SSHD_DIR/home/.ssh/known_hosts"

# --- 3) 主机密钥不一致必须拒绝，且 **--accept-new 也不能放行** ---
# 这是安全关键路径：accept-new 只应放行「未知主机」，绝不该放行「密钥变了」。
# 注意端口非 22 时条目形式必须是 [host]:port，否则会被当成另一台主机
# （那样测试就变成了「未知主机」而不是「不一致」，测不到真正的分支）。
ssh-keygen -q -t ed25519 -N "" -f "$SSHD_DIR/other_host"
{
    printf '[127.0.0.1]:%s ' "$PORT"
    cut -d' ' -f1,2 < "$SSHD_DIR/other_host.pub"
} > "$SSHD_DIR/home/.ssh/known_hosts"
chmod 600 "$SSHD_DIR/home/.ssh/known_hosts"
OUT3=$(HOME="$SSHD_DIR/home" timeout 60 "$BIN" --transport=native --no-pty \
        --accept-new "$USER_NAME@127.0.0.1:$PORT" "echo SHOULD_NOT_RUN2" 2>&1) || true
if echo "$OUT3" | grep -q "SHOULD_NOT_RUN2"; then
    fail "--accept-new 放行了密钥不一致的主机（这是安全缺陷）：$OUT3"
fi
echo "$OUT3" | grep -q "主机密钥未通过校验" || fail "密钥不一致时未给出明确拒绝信息：$OUT3"
# 必须与「未知主机」区分开：未知可以加 --accept-new 继续，密钥不一致**不能**。
# 这里直接用**不带 --accept-new** 的调用再验一次，并且断言它也没执行命令。
OUT3B=$(HOME="$SSHD_DIR/home" timeout 60 "$BIN" --transport=native --no-pty \
        "$USER_NAME@127.0.0.1:$PORT" "echo SHOULD_NOT_RUN3" 2>&1) || true
if echo "$OUT3B" | grep -q "SHOULD_NOT_RUN3"; then
    fail "密钥不一致（无 accept-new）时居然执行了命令：$OUT3B"
fi

# --- 4) 交互会话：raw 模式、按键转发、Ctrl-C 的语义、退出后终端恢复 ---
# 这一段是用户报的「卡死、没输出、Ctrl-C 直接退出」的直接回归测试，
# 因此必须在**真实伪终端**里跑（用 ptydrive 那种线性脚本测不到 termios）。
# 注意文件名不以 t 开头：Makefile 用 t*.py 通配集成测试，本文件是它的子脚本。
#
# 先把 known_hosts 恢复成**正确**的那份：上面第 3 步故意写进了不一致的主机密钥，
# 不恢复的话这里的连接会在「主机密钥未通过校验」处被拒——那样本段测的就不是
# 交互行为了（血泪：第一版就忘了恢复，看到的是一堆与 raw 模式无关的失败）。
ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 > "$SSHD_DIR/home/.ssh/known_hosts" 2>/dev/null
chmod 600 "$SSHD_DIR/home/.ssh/known_hosts"
HOME="$SSHD_DIR/home" timeout 90 python3 "$ROOT/tests/integration/native_interactive.py" \
    "$BIN" "$USER_NAME@127.0.0.1:$PORT" || \
    fail "交互会话用例失败（见上面的输出）"

echo "PASS: 原生 SSH 传输（exec/大输出/stdin/未知主机拒绝/交互与 Ctrl-C）"