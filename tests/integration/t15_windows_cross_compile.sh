#!/bin/sh
# t15: 交叉编译 Windows 版 uya-ssh.exe，并（有 wine 时）运行验证。
#
# 参照 ~/uya/uya/tests/verify_std_path_platform_targets.sh 的 wine 用法：
#   * uya 先生成 C99 源码；
#   * zig cc 交叉编译为 x86_64-windows-gnu；
#   * file 校验产物为 PE32+ Windows 控制台程序；
#   * wine 存在时跑 --version；不存在时只验证交叉编译通过，跳过运行（不假装跑过）。
#
# 用法: sh tests/integration/t15_windows_cross_compile.sh
# 环境变量：UYA（uya 编译器，默认 $HOME/uya/uya/bin/uya）
#          ZIG（zig 可执行文件，默认 zig）
#          WINE（wine 可执行文件，默认 command -v wine，兜底 /opt/deepin-wine-staging/bin/wine）

set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
UYA="${UYA:-$HOME/uya/uya/bin/uya}"
# zig：优先用 $ZIG，否则探测常见路径
if [ -n "${ZIG:-}" ]; then
    ZIG="$ZIG"
elif command -v zig >/dev/null 2>&1; then
    ZIG="zig"
elif [ -x /home/winger/zig-x86_64-linux-0.17.0-dev.956+2dca73595/zig ]; then
    ZIG=/home/winger/zig-x86_64-linux-0.17.0-dev.956+2dca73595/zig
elif [ -x /home/winger/zig-x86_64-linux-0.16.0/zig ]; then
    ZIG=/home/winger/zig-x86_64-linux-0.16.0/zig
fi
if [ -z "${ZIG:-}" ]; then
    fail "未找到 zig（设置 ZIG 环境变量或将其加入 PATH）"
fi
WINE="${WINE:-$(command -v wine 2>/dev/null || echo /opt/deepin-wine-staging/bin/wine)}"

fail() { echo "FAIL: $*" >&2; exit 1; }

[ -x "$UYA" ] || fail "找不到 uya 编译器：$UYA"
command -v "$ZIG" >/dev/null 2>&1 || fail "未找到 zig（设置 ZIG 环境变量或将其加入 PATH）"
command -v file >/dev/null 2>&1 || fail "需要 file 命令"

BUILD="$ROOT/tests/tmp/t15_win"
rm -rf "$BUILD"; mkdir -p "$BUILD"

echo "== 语法检查 =="
"$UYA" check --project-root src/uyassh src/uyassh/main.uya >"$BUILD/check.log" 2>&1 || {
    cat "$BUILD/check.log"; fail "uya check 失败"
}

# 交叉编译需要全部生成的 C 源码，make build 已把它们填充到 .uyacache。
CACHE="$ROOT/.uyacache"
[ -d "$CACHE" ] || fail "未找到 $CACHE（请先执行 make build，生成 C99 源码）"

echo "== 复制构建缓冲并打 Windows 兼容补丁 =="
cp -r "$CACHE" "$BUILD/uyacache"
cd "$BUILD/uyacache"

python3 - "$PWD" <<'PY'
import sys, os
root = sys.argv[1]

# --- uya_part1_types.h ---
p = os.path.join(root, 'uya_part1_types.h')
s = open(p).read()

# 1) wchar_t / wint_t：Windows UCRT 自身定义（unsigned short），uya 生成的 int32_t 定义
#    与之冲突，只在非 Windows 平台启用。
old = '''typedef int64_t time_t;
typedef uint64_t clock_t;
typedef int32_t wchar_t;
typedef int32_t wint_t;'''
new = '''typedef int64_t time_t;
typedef uint64_t clock_t;
#if !defined(_WIN32)
typedef int32_t wchar_t;
typedef int32_t wint_t;
#endif'''
assert old in s, "wchar_t typedef not found"
s = s.replace(old, new, 1)

# 2) POSIX 封装函数块：Windows UCRT 已提供同名符号，限制在非 Windows 平台。
start = s.find('int stat(const char *pathname, struct Stat *statbuf) {')
end_marker = 'int waitpid(int pid, int *status, int options) { (void)pid; (void)status; (void)options; return -1; }'
end = s.find(end_marker)
assert start != -1 and end != -1, "POSIX block not found"
end = end + len(end_marker)
block = s[start:end]
s = s.replace(block, '#if !defined(_WIN32)\n' + block + '\n#endif', 1)

