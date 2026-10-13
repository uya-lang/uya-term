#!/bin/sh
# t05: 真实 ssh 端到端 —— 用临时 sshd（自定义端口/主机密钥/授权密钥）验证
#      uya-ssh 包住系统 ssh 后能完成握手、分配远端 PTY、取回输出与退出码。
#      需要 sudo -n 可用；不可用时打印 SKIP 并以 0 退出（由 t01/t02/t04 覆盖本地能力）。
#      注意：命令模式下 ssh 默认不申请远端 PTY，因此这里显式传 -tt。
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN="$ROOT/bin/uya-ssh"
TMP="$ROOT/tests/tmp"
SSHD_DIR="$TMP/sshd"
PORT=${UYA_SSH_TEST_PORT:-2222}

rm -rf "$SSHD_DIR"
mkdir -p "$SSHD_DIR"
chmod 755 "$SSHD_DIR"

if ! sudo -n true 2>/dev/null; then
    echo "SKIP: 需要 sudo -n 才能启动临时 sshd"
    exit 0
fi

USER_NAME=$(id -un)
ssh-keygen -q -t rsa -b 2048 -N "" -f "$SSHD_DIR/host_rsa"
ssh-keygen -q -t ed25519 -N "" -f "$SSHD_DIR/user_key"
cat "$SSHD_DIR/user_key.pub" > "$SSHD_DIR/authorized_keys"
chmod 600 "$SSHD_DIR/authorized_keys" "$SSHD_DIR/user_key" "$SSHD_DIR/host_rsa"

cat > "$SSHD_DIR/sshd_config" <<EOF
Port $PORT
ListenAddress 127.0.0.1
HostKey $SSHD_DIR/host_rsa
PidFile $SSHD_DIR/sshd.pid
AuthorizedKeysFile $SSHD_DIR/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
StrictModes no
AllowUsers $USER_NAME
Subsystem sftp internal-sftp
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

cat > "$TMP/t05.json" <<EOF
{
  "cmd": ["$BIN", "-tt", "-i", "$SSHD_DIR/user_key", "-p", "$PORT",
          "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=$SSHD_DIR/known_hosts",
          "-o", "BatchMode=yes",
          "$USER_NAME@127.0.0.1", "echo REMOTE_MARK; tty; exit 9"],
  "timeout": 30,
  "expect_exit": 9,
  "expect_contains": ["REMOTE_MARK", "/dev/pts/"]
}
EOF

python3 "$ROOT/tests/integration/ptydrive.py" "$TMP/t05.json"
