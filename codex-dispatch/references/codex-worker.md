# codex-worker 参考

`codex-worker` 把一次派活或审查包装成一条命令：固定沙箱与审批策略，只在 git 链接工作树里运行，把 Codex 的事件流整理成精简输出。脚本在 `../scripts/`，安装见 SKILL.md。

## 用法

```bash
codex-worker <工作树> "任务"                         # 新会话
codex-worker <工作树> @task.md                       # 任务从 UTF-8 文件读（长任务推荐）
echo "任务" | codex-worker <工作树> -                 # 任务从 stdin 读
codex-worker --resume <会话ID> <工作树> @more.md      # 续跑已有会话
codex-worker --review <工作树> <基准分支或提交>        # 通用审查
codex-worker --review <工作树> <基准> @angle.md       # 按指定角度审查
```

- `<工作树>` 必须是 `git worktree add` 建出的**链接工作树的根目录**。传主检出（`.git` 是目录）或子目录会被拒绝：主检出里 Codex 能写到 `.git`，提交就拦不住了。确需在主检出运行时设 `CODEX_WORKER_ALLOW_MAIN=1`，不推荐。
- 任务里含引号、以 `/` 开头或比较长时，用 `@文件` 或 `-`，直接写在命令行里容易被 shell 转义改坏。
- 要单独审一个已经合并的提交：建一个停在该提交的分离工作树（`git worktree add --detach <路径> <提交>`），基准传 `<提交>~1`。

## 输出

开头两行：模式、工作树、模型，以及当前登录的 Codex 账号（邮箱打码加套餐，例如 `ab***@gmail.com（plus）`）。切换账号后看这一行确认是否生效。

过程中：`[codex] …` 是 Codex 的说明，`[cmd exit=N] …` 是它执行的命令，`[file …] …` 是它改动的文件，`[error] …` 是报错。

结束摘要依次是：退出码与用时、token 用量、账号、错误、最终答复、工作树 `git status`、完整日志路径，**最后一行固定是 `会话ID: <id>`**。

| 退出码 | 含义 |
|---|---|
| 0 | 正常结束 |
| 2 | 参数错误，或因工作树、基准不合格拒绝运行 |
| 75 | Codex 额度用尽；摘要里有「Codex 额度用尽：恢复时间 …」 |
| 其他 | Codex 自身的退出码；摘要里会附 stderr 末尾 |

审查模式（`codex exec review`）上报的 token 用量是全 0，摘要显示「未上报」，实际照样消耗额度。

## 固定行为

- 派活用 `workspace-write` 沙箱：只能写工作树和临时目录，命令没有网络。审批策略 `never`，不会卡在等待确认上。审查用 `read-only` 沙箱，不编译、不跑测试。
- 新会话的任务前面会自动加一段约束前言：非交互运行、不要提问；只写工作树；不要尝试提交和联网；完成后用中文写交付说明。
- 项目说明加载上限调到 128KB。Codex 默认只加载 AGENTS.md 的前 32KB，大项目的 AGENTS.md 常超过这个长度。
- 带审查角度时不走 `codex exec review`：它的 `--base` 不能和自定义指令同时用，会直接报错。这时改用普通 `codex exec` 加只读沙箱和一段审查前言，审查范围是 `git diff <基准>...HEAD`。
- 完整事件流写到 `~/.codex-worker/logs/<时间>.jsonl`，stderr 写到同名 `.stderr.txt`。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `CODEX_EXE` | Codex CLI 可执行文件路径 | 自动查找，见「平台差异」 |
| `CODEX_WORKER_MODEL` / `CODEX_WORKER_EFFORT` | 派活与续跑的模型和思考强度 | `gpt-6.2-sol` / `max` |
| `CODEX_WORKER_REVIEW_MODEL` / `CODEX_WORKER_REVIEW_EFFORT` | 审查的模型和思考强度，与上一组互不影响 | `gpt-6.2-astra` / `xhigh` |
| `CODEX_WORKER_M2_REPO` | 直接指定给 Codex 用的 Maven 仓库（Windows），设了就不同步副本 | — |
| `CODEX_WORKER_M2_MIRROR` | Maven 仓库副本位置（Windows），必须在用户主目录之外 | 有 D 盘用 `D:\codex-m2\repository`，否则 `%SystemDrive%\codex-m2\repository` |
| `CODEX_WORKER_ALLOW_MAIN` | 设为 `1` 时允许在主检出运行 | — |
| `CODEX_WORKER_BIN` | `install.sh` 的安装目录（macOS / Linux） | `~/.local/bin` |

