#!/usr/bin/env python3
"""t10：配置文件生效（前缀键）与默认行为对照。

* 无配置时 ^A 是普通字节，必须原样转发给子进程；
* 配置里写 `prefix = ^a` 后，^A 成为前缀键，^A q 应结束会话（不再回显 ^Aq）。
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "bin", "uya-ssh")
DRIVER = os.path.join(ROOT, "tests", "integration", "ptydrive.py")


# 子进程把 stdin 的原始字节以十六进制打印，避免 tty 回显把控制字符渲染成 ^A。
HEXDUMP = [sys.executable, os.path.join(ROOT, "tests", "integration", "echo_hex.py"), "2"]


def write_spec(path, prefix_byte, expect_contains):
    spec = {
        "cmd": [BIN, "--program=" + HEXDUMP[0], "--"] + HEXDUMP[1:],
        "timeout": 6,
        "steps": [
            {"sleep": 0.8},
            {"send_raw": prefix_byte},
            {"send": "q"},
            {"sleep": 0.8},
        ],
    }
    if expect_contains is not None:
        spec["expect_contains"] = expect_contains
    else:
        # 期望会话被前缀键 + q 结束：进程应正常退出（退出码 0）
        spec["expect_exit"] = 0
    with open(path, "w") as fh:
        json.dump(spec, fh)


def run(spec_path, home):
    env = dict(os.environ)
    env["HOME"] = home
    env.pop("XDG_CONFIG_HOME", None)     # 避免宿主环境影响判定
    return subprocess.call([sys.executable, DRIVER, spec_path], env=env,
                           stdout=subprocess.DEVNULL)


def main():
    work = tempfile.mkdtemp(prefix="uyassh_t10_")
    plain = os.path.join(work, "plain")
    configured = os.path.join(work, "configured")
    os.makedirs(os.path.join(configured, ".config", "uyassh"))
    os.makedirs(plain)
    with open(os.path.join(configured, ".config", "uyassh", "config"), "w") as fh:
        fh.write("# t10 配置\nprefix = ^a\n")

    # 无配置：^A 与 q 应作为裸字节到达子进程 → 十六进制 01 71
    spec_forward = os.path.join(work, "forward.json")
    write_spec(spec_forward, "\\x01", ["GOT_HEX=0171"])
    ok_plain = run(spec_forward, plain) == 0

    # 配置 prefix=^a：^A q 被本层消费，子进程什么都收不到 → 会话被结束
    spec_quit = os.path.join(work, "quit.json")
    write_spec(spec_quit, "\\x01", None)
    ok_configured = run(spec_quit, configured) == 0

    # 交叉验证：默认前缀是 ^]，配置了 ^a 之后 ^] 不再退出
    # 交叉验证：配置了 ^a 之后，^] 不再是前缀键 → 裸字节 1d 71 到达子进程
    spec_cross = os.path.join(work, "cross.json")
    with open(spec_cross, "w") as fh:
        json.dump({
            "cmd": [BIN, "--program=" + HEXDUMP[0], "--"] + HEXDUMP[1:],
            "timeout": 6,
            "steps": [{"sleep": 0.8}, {"send_raw": "\\x1d"}, {"send": "q"}, {"sleep": 0.8}],
            "expect_contains": ["GOT_HEX=1d71"],
        }, fh)
    ok_cross = run(spec_cross, configured) == 0

    rc = 0
    if not ok_plain:
        print("FAIL: 无配置时 ^A 未被原样转发", file=sys.stderr)
        rc = 1
    if not ok_configured:
        print("FAIL: 配置 prefix=^a 后 ^A q 未能结束会话", file=sys.stderr)
        rc = 1
    if not ok_cross:
        print("FAIL: 配置 prefix=^a 后 ^] 仍被当作前缀键", file=sys.stderr)
        rc = 1
    if rc == 0:
        print("OK: t10 配置文件前缀键生效（默认 ^] 转发、配置 ^a 生效）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
