# claudex 迭代状态

claudex 当前状态的快照与展望。

## 当前状态

- 运行环境：uv 项目，Python 3.14；开发环境 `uv sync --locked --group dev --python 3.14`，源码树的 `bin/claudex` 可直接运行；日常运行按 tag 安装为 uv tool。运行时需要 PATH 上的 `claude` 与 `~/.local/bin/cli-proxy-api`（README 第 2、3 节）。
- 来源：`codex`、`antigravity`、`openrouter`、`openai`、`anthropic` 五种 type 可用，写法见 README 第 5 节。
- 命令：README 第 10 节所列命令全部可用。
- 验证：测试全部离线，启动器用例需要非特权用户命名空间；类型检查基线为空，检查命令见 AGENTS.md「开发与验证」节。

## 已知问题

| 问题 | 影响 | 优先级 |
| --- | --- | --- |
| OpenAI 兼容段（`openrouter`、`openai` 类来源）无法让网关剥掉档位，无档位模型仍会收到 low、medium、high | 不接受 `reasoning_effort` 的上游可能拒收请求；可用 `claudex probe` 发现，README 第 5.3 节已说明 | 低 |

## 路线图

- `claudex doctor`：把 `status`、`preflight` 与常见故障的排查步骤合成一次诊断。
- `claudex usage`：按会话与来源汇总费用。
- 逐模型档位：OpenRouter 目录的 `reasoning.supported_efforts` 字段给出每个模型支持的档位，可替代「支持 reasoning 即 low、medium、high」的缺省规则。
- CLIProxyAPI v8 的核实：读 v8 的配置加载与迁移代码，核对配置文件监视、OpenAI 兼容段的档位转发、日志目录解析是否仍成立，以及 v8 是否在启动时改写 `gateway.yaml`；核实后更新 `upgrade.VERIFIED_GATEWAY_VERSION` 与 README 第 3 节。
- 启动器失败分支的用例：`running but not healthy`、端口被不认识的进程占用、残留 PID 文件、网关二进制缺失、`claude` 不在 PATH、渲染没有产出 settings、网关 5 秒内停不下来，这几条出口目前只靠读代码确认，没有命名空间用例。
- 真实接口核对：OpenRouter `/api/v1/credits` 应答是否带 `data` 包装、Antigravity 上游超时经网关 `api-call` 返回的形态，现有解析对两种形态都接受，尚未在真实接口上逐一核对。
