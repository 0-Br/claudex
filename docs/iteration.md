# claudex 迭代状态

claudex 当前状态的快照与展望。

## 当前状态

- 运行环境：uv 项目，Python 3.14；开发环境 `uv sync --locked --group dev --python 3.14`，源码树的 `bin/claudex` 可直接运行；日常运行按 tag 安装为 uv tool。运行时需要 PATH 上的 `claude` 与 `~/.local/bin/cli-proxy-api`（README 第 2、3 节）。
- 来源：`codex`、`antigravity`、`openrouter`、`openai`、`anthropic` 五种 type 可用，写法见 README 第 5 节。
- 命令：README 第 10 节所列命令全部可用。
- 验证：测试全部离线，启动器用例需要非特权用户命名空间；lint、格式与类型检查都是零诊断，检查命令见 AGENTS.md「开发与验证」节。

## 已知问题

| 问题 | 影响 | 优先级 |
| --- | --- | --- |
| OpenAI 兼容段（`openrouter`、`openai` 类来源）无法让网关剥掉档位，无档位模型仍会收到 low、medium、high | 不接受 `reasoning_effort` 的上游可能拒收请求；可用 `claudex probe` 发现，README 第 5.3 节已说明 | 低 |
| 起步文件 `templates/claudex.toml`（与 `examples/claudex.toml`）的首行注释写「claudex 的唯一配置文件」，而使用者要维护的配置有三份，另两份是 `gateway.base.yaml` 与 `settings.base.json` | 与 README 第 5 节把三份都列为使用者维护的说法不一致，读者可能以为另两份不用管；不影响功能。改成「来源、模型与 profile 的唯一真相源」这类说法即可，改后已有的用户配置要各自同步这一行 | 低 |
| `cli.py` 的分区注释与 `catalog.py` 一个函数的 docstring 写「按 spec 的次序」「按 spec 的取值顺序」，仓库里没有这份文档 | 读者找不到这句话指的规定；不影响功能。改成直接写出所遵守的次序，或指向 `docs/architecture.md` 的启动次序与 README 的对应小节 | 低 |
| 根指引 `AGENTS.md` 的第三行要求开工前先读项目规则目录下的全部文件，仓库里没有这个目录 | 照着做的读者会去找一个不存在的目录；不影响功能。删掉这一句即可 | 低 |
| 启动器把不认识的首个参数一律当作会话启动（`bin/claudex` 的 `main`）：`claudex --version` 不报 claudex 的版本，而是生成网关配置、拉起网关、写一份会话快照，再把 `--version` 透传给 claude，打印出来的是 Claude Code 的版本；按同一条代码路径，拼错的子命令（如把 `status` 敲错）会被当成提示词开出一个会话。claudex 自己没有读版本的入口 | 按惯例探测版本的使用者与脚本会在不知情时拉起常驻的网关进程并改写 state 目录，而输出看起来像一次成功的版本查询，事后不易察觉；拼错子命令可能直接发出模型请求。可选的改法：`--version`、`-V` 与 `version` 由启动器自己处理，只打印版本、不碰网关；未识别的前导参数报错并提示用 `--` 分隔，只把 `--` 之后的内容透传给 claude。后一项会改变现有的调用方式（如 `claudex --resume` 要写成 `claudex -- --resume`），取舍留到动手时定 | 中 |

## 路线图

- `claudex doctor`：把 `status`、`preflight` 与常见故障的排查步骤合成一次诊断。
- `claudex usage`：按会话与来源汇总费用。
- 逐模型档位：OpenRouter 目录的 `reasoning.supported_efforts` 字段给出每个模型支持的档位，可替代「支持 reasoning 即 low、medium、high」的缺省规则。
- CLIProxyAPI v8 的核实：读 v8 的配置加载与迁移代码，核对配置文件监视、OpenAI 兼容段的档位转发、日志目录解析是否仍成立，以及 v8 是否在启动时改写 `gateway.yaml`；核实后更新 `upgrade.VERIFIED_GATEWAY_VERSION` 与 README 第 3 节。
- 启动器失败分支的用例：`running but not healthy`、端口被不认识的进程占用、残留 PID 文件、网关二进制缺失、`claude` 不在 PATH、渲染没有产出 settings、网关 5 秒内停不下来，这几条出口目前只靠读代码确认，没有命名空间用例。
- 真实接口核对：OpenRouter `/api/v1/credits` 应答是否带 `data` 包装、Antigravity 上游超时经网关 `api-call` 返回的形态，现有解析对两种形态都接受，尚未在真实接口上逐一核对。