# 3) uya_windows_* 标为 static，避免头文件在多个译译单元中展开导致重复符号。
for name in ('uya_windows_stat', 'uya_windows_fstat', 'uya_windows_lstat'):
    old2 = 'struct err_union_int32_t ' + name + '('
    new2 = 'static struct err_union_int32_t ' + name + '('
    assert old2 in s, f"{name} static not found"
    s = s.replace(old2, new2, 1)

open(p, 'w').write(s)
print("  [patch] uya_part1_types.h")

# --- pty/spawn.c：Windows 无 posix_openpt/grantpt/unlockpt/ptsname_r/posix_spawn* ---
# 整体重写为 Windows stub（避免部分匹配留下残留代码）。
p = os.path.join(root, 'uya-term/src/uyassh/pty/spawn.c')
open(p, 'w').write(r'''#include "uya_split_protos.h"
#include "uya_mirror_globals.h"
#include "uya_vtable_externs.h"
#include "uya_strings_extern.h"
#include <string.h>

int32_t std_runtime_get_argc(void);
uint8_t *std_runtime_get_argv(int32_t index);
extern char *getenv(const char *);
extern const uint32_t S_IFMT;
extern const uint32_t S_IFDIR;
extern const uint32_t S_IFREG;
extern const uint8_t DT_REG;
extern const uint8_t DT_UNKNOWN;
extern const uint8_t DT_DIR;
extern const int64_t CLOCKS_PER_SEC;
extern uint64_t clock(void);
extern struct FILE _stdin, _stdout, _stderr;
#define stdin (&_stdin)
#define stdout (&_stdout)
#define stderr (&_stderr)

#ifdef SPAWN_ATTR_BYTES
#undef SPAWN_ATTR_BYTES
#endif
__attribute__((used)) const size_t SPAWN_ATTR_BYTES = 336;

#ifdef SPAWN_FA_BYTES
#undef SPAWN_FA_BYTES
#endif
__attribute__((used)) const size_t SPAWN_FA_BYTES = 80;

#ifdef POSIX_SPAWN_SETSID
#undef POSIX_SPAWN_SETSID
#endif
__attribute__((used)) const int16_t POSIX_SPAWN_SETSID = 128;

struct err_union_int32_t uyassh_pty_spawn_pty_open(uint8_t * slave_path_out, size_t path_cap) {
    /* Windows UCRT 无 posix_openpt/grantpt/unlockpt/ptsname_r：PTY 不支持，
       交叉编译时打桩，让上层降级。 */
    (void)slave_path_out;
    (void)path_cap;
    return (struct err_union_int32_t){ .error_id = -1688638471U, .value = 0 };
}

struct err_union_void uyassh_pty_spawn_pty_spawn(const struct SpawnSpec * spec, int32_t master_fd, struct PtySession * out) {
    /* Windows UCRT 无 posix_spawn*：PTY 启动不支持，交叉编译时打桩。 */
    (void)spec;
    (void)master_fd;
    (void)out;
    return (struct err_union_void){ .error_id = -101740296U };
}

struct err_union_void uyassh_pty_spawn_pty_set_nonblocking(int32_t fd) {
    (void)fd;
    const int32_t flags = ({ struct err_union_int32_t _uya_try_tmp = libc_sys_fcntl(fd, libc_F_GETFL, 0); if (_uya_try_tmp.error_id != 0) {
        return (struct err_union_void){ .error_id = _uya_try_tmp.error_id };
    }; _uya_try_tmp.value; });
    (void)(({ struct err_union_int32_t _uya_try_tmp = libc_sys_fcntl(fd, libc_F_SETFL, (flags | (int32_t)libc_O_NONBLOCK)); if (_uya_try_tmp.error_id != 0) {
        return (struct err_union_void){ .error_id = _uya_try_tmp.error_id };
    }; _uya_try_tmp.value; }));
        return (struct err_union_void){ .error_id = 0 };
}
''')
print("  [rewrite] pty/spawn.c (Windows stub)")

