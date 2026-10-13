# uya-term

纯 [Uya](https://github.com/wyf0912/uya) 实现的终端工具集。当前阶段交付 **`uya-ssh`**：
一个用本机伪终端（PTY）包住 `ssh`（或任意程序）的包装器，并在本地实现 **OSC 5522 / OSC 52**
剪贴板协议，使远端 TUI 应用能够**粘贴图片**——即使本机终端（如 xterm、VTE 系）并不支持该协议。

## 状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 项目骨架、Makefile、CLI 解析 | ✅ |
| M1 | PTY 透传：raw 模式、窗口尺寸同步、信号/退出码透传 | 🚧 |
| M2 | OSC 扫描器 + 剪贴板仓库 + `clip` 子命令 | ⏳ |
| M3 | OSC 5522 服务端模式（粘贴事件、一次性口令、read/write/walias） | ⏳ |
| M4 | OSC 5522 代理模式（本地终端支持时）+ OSC 52 透传 | ⏳ |
| P2 | 纯 Uya 的 SSH-2 协议客户端（curve25519/ed25519/bigint 等） | 规划中 |

## 快速开始

```sh
make build          # 编译到 bin/uya-ssh
bin/uya-ssh --help
bin/uya-ssh user@host          # 等价于 ssh user@host，但启用了图片粘贴
alias ssh="$PWD/bin/uya-ssh"   # 也可以直接替换 ssh
```

验收命令：

```sh
make check          # 类型检查 + 单元测试 + 集成测试（伪终端端到端）
```

## 为什么需要包装层

终端里的程序（例如运行在远端 TUI 中的编码助手）想把剪贴板里的图片读进来，需要终端支持
[OSC 5522](https://sw.kovidgoyal.net/kitty/clipboard/) 协议。但绝大多数终端（xterm、GNOME
Terminal、Konsole、Deepin Terminal…）只支持纯文本的 OSC 52，图片无从谈起。

`uya-ssh` 把 `ssh` 放进本机 PTY，接管它和真实终端之间的字节流，于是：

- 应用发出的 OSC 5522 请求被 `uya-ssh` 截获，由**本地**的剪贴板仓库应答；
- 用户按前缀键即可触发一次"粘贴事件"，应用据此取回 PNG/JPEG 原始字节；
- 本机终端若本身支持 5522（kitty、ghostty 等），`uya-ssh` 改为透明代理，把请求转给真实终端。

## 前缀键

默认前缀为 `Ctrl-]`（可用 `--prefix=^a` 修改）。前缀后接：

| 键 | 行为 |
|---|---|
| `p` | 触发一次粘贴事件（5522 模式）；终端不支持时退化为注入文本 |
| `t` | 以文本形式粘贴当前剪贴项 |
| `l` | 列出本地剪贴板仓库 |
| `q` | 断开连接 |
| `Ctrl-]` | 发送字面前缀字节 |

## 剪贴板仓库

图片与文本存放在 `~/.cache/uyassh/clip/`，`current` 指针表示"当前剪贴项"：

```sh
bin/uya-ssh clip add shot.png    # 放入仓库并设为当前项
bin/uya-ssh clip list
bin/uya-ssh clip use 2
bin/uya-ssh clip clear
```

## 依赖与构建

- 编译器：`~/uya/uya/bin/uya`（可用 `make UYA=/path/to/uya` 覆盖）。
- 标准库：`UYA_ROOT` 默认由编译器自带；仓库本身不复制标准库源码。
- 纯 Uya，无第三方依赖；不使用 `fork`（改用 `posix_spawn`，见下）。

## 实现说明

- **不用 `fork()`**：本机实测 Uya 运行时的 `fork` 路径存在偶发子进程启动失败，因此统一走
  `posix_spawnp` + `posix_spawn_file_actions_*`（把子进程的 0/1/2 直接接到 PTY slave），
  并对 spawn 失败给出明确报错。
- **`@print` 的坑**：编译器对 `&const byte` 变量会生成 `%lld`，因此动态字符串统一经
  `util/out.uya`（直接写 fd 1/2）输出。
- **原始模式**：进入前保存 `struct termios`（本机 60 字节），退出与信号路径都会恢复。
- **窗口尺寸**：`SIGWINCH` → `TIOCGWINSZ` → `TIOCSWINSZ` 写 PTY master，子进程的
  `stty size` 立刻可见。

## 已知限制

- 目前只做终端透传与剪贴板协议，SSH 协议本身仍由系统 `ssh` 完成。
- 需要 Linux（termios/TIOCGWINSZ/PTY 走 libc）。macOS 分支未验证。
- 全屏程序的交互行为依赖真实终端，自动化测试只断言关键序列。

## 许可证

与上游 Uya 项目一致（MIT）。
