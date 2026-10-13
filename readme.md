# uya-term

纯 [Uya](https://github.com/wyf0912/uya) 实现的终端工具集。当前交付 **`uya-ssh`**：
一个用本机伪终端（PTY）包住 `ssh`（或任意程序）的包装器，并在本地实现
**OSC 5522 / OSC 52** 剪贴板协议，使远端 TUI 应用能够**粘贴图片**——
即使本机终端（xterm、GNOME Terminal、Konsole…）并不支持该协议。

## 快速开始

```sh
make build                       # 编译到 bin/uya-ssh
bin/uya-ssh --help
bin/uya-ssh user@host            # 等价于 ssh user@host，但启用了图片粘贴
alias ssh="$PWD/bin/uya-ssh"     # 也可以直接替换 ssh
make install                     # 安装到 ~/.local/bin/uya-ssh
```

验收命令：

```sh
make check      # 类型检查 + 单元测试 + 集成测试（伪终端端到端，含真实 sshd）
```

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
- 用户按前缀键触发"粘贴事件"，应用据此取回 PNG/JPEG 原始字节；
- 本机终端若本身支持 5522（kitty、ghostty、wezterm），改走**代理模式**，
  由 `uya-ssh` 反向向终端索取剪贴板再转交应用。

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
verbose = no
```

命令行选项（`--no-pty`、`--pty`、`--program=`、`--prefix=`、`-v`、`--version`、`-h`）
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

## 测试

```
tests/unit/        协议编解码与状态机（uya test）
  test_b64.uya        严格 Base64（RFC 4648 向量 + 非法输入拒绝）
  test_osc_scan.uya   OSC/CSI 增量扫描（跨分片、终止符、超长丢弃）
  test_clip_store.uya 剪贴板仓库（往返、MIME 嗅探、当前项切换）
  test_osc5522.uya    5522 状态机（事件+口令读回、write 落盘、EINVAL）

tests/integration/ 伪终端端到端（python 驱动）
  t01 透传/尺寸/退出码    t02 运行中改尺寸      t03 raw 模式恢复
  t04 参数逐字透传        t05 临时 sshd 真实 ssh 端到端
  t06 5522 闭环（图片原样读回）
  t07 真实 ssh 会话中粘贴图片
  t08 OSC 52 透传 + DECRPM 应答不泄漏屏幕
  t09 代理模式（假 5522 终端）
  t10 配置文件前缀键生效
```

## 状态与路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 骨架、CLI | ✅ |
| M1 | PTY 透传：raw 模式、尺寸同步、退出码 | ✅ |
| M2 | OSC 扫描器、剪贴板仓库、`clip` 子命令 | ✅ |
| M3 | OSC 5522 服务端（粘贴事件、口令、读写） | ✅ |
| M4 | 代理模式 + OSC 52 透传 | ✅ |
| M5 | 配置、文档、测试收口 | ✅ 已交付 |
| P2 | 纯 Uya 的 SSH-2 协议客户端（curve25519/ed25519/bigint…） | 规划中 |

## 已知限制

- SSH 协议本身仍由系统 `ssh` 完成；纯 Uya 的协议实现见路线图 P2。
- 仅 Linux 已验证（termios/PTY 走 libc）；macOS 分支未测试。
- 剪贴板仓库为单机本地存储，未实现 X11/Wayland 剪贴板对接（代理模式下由终端提供）。
- 全屏程序交互依赖真实终端，自动化测试只断言关键序列。

## 许可证

与上游 Uya 项目一致（MIT）。
