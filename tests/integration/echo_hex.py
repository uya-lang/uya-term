#!/usr/bin/env python3
"""把 stdin 收到的前 N 个字节以十六进制打印（用于断言透传的原始字节）。"""

import os
import sys
import tty

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def main():
    tty.setraw(0)              # 关回显，避免控制字符被 tty 渲染
    data = b""
    while len(data) < N:
        chunk = os.read(0, N - len(data))
        if not chunk:
            break
        data += chunk
    print("GOT_HEX=%s" % data.hex(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
