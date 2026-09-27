# claudex 架构

claudex 系统设计的现行事实。

## 项目定位

claudex 以专用 settings 启动 Claude Code，让它的四个模型档位（fable、opus、sonnet、haiku）各自落到用户选定的后端。后端经本机回环地址上的 CLIProxyAPI 网关接入，分两类：网关以 OAuth 反代的订阅通道（Codex、Antigravity），以及网关按 OpenAI 或 Anthropic 兼容格式转发的通用接口（OpenRouter 与任意兼容端点）。

能力边界：claudex 生成网关配置、管理网关进程与受管升级、为每次启动生成会话快照、提供状态栏适配器与后台刷新；它不实现协议转换（由网关完成），不带状态栏渲染器本身（交给用户配置的底层渲染器，没有时出一行简版），只支持 Linux x86_64 上的 Claude Code。

## 核心设计原则

- **单一配置**：用户只维护 `claudex.toml`、不含 key 的 `gateway.base.yaml` 与 `settings.base.json`；网关的来源段、key 与会话 settings 都由 claudex 派生。接入或移除一个来源只改一处，因为多处手写同一份事实迟早不一致。
- **只列要用的模型**：每个来源显式列出挑中的模型，没列的不进网关配置、不进模型选择器。订阅通道在网关里仍提供全部模型，挑选只在 claudex 一侧生效。
- **元数据自动取，出错即报**：context、档位与显示名按「显式配置、网关模型定义或 OpenRouter 目录、模型 id」的顺序取；网关定义每次启动在线取，取不到就报错退出，不做离线降级，也不做多候选的自动回退。问题应当在出现时就被看见并修掉，而不是被悄悄绕开。
- **不提醒过期**：没有复核日期与过期告警。只有真问题才报：引用的模型不在网关里、订阅模型不在网关定义里、slug 不在目录里、配置错误。
- **不可变快照**：每次启动生成按内容摘要命名的快照，会话只读启动时那一份；配置之后的改动不影响已开着的会话，不需要会话中途的一致性检查。
- **统一计价**：费用一律按 OpenRouter 目录的标价计，含缓存读写；非 OpenRouter 来源显示等价花费并标 ≈。不维护各来源自己的价格表。
- **凭据只在文件里**：key 只从 0600 的文件读，只写进 0600 的 `gateway.yaml`；不进命令行参数、日志、错误信息、快照与状态文件。本机对网关的请求不走环境代理。
- **结构化的状态栏契约**：适配器把 claudex 口径的信息放进状态栏 JSON 的 `claudex` 对象，交给底层渲染器；不解析、不改写渲染器的输出。

## 技术栈

- Python 3.14，只依赖 PyYAML（钉死版本），其余标准库。
- 启动器是 bash，因为它要在 `exec claude` 之前管理网关进程（flock、PID 身份核对、端口与健康检查），这部分只实现一份，Python 侧需要启停网关时也调它。
- 打包用 hatchling；启动器与 `claudex-client-key` 作为 shared-scripts 装进 uv tool 环境的 `bin/`，日常按 git tag 安装为 uv tool。
- 开发工具：pytest、ruff、basedpyright（standard 档），版本由 `uv.lock` 钉住。
- 不选：PyPI 发布；把状态栏渲染器并入本包；Python 实现的启动器。

## 模块

| 模块 | 职责 |
| --- | --- |
| `bin/claudex` | 解析启动参数、`gateway start` 与 `stop`、按启动次序调 Python 内部子命令、`exec claude`；其余子命令转给 `claudex.cli` |
| `bin/claudex-client-key` | 输出下游 key 一行，供派生 settings 的 `apiKeyHelper` 与其他本机工具调用 |
| `paths.py` | 配置根、state 根与全部文件路径，调用时求值 |
| `jsonio.py` | JSON 的原子写入与读取 |
| `config.py` | `claudex.toml` 的解析、校验与模型引用解析 |
| `gateway.py` | 生成与就地覆写 `gateway.yaml`、读 key、查询网关、等待模型注册 |
| `catalog.py` | OpenRouter 目录缓存、元数据取值、计价 |
| `render.py` | 会话快照与派生 settings、快照清理、preflight |
| `statusline.py` | 状态栏适配器 |
| `quota.py` | 后台刷新程序：订阅额度、OpenRouter 余额、目录与网关新版记录 |
| `upgrade.py` | 网关的受管升级与受管重启、发布变更摘要 |
| `probe.py` | 来源探测 |
| `cli.py` | 全部 Python 子命令与启动器用的内部子命令 |
| `templates/` | `init` 写出的起步文件与 bash 补全脚本 |

## 启动次序

启动会话，以及 `login`、`probe`、`upgrade`、`gateway start`、`gateway restart` 这些用到网关的入口，都按同一次序：

1. 生成 `gateway.yaml`：内容不变不重写；有变时先写临时文件并回读校验，再就地覆写原文件。网关对配置文件的监视挂在文件的 inode 上，替换文件会撤掉监视，就地覆写则触发它的热加载（有 150 毫秒去抖）。
2. 确保网关在跑：没在跑就以 `-config <state>/gateway.yaml` 拉起，环境里设 `WRITABLE_PATH=<state 目录>`，让网关日志与失败快照落在 state 的 `logs/`（网关没有日志目录的配置项）。
3. 这次改写了配置且网关原本就在跑时，轮询 `/v1/models`，直到本次配置里的全部通用来源模型都出现；等不到就报错并提示受管重启，不自动重启。
4. 启动会话时最后渲染快照：在线取订阅通道的模型定义与网关模型列表，生成 `sessions/<profile>-<摘要>.profile.json` 与 `.settings.json`，然后 `exec claude --settings <快照>`。

## 数据落点

- 配置根 `~/.config/claudex/`（`CLAUDEX_CONFIG_DIR`）：用户的三份配置、两枚本地 key、`keys/` 下的上游 key。
- state 根 `~/.local/state/claudex/`（`CLAUDEX_STATE_DIR`）：生成的 `gateway.yaml`、网关进程记录与启动锁、网关日志、会话快照、目录缓存、额度缓存与刷新记录、网关新版记录、受管升级记录与锁。
- 固定位置：OAuth 凭据 `~/.local/share/claudex/auth/`；网关二进制 `~/.local/bin/cli-proxy-api`，它是指向 `~/.local/lib/cliproxyapi/<版本>/` 的 symlink，由受管升级切换。
- 会话内的费用结算状态在 `$TMPDIR`。

逐文件的清单与权限见 README 第 11 节。

## 状态栏与后台刷新

状态栏适配器每次刷新时读会话快照（并刷新快照的修改时间，使运行中会话的快照不被清理），按响应结算费用，读额度缓存，改写状态栏 JSON 后交给底层渲染器；渲染器缺失或失败时输出内置简版。它按 60 秒节流、以不等待的方式抢 `refresh.lock` 拉起 `claudex.quota`；刷新程序整个运行期持有这把锁，所以同一时刻只有一个刷新在跑，`refresh.json` 的读改写不会互相覆盖。

## 受管升级

升级与重启由 fork 出的、脱离当前会话的 worker 执行：核对旧服务身份，调 `claudex gateway stop` 停旧，升级时切换 `~/.local/bin/cli-proxy-api`，调 `claudex gateway start` 起新（它在网关不健康时非零退出），再核对新服务身份；任一步失败回退到旧二进制与旧服务，停旧之后的任何异常都落 failed 并回滚。发布包按官方 `checksums.txt` 校验，归档成员做路径穿越与 symlink 检查。同一时刻只允许一次 rollout。
