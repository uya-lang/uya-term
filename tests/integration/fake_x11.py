#!/usr/bin/env python3
"""假 X11 服务器：在自建 unix 套接字上扮演「拥有剪贴板的 X 服务器」。

为什么需要它：真 CLIPBOARD 属于用户的桌面会话。让测试去抢 :0 的所有权会和
dde-clipboard / klipper 打架，也会把别人的剪贴板内容读出来——既不可重复也不该做。
所以测试自己起一个只会应答剪贴板协议的「X 服务器」，uya-ssh 连它，全流程封闭。

实现的请求（按 X11 协议，小端）：
  CreateWindow(1) / ChangeWindowAttributes(2)   只记录，不应答
  InternAtom(16)                                分配原子 id
  GetProperty(20)                               读属性（支持 delete）
  GetSelectionOwner(23)                         返回所有者窗口（0 = 剪贴板空）
  ConvertSelection(24)                          服务 TARGETS 与 image/png（含 INCR）
  SetSelectionOwner(22) / SendEvent(25)         接受即可
其余请求一律忽略（不应答，客户端也不会等）。

用法: fake_x11.py <socket 路径> <inline|incr|empty|text> [秒数]
环境: 打印 READY 到 stdout 后开始服务。
"""

import os
import socket
import struct
import sys
import time

# opcode
OP_CREATE_WINDOW, OP_CHANGE_ATTR = 1, 2
OP_INTERN, OP_GETPROP, OP_SETPROP = 16, 20, 18
OP_SETSELOWN, OP_GETSELOWN, OP_CONVERTSEL = 22, 23, 24
# 事件/应答
EV_ERROR, EV_REPLY = 0, 1
EV_SEL_NOTIFY, EV_PROP_NOTIFY = 31, 28
SENT_EVENT_BIT = 0x80
# 预定义原子
ATOM_ATOM, ATOM_STRING = 4, 31
PNG = b"\x89PNG\r\n\x1a\n"


def u16(v):
    return struct.pack("<H", v)


def u32(v):
    return struct.pack("<I", v)


def r16(b, o):
    return struct.unpack("<H", b[o:o + 2])[0]


def r32(b, o):
    return struct.unpack("<I", b[o:o + 4])[0]


def pad4(n):
    return (4 - n % 4) % 4


def make_png(target):
    """造一张**结构完整**的真 PNG，大小接近 target（±几 KB）。

    为什么不能只写签名 + 随机字节：下游（uya-agent）会真去解析 IHDR 拿宽高，
    假 PNG 会被它判成「不是认识的图片格式」——那是另一条失败路径，
    会把「剪贴板到底通没通」这个被测点掩盖掉。
    """
    import zlib
    # 用 1x1 打底，再把 IDAT 撑到目标大小（补一个大的 tEXt 块）：
    # 既能过所有结构校验，又足够大以触发 INCR 分段。
    def chunk(typ, data):
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    out = PNG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
    # 用一块大 tEXt 把体积顶上去（合法的 PNG 辅助块，解析器都会跳过）。
    room = target - len(out) - 12 - 12
    if room > 0:
        text = b"Comment\x00" + bytes(((i * 7 + 3) & 0xFF) for i in range(max(0, room - 8)))
        out += chunk(b"tEXt", text)
    out += chunk(b"IEND", b"")
    return out


