#!/usr/bin/env python3
"""伪终端驱动：在 PTY 中运行被测程序，按 JSON 脚本发送输入/改窗口尺寸/发信号，
最后断言输出片段与退出码。

用法:
    ptydrive.py <spec.json>

spec.json:
{
  "cmd": ["bin/uya-ssh", "--program=/bin/sh", "--", "-c", "..."],
  "rows": 30, "cols": 90,          # 初始窗口尺寸（可选）
  "timeout": 10,                    # 总超时秒数（可选，默认 10）
  "expect_exit": 7,                 # 期望退出码（可选）
  "steps": [                        # 顺序执行
     {"expect": "MARK"},            # 等待输出中出现该片段
     {"send": "text"},              # 向 PTY 写字节
     {"send_raw": "\\x1d"},         # 写字节，支持 \\xNN 转义
     {"sleep": 0.3},
     {"resize": [44, 132]},         # 改变 PTY 尺寸
     {"signal": 28}                 # 向被测进程发信号
  ],
  "expect_contains": ["A", "B"],    # 结束时输出的断言
  "expect_not_contains": ["C"]
}
退出码: 0 通过，1 断言失败，2 规范文件问题。
"""

import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
import fcntl


def decode_raw(text):
    """把 \\xNN 转义还原为字节。"""
    out = bytearray()
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "x" and i + 3 < len(text) + 1:
                out.append(int(text[i + 2:i + 4], 16))
                i += 4
                continue
            if nxt == "n":
                out.append(10)
                i += 2
                continue
            if nxt == "r":
                out.append(13)
                i += 2
                continue
            if nxt == "\\":
                out.append(92)
                i += 2
                continue
        out.extend(text[i].encode())
        i += 1
    return bytes(out)


def main():
    if len(sys.argv) != 2:
        print("用法: ptydrive.py <spec.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    cmd = spec["cmd"]
    rows = spec.get("rows", 24)
    cols = spec.get("cols", 80)
    timeout = spec.get("timeout", 10)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)
        os._exit(127)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    buf = bytearray()
    exited = {"done": False, "code": None}
    deadline = time.time() + timeout

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    data = os.read(fd, 65536)
                    if data:
                        buf.extend(data)
                except OSError:
                    # Linux 上 PTY master 在从端全部关闭后 read 返回 EIO；
                    # 这里不直接退出，仍需用 waitpid 收集退出状态。
                    pass
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                exited["done"] = True
                if exited["code"] is None:
                    exited["code"] = -1
                return
            if wpid == pid:
                exited["done"] = True
                exited["code"] = os.waitstatus_to_exitcode(status)
                return

    def wait_for(needle, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if needle.encode() in bytes(buf):
                return True
            pump(0.1)
            if exited["done"]:
                return needle.encode() in bytes(buf)
        return needle.encode() in bytes(buf)

    ok = True
    for step in spec.get("steps", []):
        if "expect" in step:
            if not wait_for(step["expect"], min(5.0, timeout)):
                print("FAIL: 未在输出中看到 %r" % step["expect"], file=sys.stderr)
                ok = False
                break
        elif "send" in step:
            os.write(fd, step["send"].encode())
        elif "send_raw" in step:
            os.write(fd, decode_raw(step["send_raw"]))
        elif "sleep" in step:
            pump(step["sleep"])
        elif "resize" in step:
            r, c = step["resize"]
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", r, c, 0, 0))
        elif "signal" in step:
            os.kill(pid, step["signal"])
        else:
            print("FAIL: 未知步骤 %r" % step, file=sys.stderr)
            ok = False
            break

    while not exited["done"] and time.time() < deadline:
        pump(0.2)
    if not exited["done"]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            _, status = os.waitpid(pid, 0)
            exited["done"] = True
            exited["code"] = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            exited["done"] = True
            exited["code"] = -1
        print("FAIL: 超时（%ss）" % timeout, file=sys.stderr)
        ok = False

    text = bytes(buf).decode(errors="replace")
    for needle in spec.get("expect_contains", []):
        if needle not in text:
            print("FAIL: 输出缺少 %r" % needle, file=sys.stderr)
            ok = False
    for needle in spec.get("expect_not_contains", []):
        if needle in text:
            print("FAIL: 输出不应包含 %r" % needle, file=sys.stderr)
            ok = False
    if "expect_exit" in spec and exited["code"] != spec["expect_exit"]:
        print("FAIL: 退出码 %r，期望 %r" % (exited["code"], spec["expect_exit"]), file=sys.stderr)
        ok = False

    if not ok:
        print("---- 实际输出 ----", file=sys.stderr)
        print(text.replace("\r\n", "\\n\n"), file=sys.stderr)
        print("---- 退出码: %r ----" % exited["code"], file=sys.stderr)
        return 1
    print("OK: %s" % " ".join(cmd[len(cmd) - min(len(cmd), 6):]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