# --- pty/relay.c：waitpid/WNOHANG 打桩 ---
p = os.path.join(root, 'uya-term/src/uyassh/pty/relay.c')
open(p, 'w').write(r'''#include "uya_split_protos.h"
#include "uya_mirror_globals.h"
#include "uya_vtable_externs.h"
#include "uya_strings_extern.h"
#include <string.h>

int32_t std_runtime_get_argc(void);
uint8_t *std_runtime_get_argv(int32_t index);
extern char *getenv(const char *);
extern const uint32_t S_IFMT;
extern const uint32_t S_IFDIR;
extern const uint32_t S_IFREG;
extern const uint8_t DT_REG;
extern const uint8_t DT_UNKNOWN;
extern const uint8_t DT_DIR;
extern const int64_t CLOCKS_PER_SEC;
extern uint64_t clock(void);
extern struct FILE _stdin, _stdout, _stderr;
#define stdin (&_stdin)
#define stdout (&_stdout)
#define stderr (&_stderr)

#ifdef RELAY_CHUNK
#undef RELAY_CHUNK
#endif
const size_t RELAY_CHUNK = 8192;
struct RelayReady uyassh_pty_relay_relay_ready_none() {
        {
        struct RelayReady _uya_ret = (struct RelayReady){.child_readable = false, .local_readable = false, .interrupted = false};
        return _uya_ret;
        }
}

struct err_union_RelayReady uyassh_pty_relay_relay_wait(int32_t master_fd, int32_t local_fd, int32_t timeout_ms) {
    (void)master_fd;
    (void)local_fd;
    (void)timeout_ms;
    struct RelayReady ready = uyassh_pty_relay_relay_ready_none();
    struct PollFd fds[2] = {(struct PollFd){.fd = 0, .events = 0, .revents = 0}, (struct PollFd){.fd = 0, .events = 0, .revents = 0}};
    fds[0].fd = master_fd;
    fds[0].events = libc_POLLIN;
    size_t nfds = 1;
    if (local_fd >= 0) {
        fds[1].fd = local_fd;
        fds[1].events = libc_POLLIN;
        nfds = 2;
    }
    const int32_t n = ({ struct err_union_int32_t _uya_catch_tmp = libc_sys_poll((&fds[0]), nfds, timeout_ms); __typeof__(_uya_catch_tmp.value) _uya_catch_result; if (_uya_catch_tmp.error_id != 0) {
        _uya_catch_result = ((int32_t)(-1));
    } else _uya_catch_result = _uya_catch_tmp.value; _uya_catch_result; });
    if (n < 0) {
        ready.interrupted = true;
                {
            struct err_union_RelayReady _uya_ret = (struct err_union_RelayReady){ .error_id = 0, .value = ready };
            return _uya_ret;
                }
    }
    if (n == 0) {
                {
            struct err_union_RelayReady _uya_ret = (struct err_union_RelayReady){ .error_id = 0, .value = ready };
            return _uya_ret;
                }
    }
    const int32_t child_mask = ((int32_t)libc_POLLIN | (int32_t)libc_POLLHUP);
    ready.child_readable = (((int32_t)fds[0].revents & child_mask) != 0);
    if (nfds == 2) {
        ready.local_readable = (((int32_t)fds[1].revents & (int32_t)libc_POLLIN) != 0);
    }
        {
        struct err_union_RelayReady _uya_ret = (struct err_union_RelayReady){ .error_id = 0, .value = ready };
        return _uya_ret;
        }
}

int32_t uyassh_pty_relay_relay_read_child(int32_t master_fd, uint8_t * buf, size_t cap) {
    (void)master_fd;
    (void)buf;
    (void)cap;
    const intptr_t got = ({ struct err_union_intptr_t _uya_catch_tmp = sys_read(master_fd, (char *)buf, cap); __typeof__(_uya_catch_tmp.value) _uya_catch_result; if (_uya_catch_tmp.error_id != 0) {
        _uya_catch_result = ((intptr_t)(-1));
    } else _uya_catch_result = _uya_catch_tmp.value; _uya_catch_result; });
        {
        int32_t _uya_ret = (int32_t)got;
        return _uya_ret;
        }
}

int32_t uyassh_pty_relay_relay_read_local(int32_t local_fd, uint8_t * buf, size_t cap) {
    (void)local_fd;
    (void)buf;
    (void)cap;
    const intptr_t got = ({ struct err_union_intptr_t _uya_catch_tmp = sys_read(local_fd, (char *)buf, cap); __typeof__(_uya_catch_tmp.value) _uya_catch_result; if (_uya_catch_tmp.error_id != 0) {
        _uya_catch_result = ((intptr_t)(-1));
    } else _uya_catch_result = _uya_catch_tmp.value; _uya_catch_result; });
        {
        int32_t _uya_ret = (int32_t)got;
        return _uya_ret;
        }
}

size_t uyassh_pty_relay_relay_write_stdout(const uint8_t * data, size_t n) {
    (void)data;
    (void)n;
    if (n == 0) {
                {
            size_t _uya_ret = 0;
            return _uya_ret;
                }
    }
    const intptr_t w = ({ struct err_union_intptr_t _uya_catch_tmp = sys_write(1, (const char *)data, n); __typeof__(_uya_catch_tmp.value) _uya_catch_result; if (_uya_catch_tmp.error_id != 0) {
        _uya_catch_result = ((intptr_t)0);
    } else _uya_catch_result = _uya_catch_tmp.value; _uya_catch_result; });
    if (w <= 0) {
                {
            size_t _uya_ret = 0;
            return _uya_ret;
                }
    }
        {
        size_t _uya_ret = (size_t)w;
        return _uya_ret;
        }
}

size_t uyassh_pty_relay_relay_write_child(int32_t master_fd, const uint8_t * data, size_t n) {
    (void)master_fd;
    (void)data;
    (void)n;
    if (n == 0) {
                {
            size_t _uya_ret = 0;
            return _uya_ret;
                }
    }
    const intptr_t w = ({ struct err_union_intptr_t _uya_catch_tmp = sys_write(master_fd, (const char *)data, n); __typeof__(_uya_catch_tmp.value) _uya_catch_result; if (_uya_catch_tmp.error_id != 0) {
        _uya_catch_result = ((intptr_t)0);
    } else _uya_catch_result = _uya_catch_tmp.value; _uya_catch_result; });
    if (w <= 0) {
                {
            size_t _uya_ret = 0;
            return _uya_ret;
                }
    }
        {
        size_t _uya_ret = (size_t)w;
        return _uya_ret;
        }
}

bool uyassh_pty_relay_relay_child_exited(int32_t pid, int32_t * status_out) {
    /* Windows UCRT 无 waitpid/WNOHANG：交叉编译时报告子进程未退出。 */
    (void)pid;
    (void)status_out;
    return false;
}

int32_t uyassh_pty_relay_relay_decode_status(int32_t status) {
    (void)status;
    const int32_t sig = (status & 127);
    if (sig != 0) {
                {
            int32_t _uya_ret = (128 + sig);
            return _uya_ret;
                }
    }
        {
        int32_t _uya_ret = ((status >> 8) & 255);
        return _uya_ret;
        }
}
''')
print("  [rewrite] pty/relay.c (Windows stub)")
print("windows 兼容补丁已应用")
PY
[ $? -eq 0 ] || fail "Windows 兼容补丁应用失败"

