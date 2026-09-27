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
| 不带版本的 `claudex upgrade` 升到上游最新版，不区分大版本；`status` 在记到新版时的 `note:` 行建议的也是这条命令。CLIProxyAPI v8 以配置层迁移为主，claudex 的配置生成、就地覆写与热加载前提按 v7.3.20 源码核实 | 照 `note:` 行升级会跨到 v8，网关可能改写或不接受生成的配置；受管升级只在新网关不健康时回退，健康但行为改变的情形拦不住。升级时显式写版本号（`claudex upgrade 7.3.20`） | 中 |

## 路线图

- `claudex doctor`：把 `status`、`preflight` 与常见故障的排查步骤合成一次诊断。
- `claudex usage`：按会话与来源汇总费用。
- 逐模型档位：OpenRouter 目录的 `reasoning.supported_efforts` 字段给出每个模型支持的档位，可替代「支持 reasoning 即 low、medium、high」的缺省规则。
- 真实接口核对：OpenRouter `/api/v1/credits` 应答是否带 `data` 包装、Antigravity 上游超时经网关 `api-call` 返回的形态，现有解析对两种形态都接受，尚未在真实接口上逐一核对。
