# uya-term

纯 [Uya](https://github.com/wyf0912/uya) 实现的终端工具集。当前交付 **`uya-ssh`**：
一个用本机伪终端（PTY）包住 `ssh`（或任意程序）的包装器，并在本地实现
**OSC 5522 / OSC 52** 剪贴板协议，使远端 TUI 应用能够**粘贴图片**——
即使本机终端（xterm、GNOME Terminal、Konsole…）并不支持该协议。

## 快速开始

```sh
make build                       # 编译到 bin/uya-ssh（默认静态、零依赖）
bin/uya-ssh --help
bin/uya-ssh user@host            # 等价于 ssh user@host，但启用了图片粘贴
alias ssh="$PWD/bin/uya-ssh"     # 也可以直接替换 ssh
make install                     # 安装到 ~/.local/bin/uya-ssh
```

验收命令：

```sh
make check      # 类型检查 + 单元测试 + 集成测试（伪终端端到端，含真实 sshd）
```

### 零依赖静态产物

`make build` 默认产出**静态链接**的单个可执行文件：没有 ELF 解释器（不依赖 `ld.so`）、
动态段里没有任何 `NEEDED` 项（不依赖 `libc.so`），也没有未定义符号。
因此 `bin/uya-ssh` 可以 `scp` 到任意同架构的 Linux 机器上直接运行——包括
`env -i` 之类的空环境、没有 glibc 的容器镜像、chroot 或 rescue 环境：

```sh
file bin/uya-ssh          # => statically linked
ldd bin/uya-ssh           # => 不是动态可执行文件
readelf -d bin/uya-ssh    # => There is no dynamic section in this file
env -i ./bin/uya-ssh --version
```

链接开关是 `STATIC`（默认 `1`）：`LDFLAGS=-static` 会透传给 `uya build` 内部的链接步骤。
宿主机缺少静态 libc（`libc.a`；例如只装了裁剪过的 `libc6-dev` 或部分 musl 工具链）时
构建会失败，此时用 `make build STATIC=0` 回退动态链接；集成测试 `t11` 在 `STATIC=0`
下自动跳过。

注意：静态只是**本程序**零依赖。会话里的 `ssh` 仍是系统 `ssh`，它自身可能是动态链接的，
需要在目标机上有对应的 libc。

## 为什么需要包装层

终端里的程序（例如跑在远端 TUI 里的编码助手）要读剪贴板里的图片，需要终端支持
[OSC 5522](https://sw.kovidgoyal.net/kitty/clipboard/)。但绝大多数终端只支持纯文本的
OSC 52，图片无从谈起。

`uya-ssh` 把 `ssh` 放进本机 PTY，接管它与真实终端之间的字节流：

```
        真实终端  ←────────  uya-ssh  ────────→  ssh ──→ 远端 TUI 应用
   (xterm/kitty…)          ↑ 扫描 OSC                ↑ 5522 请求/应答
                    5522 服务端 + 剪贴板仓库
```

- 应用发出的 OSC 5522 请求被本层截获，由**本地**剪贴板仓库应答；
  仓库为空时，`uya-ssh` 自己走 X11 协议去读**本机的**剪贴板（见下）；
- 用户按前缀键触发"粘贴事件"，应用据此取回 PNG/JPEG 原始字节；
- 本机终端若本身支持 5522（kitty、ghostty、wezterm），改走**代理模式**，
  由 `uya-ssh` 反向向终端索取剪贴板再转交应用。

## 不支持 5522 的终端也能贴图

这是最要紧的一条：**终端不支持 OSC 5522 并不等于贴不了图**。

远端应用问"剪贴板里有什么"，请求会一路传到本机；此时若仓库是空的，
`uya-ssh` 就自己按 X11 协议去读**本机**剪贴板（`$DISPLAY` 指向的那台 X 服务器），
把图片字节取回来再交给远端应用。于是 deepin-terminal、xterm、GNOME Terminal
这些只支持 OSC 52 的终端同样能贴图——**不需要装 `xclip`/`xsel`/`wl-paste`**，
也不需要为此换终端。

```sh
bin/uya-ssh clip fetch     # 手动把本机剪贴板收进仓库（会话中也会自动发生）
bin/uya-ssh clip list      # 确认读到了什么
```

实现要点（`src/uyassh/x11/clip.uya`，手写 X11 协议的最小读取路径，纯 Uya）：

- 只做「读」：`InternAtom` → `GetSelectionOwner` → `ConvertSelection(TARGETS)`
  → 按优先级挑一个 target（图片优先，其次文本）→ `GetProperty` 取数据；
- 支持 **INCR** 分段传输：大图（>64 KiB）由所有者分块推送，逐段收齐；
  任一段超时或超限都**明确失败**，绝不返回半张 PNG（半张图同样能过 MIME 嗅探，
  交给下游只会变成一次莫名其妙的「坏请求」）；
- 结果缓存 3 秒：远端应用通常先问 MIME 列表再取数据，避免重复连服务器；
- `DISPLAY` 指向远程主机（`host:0`）时直接放弃——那读的是**别的机器**的剪贴板，
  不是用户眼前的。

## Ctrl-V：先入库，再交给 ssh

上面那条是**拉模式**：应用问了，我们才去读剪贴板。问题是那次读取落在**应用的读超时
窗口里**——远端应用等不了十几秒，而 X11 取一张大图（握手 + GetProperty + INCR 分段）
恰恰可能要那么久；而且读到的图只活在内存里，应用换个时机再问就没了。

所以按 `Ctrl-V` 时改成**推前置**：

```
用户按 Ctrl-V ──→ uya-ssh 先把本机剪贴板的图片收进仓库（成为当前项）
              └─→ 再把 Ctrl-V 字节原样交给 ssh
                                              ↓
                        远端应用收到 Ctrl-V，随即自己发 5522 read
                                              ↓
                              直接从仓库命中（不再现读 X11）
```

- 停顿挪到了**用户刚按下键**这个预期位置，而不是应用的读超时窗口里；
- 图是**持久**的：`clip list` 看得见、`clip use` 能切回来，不再是一次性的内存缓存；
- 按键语义完全不变——Ctrl-V 照样原样转发，预取只是"顺手先把图准备好"。

三条不变量（都有测试盯着，见 t13）：

| 不变量 | 为什么 |
|---|---|
| **永不吞键** | 预取成功/失败/跳过，Ctrl-V 都必须原样到达子进程。吃掉按键比贴不上图严重得多。 |
| **失败静默** | 屏幕上不许多打一个字：远端 TUI 的画面归它自己，插一行提示就毁一屏。诊断走 `-v`。 |
| **有界** | 去抖 250ms + X11 预算 3s + 仓库条目护栏；连按 Ctrl-V 不会把终端拖成几十秒的卡顿。 |

其它口径：

- **只收图片**：板子里是文本时直接跳过（文本有 OSC 52 那条现成的路），且判断发生在
  取数据**之前**——拿到 TARGETS 列表就知道该不该继续，不为用不上的 target 白读一遍。
- **去重**：与当前项大小、字节都相同就只切指针、不新建条目，否则每按一次就是一份新的几 MiB。
- **代理模式不预取**：那种终端（kitty/ghostty）的剪贴板由终端持有，通向它的请求是异步的，
  做不到"入库完成后再转发"这个同步顺序；那些用户仍有现成的 `Ctrl-] p` 路径。
- **前缀优先**：`--prefix=^v` 时 Ctrl-V 是前缀键，预取让路（用户的选择优先）。
- 默认开，可关：`--ctrl-v-import=no` 或配置 `ctrl-v-import = no`；命令行优先于配置文件。

拉模式那条**仍然保留**作为安全网：预取没跑（比如 Wayland 无 X11）或失败时，
应用问了照样能读到。

## 前缀键

默认前缀 `Ctrl-]`（`--prefix=^a` 或配置文件可改）。前缀后接：

| 键 | 行为 |
|---|---|
| `p` | 触发一次粘贴：应用支持 5522 时推送粘贴事件（带一次性口令），否则注入文本 |
| `t` | 以文本形式把当前剪贴项写入会话 |
| `l` | 在本地列出剪贴板仓库 |
| `q` | 结束会话 |
| `Ctrl-]` | 发送字面前缀字节 |

典型用法：远端应用启用 OSC 5522 粘贴模式后，按 `Ctrl-] p`，应用就能读到本机图片。

## 剪贴板仓库

图片与文本存放在 `~/.cache/uyassh/clip/`，`current` 指针表示当前剪贴项：

```sh
bin/uya-ssh clip add shot.png    # 放入仓库并设为当前项
printf 'hi' | bin/uya-ssh clip add    # 也可以从 stdin 读（文本粘贴）
bin/uya-ssh clip fetch           # 从本机 X11 剪贴板取回内容并设为当前项
bin/uya-ssh clip list
bin/uya-ssh clip use 2
bin/uya-ssh clip mime            # 打印当前项 MIME
bin/uya-ssh clip get > out.png   # 二进制安全地导出当前项
bin/uya-ssh clip clear
```

MIME 由内容魔数嗅探（PNG/JPEG/GIF/WebP/BMP/文本），不依赖外部命令。

## 配置

`$XDG_CONFIG_HOME/uyassh/config` 或 `~/.config/uyassh/config`：

```ini
# 本地前缀键：单字符、^X 或 C-x
prefix = ^]
# 剪贴板仓库目录（默认 ~/.cache/uyassh/clip）
clip-dir = ~/.cache/uyassh/clip
# 即使本地终端支持 5522 也自己扮演终端
force-serve = no
# Ctrl-V 时先把本机剪贴板图片收进仓库（默认开；命令行 --ctrl-v-import 优先）
ctrl-v-import = yes
verbose = no
```

命令行选项（`--no-pty`、`--pty`、`--program=`、`--prefix=`、`--ctrl-v-import=`、`-v`、`--version`、`-h`）
优先级高于配置文件；其余参数原样转交给子进程（不做 shell 拼接，逐字送达）。

## 协议实现

| 方向 | 报文 | 状态 |
|---|---|---|
| 应用 → 终端 | `type=read`（含 `loc=primary`、`pw`、`name`） | ✅ |
| 终端 → 应用 | `status=OK/DONE/DATA`（分块 ≤4096 字节，严格 base64） | ✅ |
| 应用 → 终端 | `type=write` / `wdata` / `walias` | ✅ 落进本地仓库 |
| 终端 → 应用 | `write:status=DONE/EINVAL/ENOSYS/EPERM/EBUSY` | ✅ |
| 能力查询 | `CSI ? 5522 $ p` → `CSI ? 5522 ; Ps $ y` | ✅ |
| 粘贴事件模式 | `CSI ? 5522 h/l`，未请求的 `OK`+`DATA:mime=Lg==`+`DONE` 三段通知 | ✅ |
| 一次性口令 | 事件携带 `pw`；使用时须带 `name`，用后失效 | ✅ |
| OSC 52 | 文本剪贴板，原样透传给本地终端 | ✅ |

## 实现说明（踩过的坑）

- **不用 `fork()`**：本机实测 Uya 运行时的 `fork` 路径存在偶发子进程启动失败，
  因此统一走 `posix_spawnp` + `posix_spawn_file_actions_*`，把子进程 0/1/2 直接接到
  PTY slave，并用 `addclose` 关掉 master 防止泄漏。
- **不用 `ptsname`**：split-C 后端把"返回指针的 extern"生成成返回 `int32` 的声明
  （指针被 `cltq` 截断，实测直接段错误），改用把结果写进调用方缓冲的 `ptsname_r`。
- **不用 `IAllocator` 接口取值**：接口对象经 `as &IAllocator` 后取到的指针无效，
  字节缓冲直接用 `malloc/realloc/free`。
- **`@print` 的坑**：编译器对 `&const byte` 变量生成 `%lld`，因此动态字符串统一经
  `util/out.uya` 直接写 fd。
- **跨模块类型的局部变量**：split-C 后端不为"另一个模块里声明的 struct/enum 局部变量"
  生成类型定义，因此把这类构造下沉到定义它们的模块内部。
- **原始模式**：进入前保存 `struct termios`（本机 60 字节），退出与信号路径都会恢复。
- **窗口尺寸**：`SIGWINCH`/轮询 → `TIOCGWINSZ` → `TIOCSWINSZ` 写 PTY master，
  子进程 `stty size` 立刻可见。
- **静态链接**：`uya build` 的链接步骤走内部 `make`，需要 `-static` 时通过环境变量
  `LDFLAGS=-static` 透传（`--nostdlib` 路线冲突，见下）。
- **X11 读取的坑**（详见 `src/uyassh/x11/clip.uya` 文件头）：Xauthority 文件是**大端**
  而协议是小端；应答的额外长度在 **u32@4**（不是 u16@6）；`XSendEvent` 送来的事件
  type 带 **0x80** 位（`SelectionNotify` 是 159 而非 31）；INCR 只认
  `PropertyNewValue` 通知，且等应答期间必须**把事件排队**而不是丢掉——否则大图会被截断。
- **不用 `--nostdlib`**：该路线会 `-nostdlib -static -lgcc` 并禁用宿主头文件，
  实测本程序的 `WNOHANG` 等 libc 常量在无头文件时不可见，且 `posix_spawnp` 等
  glibc 扩展不会被隐式声明；因此走"照常用 libc，但整程序静态链接"的路线。
- **`--xxx=` 的值偏移要数清楚**：`"--ctrl-v-import="` 是 **16** 字节，值从下标 16 开始；
  写成 `a + 15` 就得到 `"=no"`，而 `parse_yes_no` 对认不出的写法**返回 fallback
  （true）**——于是 `--ctrl-v-import=no` 静默地**开着**。这个 bug 一句话就能说清，
  但只看代码很难发现（"关掉"这个动作看起来完全正常，只是没生效），必须靠
  "关掉之后断言仓库是空的"这种**否定型**用例才抓得住。
- **默认开的配置键不能直接赋值**：`config_truthy` 对"没写这个键"返回 false，
  用它去填一个**默认 true** 的键（`ctrl-v-import`）会把默认值覆盖成关。所以
  `Config` 里额外记一个 `*_set` 位，只有写得认得出才覆盖；`Options` 里同样记一位，
  否则"配置里没提这件事"会反过来把 `--ctrl-v-import=yes` 改掉（命令行必须优先）。
- **测试环境要清 `XDG_CONFIG_HOME`**：它优先于 `HOME`，而宿主（本机）确实设了它。
  不清的话，所有"用 HOME 里的配置"的用例都会去读宿主真实的
  `~/.config/uyassh/config`，断言会假绿或假红（t13 第一次跑就是这么被坑的，
  症状是"配置文件写了 no 却像是没生效"）。
- **顺序类行为要用时间戳证**：只断言"最终入过库"证明不了"先入库、再转发"——
  拉模式（等应用问了才读）同样能让"最终有图"成立。t13 让假 X 服务器与假应用各自
  打时间戳，断言 **X11 交付 < 子进程收到按键**；并已用 `--ctrl-v-import=no`
  做过**负向验证**：同一断言在拉模式下必然反转（反例跑得出来，断言才算有区分力）。

## 测试

```
tests/unit/        协议编解码与状态机（uya test）
  test_b64.uya        严格 Base64（RFC 4648 向量 + 非法输入拒绝）
  test_osc_scan.uya   OSC/CSI 增量扫描（跨分片、终止符、超长丢弃）
  test_clip_store.uya 剪贴板仓库（往返、MIME 嗅探、当前项切换）
  test_osc5522.uya    5522 状态机（事件+口令读回、write 落盘、EINVAL）
  test_x11_display.uya X11 前置解析（DISPLAY→套接字、字节序、XAUTHORITY 记录）
  test_clip_prefetch.uya Ctrl-V 预取（按键判定、去抖窗口边界、时钟回拨、开关）

tests/integration/ 伪终端端到端（python 驱动）
  t01 透传/尺寸/退出码    t02 运行中改尺寸      t03 raw 模式恢复
  t04 参数逐字透传        t05 临时 sshd 真实 ssh 端到端
  t06 5522 闭环（图片原样读回）
  t07 真实 ssh 会话中粘贴图片
  t08 OSC 52 透传 + DECRPM 应答不泄漏屏幕
  t09 代理模式（假 5522 终端）
  t10 配置文件前缀键生效
  t11 零依赖静态产物（无 INTERP/NEEDED、env -i 可运行、clip 往返、PTY 端到端）
  t12 本机 X11 剪贴板读取（文本 / 内联 PNG / 300 KiB INCR 大图 / 空板 / PTY 端到端）
  t13 Ctrl-V 先入库再转发（顺序时间戳、不吞键、前缀优先、开关、去重）
```

t12 用 `tests/integration/fake_x11.py`——一个只应答剪贴板协议的**假 X 服务器**。
为什么不连真 `:0`：真 CLIPBOARD 属于用户的桌面会话（dde-clipboard / klipper 正持有它），
测试去抢所有权既会读走用户自己的剪贴板内容，也会和桌面组件打架，而且不可重复；
假服务器还能稳定造出 >64 KiB 的图，把真机上难以复现的 **INCR 分段**路径测到。
t13 用同一个假服务器，另加一个"收到按键就打时间戳"的假远端应用，用两侧时间戳
证明**先入库、再转发**这个顺序。

## 状态与路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 骨架、CLI | ✅ |
| M1 | PTY 透传：raw 模式、尺寸同步、退出码 | ✅ |
| M2 | OSC 扫描器、剪贴板仓库、`clip` 子命令 | ✅ |
| M3 | OSC 5522 服务端（粘贴事件、口令、读写） | ✅ |
| M4 | 代理模式 + OSC 52 透传 | ✅ |
| M5 | 配置、文档、测试收口 | ✅ 已交付 |
| M6 | 零依赖静态构建（`-static`，无 INTERP/NEEDED） | ✅ 已交付 |
| M7 | 本机 X11 剪贴板读取（不支持 5522 的终端也能贴图） | ✅ 已交付 |
| M8 | Ctrl-V 先入库再转发（推前置，读取不再落在应用超时窗口里） | ✅ 已交付 |
| P2 | 纯 Uya 的 SSH-2 协议客户端（curve25519/ed25519/bigint…） | 规划中 |

## 已知限制

- SSH 协议本身仍由系统 `ssh` 完成；纯 Uya 的协议实现见路线图 P2。
- 仅 Linux 已验证（termios/PTY 走 libc）；macOS 分支未测试。
- 本机剪贴板读取目前只覆盖 **X11**（`$DISPLAY` + `XAUTHORITY`）；Wayland 原生会话下
  没有 XWayland 时读不到，仍需靠终端支持 5522 或外部 helper。
- X11 读取只认 `CLIPBOARD`（空则回退 `PRIMARY`），且只做一次性快照，不监听变更。
- Ctrl-V 预取只在 **serve 模式**做（本地终端不支持 5522 时）：代理模式的剪贴板由终端
  持有，通向它的是异步请求/应答，做不到"入库完成后再转发"这个同步顺序。
- Ctrl-V 预取只收**图片**；文本仍走 OSC 52 / 拉模式。仓库条目达到 64 条时预取跳过
  （`clip_list` 的读窗口上限），需要时用 `clip clear` 腾位置。
- 全屏程序交互依赖真实终端，自动化测试只断言关键序列。
- 静态产物只覆盖自身；被包装的 `ssh` 仍需目标机上有可用的 libc 与 PATH。

## 许可证

与上游 Uya 项目一致（MIT）。