## 平台差异

| | Windows | macOS / Linux |
|---|---|---|
| 找 Codex CLI | `CODEX_EXE` → 桌面应用包里的 `codex.exe`。不回退到 PATH：旧版 Python 在 Windows 上找命令会先查当前目录，可能在沙箱外执行同名程序 | `CODEX_EXE` → PATH 上的 `codex` → `/Applications` 或 `~/Applications` 下的 `Codex.app/Contents/Resources/codex` |
| 代理环境变量 | 启动前清掉 `http(s)_proxy` / `all_proxy` | 保留 |
| Maven 项目 | 同步 `~/.m2` 副本到主目录之外、设 `MAVEN_OPTS`；Java 临时目录指到 `target/codex-tmp` | 不做额外设置，只提示 Codex 用 `mvn -o` |
| 命令入口 | Git Bash 用 `codex-worker`，cmd / PowerShell 用 `codex-worker.cmd` | `codex-worker`（bash） |

macOS 与 Linux 分支只经过单测模拟，没有在真机上跑过，所以接入时一定要做 SKILL.md 里的冒烟自检。

## Windows 上踩过的坑

这些问题都实测过，脚本已经处理，记下来方便排查：

- **本地代理导致 UAC 弹窗、shell 命令全部失败。** shell 里设了 `http_proxy=127.0.0.1:<端口>` 时，Codex 认为离线沙箱的防火墙规则要更新，会拉起提权程序弹 UAC；没人点就报 1223，之后所有 shell 命令失败。同一个代理还会让 WebSocket 握手失败、长时间重连。脚本启动前会清掉代理变量。排查看 `~/.codex/.sandbox/sandbox.<日期>.log`，特征是 `offline firewall settings changed`。
- **沙箱用另一个 Windows 账户执行命令**，它的 `~` 不是你的，`~/.m2` 是空的，离线编译连父 POM 都找不到。
- **沙箱对用户主目录下的路径调 `toRealPath` 会被拒绝。** javac 关闭多版本 jar（如 `okio-jvm`）时正要调它，于是编译报 `AccessDeniedException`。所以 Maven 仓库要用主目录之外的副本；给主目录补 ACL、让 javac 分叉都不管用。
- **JUnit `@TempDir` 默认建在你的 `%TEMP%` 下**，沙箱账户删不掉这一层目录，测试收尾报 `AccessDeniedException`。所以 Java 临时目录指到工作树里的 `target/codex-tmp`；它必须在工作树之内，指到外面的目录也删不掉。代价是 `mvn clean` 会删掉它，约束前言里提醒了 Codex 重建。
- **Codex CLI 不在 PATH 上**，桌面应用包的安装路径随版本变化，脚本每次都重新查找。不要用 `~/.codex/.sandbox-bin/codex.exe`，那是沙箱辅助程序的副本，版本会落后。

## 已知限制

- 沙箱不能联网：新依赖要派活方先下载，Codex 才能离线构建。
- 强行结束 `codex-worker` 进程（而不是 Ctrl-C）时，Codex 子进程可能不会跟着退出。
- 免费账号不支持 `gpt-6.2-astra`，审查会直接报错；改用 `CODEX_WORKER_REVIEW_MODEL` 指定别的模型。
- macOS / Linux 上日志按默认 umask 创建，通常是 0644。多用户机器上，能进入你主目录的其他用户可以读到命令输出和模型答复；需要时自行收紧 `~/.codex-worker/` 的权限。
- `install.sh` 要直接运行，不要通过指向它的符号链接运行；从只读挂载的仓库安装、或系统的 `ln` 不支持 `-n` 时，安装会失败。
