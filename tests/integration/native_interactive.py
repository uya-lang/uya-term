#!/usr/bin/env python3
"""t14 的交互会话子用例：在真实伪终端里驱动原生 SSH 会话。

为什么必须单独写一个、而不是用 ptydrive.py：这里要断言的是**本地终端的
termios 状态**（raw 模式进来没有、退出后恢复没有）和 **Ctrl-C 的语义**，
而 ptydrive.py 是「发字节、等输出」的线性脚本，看不到这两件事。

断言清单（每一条都对应一个真实缺陷）：
  1. 会话期间本地终端进入 raw（ICANON/ECHO/ISIG 都关）。ISIG 关掉是关键：
     不关的话本地内核会把 Ctrl-C 变成给我们**自己**的 SIGINT，客户端直接
     消失、远端命令被孤儿化。
  2. 按键实时到达远端（登录后敲 `echo`，输出里能看到）。
  3. 远端申请到的 pty 尺寸 = 本地尺寸（不是写死的 80x24）。
  4. $TERM 传到了远端（不是空串回落成 dumb）。
  5. Ctrl-C 中断**远端**命令（sleep 被干掉），而本地客户端存活、还能继续交互。
  6. 正常退出后本地终端属性被恢复（ICANON/ECHO 回来），否则用户回到一个
     无回显的 shell，只能 `reset`。

用法: interactive.py <uya-ssh 路径> <user@host:port>
退出码: 0 通过，1 失败。
"""

import os
import pty
import select
import struct
import sys
import termios
import time
import fcntl


def pump(fd, seconds, out):
    """读一段时间的输出。"""
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            if data:
                out.extend(data)


def main():
    if len(sys.argv) != 3:
        print("用法: interactive.py <uya-ssh> <user@host:port>", file=sys.stderr)
        return 2
    binpath, target = sys.argv[1], sys.argv[2]
    rows, cols = 41, 137

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execv(binpath, [binpath, "--transport=native", target])

    # 把本地 pty 设成明确的尺寸，用来验证远端 pty 尺寸确实来自本地
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    out = bytearray()
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)
            print("FAIL: %s" % msg, file=sys.stderr)

    # 等登录完成（提示符出现）
    deadline = time.time() + 25
    while time.time() < deadline and b"# " not in bytes(out) and b"$ " not in bytes(out):
        pump(fd, 0.5, out)
    check(len(out) > 0, "登录后没有收到任何输出")

    mid = termios.tcgetattr(fd)
    check(not (mid[3] & termios.ICANON), "会话期间 ICANON 仍开着（没进 raw 模式）")
    check(not (mid[3] & termios.ECHO), "会话期间 ECHO 仍开着（没进 raw 模式）")
    check(not (mid[3] & termios.ISIG),
          "会话期间 ISIG 仍开着：本地内核会把 Ctrl-C 变成给自己的 SIGINT")

    # 2/3/4) 按键转发、pty 尺寸、TERM
    os.write(fd, b"echo T14_$TERM; stty size\r")
    time.sleep(1.5)
    pump(fd, 3, out)
    body = bytes(out)
    check(b"T14_xterm-256color" in body, "$TERM 没有正确传到远端：%r" % body[-200:])
    check(("%d %d" % (rows, cols)).encode() in body,
          "远端 pty 尺寸不是本地尺寸 %dx%d：%r" % (rows, cols, body[-200:]))

    # 5) Ctrl-C：远端命令被中断，本地会话存活并能继续用
    # 标记刻意写成 SHOULD_NOT_"PRINT"：**输入的那行文本里不含** SHOULD_NOT_PRINT
    # 这个子串（多了两个引号），只有命令真的跑完才会打印出无引号的版本。
    # 直接写 `echo SHOULD_NOT_PRINT` 的话，终端回显的命令行本身就命中了断言，
    # 「被中断」与「跑完了」看起来完全一样——第一版就是这么假绿的。
    mark = len(out)
    os.write(fd, b'sleep 30; echo SHOULD_NOT_"PRINT"\r')
    pump(fd, 2, out)
    os.write(fd, b"\x03")
    pump(fd, 3, out)
    try:
        exited = os.waitpid(pid, os.WNOHANG)[0] != 0
    except ChildProcessError:
        exited = True
    check(not exited, "Ctrl-C 把本地 uya-ssh 打死了（应该只中断远端命令）")
    if not exited:
        os.write(fd, b"echo STILL_ALIVE_T14\r")
        pump(fd, 4, out)
        check(b"STILL_ALIVE_T14" in bytes(out)[mark:],
              "Ctrl-C 之后会话不能继续交互")
    check(b"SHOULD_NOT_PRINT" not in bytes(out)[mark:], "被 Ctrl-C 中断的命令居然跑完了")

    # 6) 正常退出 -> 终端恢复
    if not exited:
        os.write(fd, b"exit 0\r")
        pump(fd, 4, out)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        # 给它一点时间走完恢复路径
        pump(fd, 1, out)
    after = termios.tcgetattr(fd)
    check(bool(after[3] & termios.ICANON), "退出后 ICANON 没恢复（终端被留在 raw 模式）")
    check(bool(after[3] & termios.ECHO), "退出后 ECHO 没恢复（用户会看到无回显的 shell）")

    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass

    if fails:
        print("---- 实际输出 ----", file=sys.stderr)
        print(bytes(out).decode(errors="replace")[-1500:], file=sys.stderr)
        return 1
    print("OK: t14 交互会话（raw/Ctrl-C/尺寸/TERM/终端恢复）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