class Server:
    def __init__(self, sock_path, mode):
        self.mode = mode
        self.sock_path = sock_path
        self.atoms = {ATOM_ATOM: b"ATOM", ATOM_STRING: b"STRING"}
        self.atom_ids = {}
        self.next_atom = 0x100
        self.props = {}          # (window, property) -> (type, fmt, bytes)
        self.window = 0
        self.requestor = 0
        self.seq = 0
        self.want_atom = {}
        # INCR 状态：目标属性 -> 剩余数据
        self.incr_left = {}
        self.payload = self.build_payload()
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        self.srv.bind(sock_path)
        self.srv.listen(4)

    def build_payload(self):
        if self.mode in ("inline", "incr"):
            n = 4096 if self.mode == "inline" else 300 * 1024
            return make_png(n)
        if self.mode == "text":
            return "HELLO-FROM-FAKE-X11".encode()
        return b""

    def atom(self, name):
        if isinstance(name, bytes):
            name = name.decode()
        if name in self.atom_ids:
            return self.atom_ids[name]
        aid = self.next_atom
        self.next_atom += 1
        self.atom_ids[name] = aid
        self.atoms[aid] = name.encode()
        return aid

    def atom_name(self, aid):
        return self.atoms.get(aid, b"").decode("latin1")

    def targets(self):
        out = [self.atom("TARGETS")]
        if self.mode in ("inline", "incr"):
            out.append(self.atom("image/png"))
        else:
            out += [self.atom("UTF8_STRING"), self.atom("text/plain")]
        return out

    # ---- 应答 ----
    def reply_fmt(self, conn, seq, fmt, body, extra=b""):
        head = struct.pack("<BBHI", EV_REPLY, fmt, seq, len(extra) // 4)
        conn.sendall(head + body.ljust(24, b"\x00")[:24] + extra)

    def reply(self, conn, seq, body, extra=b""):
        # 应答头是 **8** 字节：type、data、sequence(2)、length(4，4 字节单位)。
        # body 里只放固定字段（从偏移 8 开始算），额外数据跟在头后面。
        # 头写成 4 字节的话，客户端会把你当长度的那几个字节解释成字段，
        # 症状是「应答收下了，但每个字段都是垃圾」。
        head = struct.pack("<BBHI", EV_REPLY, 0, seq, len(extra) // 4)
        assert len(head) == 8, len(head)
        conn.sendall(head + body.ljust(24, b"\x00")[:24] + extra)

    def send_event(self, conn, ev):
        # 事件 32 字节；SentEvent 送来的事件，type 带 0x80 位。
        pkt = bytes([ev[0] | SENT_EVENT_BIT]) + ev[1:]
        conn.sendall(pkt.ljust(32, b"\x00"))

    # ---- setup ----
    def handshake(self, conn):
        # setup 请求头**固定 12 字节**（不是 8）：order、pad、major(2)、minor(2)、
        # auth-name-len(2)、auth-data-len(2)、pad(2)。少读 4 字节，剩下的
        # 「长度字段 + 填充」会被当成下一个请求的头，之后**每一个**请求都错位一格，
        # 症状就是「握手成功、InternAtom 也拿到了 id、然后所有请求石沉大海」。
        head = self.readn(conn, 12)
        name_len = r16(head, 6)
        data_len = r16(head, 8)
        if name_len:
            self.readn(conn, name_len + pad4(name_len))
        if data_len:
            self.readn(conn, data_len + pad4(data_len))
        vendor = b"fake-x11"
        b = bytearray()
        b += u32(12101014)           # release
        b += u32(0x7200000)          # resource id base
        b += u32(0x1FFFFF)           # resource id mask
        b += u32(0)                  # motion buffer size
        b += u16(len(vendor))        # vendor length
        b += u16(65535)              # max request length（4 字节单位）
        b += bytes([1, 1])           # roots 数量、formats 数量
        b += bytes([0, 0])           # image byte order、bitmap bit order
        b += bytes([32, 32])         # scanline unit、scanline pad
        b += bytes([8, 255])         # min keycode、max keycode
        b += b"\x00" * 4             # 未使用
        b += vendor
        b += b"\x00" * pad4(len(vendor))
        # format 表（每项 8 字节）
        for depth, bpp, scan in ((1, 1, 32), (24, 32, 32)):
            b += bytes([depth, bpp, scan]) + b"\x00" * 5
        # 屏幕（40 字节）
        b += u32(0x1E6)              # root
        b += u32(0x20)               # default colormap
        b += u32(0xFFFFFF)           # white pixel
        b += u32(0x000000)           # black pixel
        b += u32(0)                  # current input masks
        b += u16(1200) + u16(800)    # 像素宽高
        b += u16(300) + u16(200)     # 毫米宽高
        b += u16(1) + u16(1)         # min/max installed maps
        b += u32(0x21)               # root visual
        b += bytes([0, 0, 24, 0])    # backing store、save unders、root depth、允许的深度数
        # 长度字段必须以 4 字节为单位，且要如实反映**实际**写出的字节数：
        # 写错会让客户端读多/读少，症状是「握手看起来成功，之后全错位」。
        assert len(b) % 4 == 0, len(b)
        # 8 字节头：success、unused、major(2)、minor(2)、additional_length(2，4 字节单位)。
        # major/minor 各占 **2** 字节 —— 少写一个字节，头就只有 6 字节，
        # 后面 release 的前两字节会被当成长度，客户端读到天文数字然后超时。
        head = struct.pack("<BBHHH", 1, 0, 11, 0, len(b) // 4)
        assert len(head) == 8, len(head)
        conn.sendall(head + bytes(b))

    def readn(self, conn, n):
        out = b""
        while len(out) < n:
            c = conn.recv(n - len(out))
            if not c:
                raise EOFError
            out += c
        return out

    def set_prop(self, window, prop, typ, fmt, data, notify=False):
        self.props[(window, prop)] = (typ, fmt, data)
        if notify:
            ev = bytes([EV_PROP_NOTIFY, 0]) + u16(0) + u32(window) + u32(prop) \
                + u32(0) + bytes([0])   # state = NewValue
            return ev
        return None

    # ---- 主循环 ----
    def serve(self, conn, seconds):
        self.handshake(conn)
        end = time.time() + seconds
        conn.settimeout(max(0.5, seconds))
        while time.time() < end:
            try:
                head = self.readn(conn, 4)
            except (EOFError, socket.timeout):
                return
            opcode = head[0]
            length = r16(head, 2)
            rest = self.readn(conn, length * 4 - 4) if length > 1 else b""
            self.seq += 1
            seq = self.seq
            if opcode == OP_CREATE_WINDOW:
                self.window = r32(rest, 0)
            elif opcode == OP_CHANGE_ATTR:
                pass
            elif opcode == OP_INTERN:
                n = r16(rest, 0)
                name = rest[4:4 + n].decode("latin1")
                self.reply(conn, seq, u32(self.atom(name)))   # atom @8
            elif opcode == OP_GETSELOWN:
                owner = 0 if self.mode == "empty" else 0x1E6
                self.reply(conn, seq, u32(owner))             # owner @8
            elif opcode == OP_CONVERTSEL:
                self.on_convert(conn, rest)
            elif opcode == OP_GETPROP:
                self.on_getprop(conn, seq, rest)
            elif opcode == OP_SETSELOWN:
                pass
            else:
                pass

    def on_convert(self, conn, rest):
        requestor = r32(rest, 0)
        selection = r32(rest, 4)
        target = r32(rest, 8)
        prop = r32(rest, 12)
        tm = r32(rest, 16)
        self.requestor = requestor
        tname = self.atom_name(target)
        if tname == "TARGETS":
            ids = self.targets()
            data = b"".join(u32(a) for a in ids)
            self.set_prop(requestor, prop, ATOM_ATOM, 32, data)
            self.notify(conn, tm, requestor, selection, target, prop)
            return
        if tname == "image/png" and self.mode in ("inline", "incr"):
            if self.mode == "inline":
                self.set_prop(requestor, prop, self.atom("image/png"), 8, self.payload)
            else:
                # INCR：先放一个 32 位「总长度」标记，随后逐段写入。
                self.incr_left[prop] = self.payload
                self.set_prop(requestor, prop, self.atom("INCR"), 32, u32(len(self.payload)))
            self.notify(conn, tm, requestor, selection, target, prop)
            return
        if tname in ("UTF8_STRING", "text/plain") and self.mode == "text":
            self.set_prop(requestor, prop, self.atom("UTF8_STRING"), 8, self.payload)
            self.notify(conn, tm, requestor, selection, target, prop)
            return
        # 拒绝：property = 0
        self.notify(conn, tm, requestor, selection, target, 0)

    def notify(self, conn, tm, requestor, selection, target, prop):
        ev = struct.pack("<BBH", EV_SEL_NOTIFY, 0, 0) + u32(tm) + u32(requestor) \
            + u32(selection) + u32(target) + u32(prop)
        self.send_event(conn, ev)

    def on_getprop(self, conn, seq, rest):
        window = r32(rest, 0)
        prop = r32(rest, 4)
        delete = True
        if os.environ.get("FAKE_X11_DEBUG"):
            typ_d, fmt_d, data_d = self.props.get((window, prop), (0, 0, b""))
            print("GETPROP prop=%d type=%s fmt=%d len=%d incr_left=%d"
                  % (prop, self.atom_name(typ_d), fmt_d, len(data_d),
                     len(self.incr_left.get(prop, b""))), file=sys.stderr, flush=True)
        key = (window, prop)
        typ, fmt, data = self.props.get(key, (0, 0, b""))
        if key in self.props and delete:
            del self.props[key]
            # INCR 的推进方式：客户端把属性**取走**（delete=true）之后，所有者才写下一段。
            # 注意顺序：先写「下一段数据」，等下一轮再写「零长结束标记」。
            # 在同一轮里先写数据再写零长标记，会把**最后一段**直接覆盖掉 ——
            # 症状正是「大文件被截断，且刚好少一段」。
            if prop in self.incr_left:
                left = self.incr_left[prop]
                if len(left) > 0:
                    chunk = left[:65536]
                    self.incr_left[prop] = left[65536:]
                    self.set_prop(window, prop, self.atom("image/png"), 8, chunk)
                else:
                    del self.incr_left[prop]
                    self.set_prop(window, prop, self.atom("image/png"), 8, b"")
                ev = bytes([EV_PROP_NOTIFY, 0]) + u16(0) + u32(window) + u32(prop) \
                    + u32(0) + bytes([0])
                # 通知必须排在应答之后（客户端先读应答，再等通知）
                self.pending_ev = ev
        nitems = len(data) * 8 // fmt if fmt else 0
        # GetProperty 应答：type @8、bytes_after @12、nitems @16；format 在 @1。
        body = u32(typ) + u32(0) + u32(nitems) + b"\x00" * 8
        extra = data + b"\x00" * pad4(len(data))
        self.reply_fmt(conn, seq, fmt, body, extra)
        ev = getattr(self, "pending_ev", None)
        if ev is not None:
            self.pending_ev = None
            self.send_event(conn, ev)


def main():
    if len(sys.argv) < 3:
        print("用法: fake_x11.py <socket> <inline|incr|empty|text> [秒数]", file=sys.stderr)
        return 2
    sock_path, mode = sys.argv[1], sys.argv[2]
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 30
    srv = Server(sock_path, mode)
    print("READY %s" % sock_path, flush=True)
    conn, _ = srv.srv.accept()
    try:
        srv.serve(conn, seconds)
    except (EOFError, ConnectionResetError, socket.timeout):
        pass
    finally:
        conn.close()
        srv.srv.close()
        if os.path.exists(sock_path):
            os.unlink(sock_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