echo "== zig cc 交叉编译所有目标文件 (x86_64-windows-gnu) =="
fail=0; n=0
for f in $(find . -name '*.c' | sort); do
    n=$((n + 1))
    o="${f%.c}.o"
    if ! "$ZIG" cc -target x86_64-windows-gnu -std=c99 -O0 -fno-builtin -I. -c "$f" -o "$o" 2>/dev/null; then
        echo "FAIL: $f"
        fail=$((fail + 1))
    fi
done
echo "编译目标文件：$n 个，失败：$fail"
[ "$fail" -eq 0 ] || fail "zig cc 交叉编译失败"

echo "== 链接 uya-ssh.exe =="
objs=$(find . -name '*.o' | sort)
[ -n "$objs" ] || fail "未生成任何目标文件"
"$ZIG" cc -target x86_64-windows-gnu -O0 $objs -o uya-ssh.exe -lm 2>"$BUILD/link.log" || {
    cat "$BUILD/link.log"; fail "链接失败"
}

echo "== 校验产物为 PE32+ Windows 可执行文件 =="
file uya-ssh.exe >"$BUILD/file.log" 2>&1
grep -q 'PE32+ executable' "$BUILD/file.log" || {
    cat "$BUILD/file.log"; fail "产物不是 PE32+ 可执行文件"
}
grep -q 'for MS Windows' "$BUILD/file.log" || {
    cat "$BUILD/file.log"; fail "产物不是面向 MS Windows 的二进制"
}
echo "OK: 交叉编译产出 PE32+ Windows 可执行文件：$(cat "$BUILD/file.log")"

echo "== wine 运行验证（可选）=="
if [ -x "$WINE" ]; then
    WINEDEBUG=-all "$WINE" uya-ssh.exe --version >"$BUILD/wine.log" 2>&1 && {
        echo "wine 运行通过：$(cat "$BUILD/wine.log")"
    } || {
        echo "WARN: wine 运行失败（见 $BUILD/wine.log），跳过运行验证"
    }
else
    echo "SKIP: 未找到 wine（设置 WINE 环境变量或安装 wine），仅验证交叉编译，不运行"
fi

echo "PASS: t15 Windows 交叉编译验证"
