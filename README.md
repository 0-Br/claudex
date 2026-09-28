# claudex

claudex launches Claude Code with a dedicated settings file and routes its model requests through a local CLIProxyAPI gateway, so each of the four model tiers (fable, opus, sonnet, haiku) can be served by a backend you choose: a subscription channel proxied by the gateway (Codex, Antigravity), OpenRouter, or any OpenAI- or Anthropic-compatible endpoint. One `claudex.toml` declares the sources, the models you pick from each, and named profiles; claudex generates the gateway configuration, per-session settings snapshots and a status line with OpenRouter-priced cost and subscription quota. It is an unofficial personal project and is not affiliated with or endorsed by Anthropic, OpenAI, Google or OpenRouter. Routing a subscription through a gateway may conflict with the providers' terms of service, so use it at your own risk. The rest of this document is in Chinese.

## 1. 工作原理

claudex 由一个 bash 启动器和一个 Python 包组成，装成一个 uv tool。每次运行 `claudex` 启动会话时，按固定次序做四件事：

1. 以你写的 `gateway.base.yaml` 为底稿，按 `claudex.toml` 生成网关配置 `gateway.yaml`，其中含全部来源段与 key；
2. 确保 CLIProxyAPI 网关在 `127.0.0.1:8317` 上运行，没在跑就拉起它；这次改写了配置而网关原本就在跑时，等网关加载完新模型；
3. 生成一份不可变的会话快照：Claude Code 的派生 settings，以及状态栏读的模型表；
4. 以这份派生 settings 启动 `claude`。

会话只读启动时那份快照，之后改 `claudex.toml` 不影响已经开着的会话。

来源分两类：

| 类别 | `type` | 网关里的接法 | claudex 额外做的事 |
| --- | --- | --- | --- |
| 订阅反代 | `codex`、`antigravity` | OAuth 通道（`claudex login`） | 额度窗口与冷却；context 与档位取自网关模型定义 |
| 通用接口 | `openrouter` | OpenAI 兼容段，地址固定为 OpenRouter | 账户余额；元数据与价格取自 OpenRouter 目录 |
| 通用接口 | `openai` | OpenAI 兼容段，地址自填 | 无 |
| 通用接口 | `anthropic` | Anthropic 兼容段，地址自填 | 无 |

每个来源只列你要用的模型。没列的模型不写进网关配置，也不出现在 Claude Code 的模型选择里。

## 2. 前置条件

- Linux x86_64。启动器用到 `bash`、`curl`、`flock` 与 `/proc`。
- uv，以及 uv 托管的 Python 3.14（`uv python install --no-bin 3.14`）。
- Claude Code：`claude` 命令在 PATH 上，claudex 取 PATH 上的第一个。
- CLIProxyAPI 网关的二进制，位置固定为 `~/.local/bin/cli-proxy-api`，首装方法见第 3 节。网关端口固定为 8317，不能改。

## 3. 安装

安装 claudex：

```bash
uv tool install --managed-python --python 3.14 "claudex @ git+https://github.com/0-Br/claudex@v0.1.0"
```

安装后，`claudex` 与 `claudex-client-key` 两个命令在 `~/.local/bin` 下。运行依赖只有 PyYAML，在 `pyproject.toml` 里钉死版本，因为 `uv tool install` 不读 `uv.lock`。升级时换 tag，重新执行同一条命令并加 `--reinstall`。

首次安装网关二进制：这一步手工做一次，布局与 `claudex upgrade` 管理的一致，之后的升级交给 `claudex upgrade`。

```bash
v=X.Y.Z   # 换成 CLIProxyAPI 的发布版本号，不带 v
dir=~/.local/lib/cliproxyapi/$v
mkdir -p "$dir" ~/.local/bin && cd "$(mktemp -d)"
base=https://github.com/router-for-me/CLIProxyAPI/releases/download/v$v
curl -fLO "$base/CLIProxyAPI_${v}_linux_amd64.tar.gz" && curl -fLO "$base/checksums.txt"
grep " CLIProxyAPI_${v}_linux_amd64.tar.gz\$" checksums.txt | sha256sum -c -
tar -xzf "CLIProxyAPI_${v}_linux_amd64.tar.gz" -C "$dir" cli-proxy-api
ln -sfn "$dir/cli-proxy-api" ~/.local/bin/cli-proxy-api
```

## 4. 起步

```bash
claudex init
```

`init` 建配置目录 `~/.config/claudex/` 与其中的 `keys/`（0700），生成两枚随机 key：网关的下游 key `client.key` 与管理接口密码 `management.key`，都是 0600、64 位小写十六进制。随后写三份起步文件：`claudex.toml`、`gateway.base.yaml`、`settings.base.json`。已存在的文件不覆盖，逐个说明跳过。仓库 `examples/` 下是同样的三份起步文件。

接着按第 5 节写来源与 profile，按第 6 节登录订阅、录入 key，然后：

```bash
claudex preflight      # 检查配置、网关模型、订阅定义与目录条目
claudex                # 用 default_profile 启动会话
```

## 5. 配置

### 5.1 文件

配置目录缺省为 `~/.config/claudex/`，可用 `CLAUDEX_CONFIG_DIR` 覆盖：

| 文件 | 谁写 | 内容 |
| --- | --- | --- |
| `claudex.toml` | 你 | 来源、模型、profile 与全局选项，唯一的真相源 |
| `gateway.base.yaml` | 你 | 网关的非来源配置，如 `proxy-url`、`routing`、`disable-image-generation`、`payload` 规则；不含任何 key 与来源段 |
| `settings.base.json` | 你 | 派生 Claude Code settings 的基底，透传规则见 5.4 |
| `client.key`、`management.key` | `claudex init` | 下游 key 与管理密码，0600 |
| `keys/<来源名>.key` | `claudex key set` | 通用来源的上游 API key，0600 |

`gateway.base.yaml` 里写了 `host`、`port`、`auth-dir`、`api-keys`、`remote-management.secret-key`，或者 `openai-compatibility`、`claude-api-key` 段，都会报配置错误，因为这些由 claudex 生成。

### 5.2 `claudex.toml`

```toml
default_profile = "daily"
mcp_deny = ["mcp__github__*"]      # 可选；不写即不屏蔽任何 MCP 工具
compact_window_factor = 0.95       # 可选，缺省 0.95

[sources.sub]
type = "codex"
models = [
  "model-a",
  { id = "model-b", context = 900000, openrouter = "vendor/model-b", display = "Model B" },
]

[sources.or]
type = "openrouter"
models = ["vendor/model-c", "vendor/model-d"]

[sources.plan]
type = "anthropic"
base_url = "https://example.invalid/anthropic"
models = [
  { id = "model-e", openrouter = "vendor/model-e" },
  { id = "model-f", context = 256000, efforts = ["low", "high"] },
]

[profiles.daily]
fable = "or/vendor/model-d"
opus = "sub/model-b"
sonnet = "plan/model-e"
haiku = "or/vendor/model-c"
```

示例里的来源名、模型 id 与 slug 都是虚构的。规则如下：

- 顶层只允许 `default_profile`（必填，须是已定义的 profile）、`mcp_deny`、`compact_window_factor`（取值 (0, 1]）、`sources`、`profiles` 五个键。
- 来源名与 profile 名取 `[a-z0-9-]+`。通用来源的来源名同时是它在网关里的前缀。每种订阅 `type` 至多一个来源，不同订阅来源不能列出同一个上游 id。
- `models` 的元素是字符串（上游模型 id）或表。表的字段：
  - `id`：必填；
  - `openrouter`：计价与元数据借用的 OpenRouter slug；`openrouter` 类来源不写时等于 `id`，其他类型不写就没有 slug；
  - `context`、`efforts`：覆盖自动取得的值；
  - `display`：状态栏与模型选择器里的显示名。
- 模型引用写 `<来源名>/<模型 id>`，在第一个 `/` 处切开，模型 id 里可以再含 `/`。profile 的 `fable`、`opus`、`sonnet`、`haiku` 四档都必须引用已列出的模型。
- `openai` 与 `anthropic` 类来源必须写 `base_url`（`http://` 或 `https://` 开头）；`openrouter` 与订阅来源不写。

### 5.3 元数据怎么取

- **context 与档位**：先取配置里显式写的值。没写时，订阅来源取网关的模型定义；通用来源取 `openrouter` slug 在 OpenRouter 目录里的条目，目录条目的 `supported_parameters` 含 `reasoning` 时档位为 `low`、`medium`、`high`，否则不支持档位。
- **显示名**：依次取配置里的 `display`、目录里的名字、模型 id。
- **取不到 context 时**：启动与 `preflight` 报错，`probe` 放行。context 大于 200000 的模型，派生给 Claude Code 的模型 id 末尾加 `[1m]`。

以 Anthropic 或 OpenAI 兼容接口提供的套餐，写法同上面的 `plan` 来源：填厂商给的接口地址，列出要用的模型；目录里查不到的模型，显式写 `context`，需要计价就写 `openrouter`。

网关对无档位模型的处理有一处差异。`anthropic` 类来源的无档位模型，网关会剥掉请求里的档位。OpenAI 兼容段（`openrouter`、`openai` 类）没有剥掉档位的写法，网关会按 `low`、`medium`、`high` 转发，上游是否接受可以用 `probe` 看。

### 5.4 派生 settings 与 `settings.base.json`

每次启动，claudex 以 `settings.base.json` 为基底生成派生 settings。它产出的键会覆盖基底里的同名键：

- 四档模型的环境变量，及其显示名与描述；
- `ANTHROPIC_BASE_URL`；
- `model`、`availableModels`、`modelOverrides`、`modelSettings`（档位按后端支持的范围夹紧）；
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW`；
- `apiKeyHelper`、`statusLine.command`；
- `CLAUDEX_PROFILE`、`CLAUDEX_PROFILE_FILE`、`CLAUDEX_STATUSLINE_COMMAND`、`CLAUDEX_FAST`；
- `ANTHROPIC_CUSTOM_HEADERS` 里的 `X-Claudex-Tier` 一行。

其余内容原样透传，`permissions` 与 `hooks` 在内。基底里的 `statusLine.command` 不会丢：它被当作底层渲染器，见第 8 节。

## 6. 登录、key 与探测

```bash
claudex login codex           # 或 antigravity；凭据由网关写在 ~/.local/share/claudex/auth/
claudex key set or            # 从 stdin 读一行 key，终端输入时不回显；写 keys/or.key（0600）
claudex probe plan            # 列出来源的上游模型
claudex probe plan model-f    # 经网关探测已列出的模型
```

- `key set` 只接受 `claudex.toml` 里已有的通用来源。
- `probe` 不带模型 id 时，列出上游的模型：`openai`、`openrouter` 取 `GET <base_url>/models`，`anthropic` 取 `GET <base_url>/v1/models`，上游不提供这个接口时报错说明；订阅来源列出网关模型定义里的模型。
- 带模型 id 时，这些模型须已写进该来源的 `models`（缺 context 可以），probe 经网关对每个模型发几个极小请求，检验四项：能否应答、工具调用、各档位是否被接受、图片输入。最后打印结果与建议写进配置的 `context`、`efforts`。「档位被接受」只说明上游没有报错，不等于档位真的生效。
- 订阅来源不做能力探测。

## 7. 启动会话

```bash
claudex                          # default_profile
claudex @alt                     # 或 claudex --profile alt
claudex @daily --opus or/vendor/model-c   # 本次启动临时替换一档，不写回配置
claudex --with-mcp               # 本次不屏蔽 mcp_deny 里的工具
claudex --fast                   # 整个会话带 Fast 档请求头
claudex @daily -p "hello"        # 其余参数原样交给 claude
```

- profile 只从 `@名字`、`--profile` 或 `default_profile` 取，不读环境变量。
- `--fable`、`--opus`、`--sonnet`、`--haiku` 后接模型引用，可以重复。
- `--fast` 让会话给网关带 `X-Claudex-Tier: fast` 请求头；不带时，从上一个会话继承的 Fast 头与 `CLAUDEX_FAST` 会被清掉。Fast 档只对 Codex 来源生效：主对话档 `fable` 不是 Codex 来源时，启动时会提示主对话不会走 Fast，而会话里落到 Codex 来源的请求仍按 Fast 计费。

## 8. 状态栏

派生 settings 的 `statusLine.command` 是 claudex 的状态栏适配器。它读 Claude Code 给状态栏的 JSON，改写为 claudex 的口径：

- 显示名与 effort 取自快照；
- `cost.total_cost_usd` 换成按 OpenRouter 单价累计的费用；
- `rate_limits` 换成当前订阅来源的额度窗口；
- 上下文分母按所选模型取；
- 删除 `prompt_cache`；
- 加一个 `claudex` 对象。

`claudex` 对象的字段固定：

```json
{"claudex": {"profile": "daily", "source": "or", "source_type": "openrouter",
             "cost_estimated": false, "fast": false, "quota_label": "or",
             "markers": ["…"]}}
```

| 字段 | 含义 |
| --- | --- |
| `profile` | 会话的 profile 名 |
| `source`、`source_type` | 当前模型的来源名与来源类型 |
| `cost_estimated` | 已结算的费用里有没有按等价标价估算的部分；为真时，显示费用前加 ≈ |
| `fast` | Fast 档是否实际生效（带了 `--fast` 且当前模型来自 Codex） |
| `quota_label` | 额度窗口对应的来源标签 |
| `markers` | 额外的短标记，如订阅额度窗口、OpenRouter 余额 |

基底 `settings.base.json` 里的 `statusLine.command`，会被记进派生 env 的 `CLAUDEX_STATUSLINE_COMMAND`，作为底层渲染器：适配器以 `/bin/sh -c` 调用它，stdin 为改写后的 JSON，超时 3 秒，stdout 原样输出。没配置底层渲染器时，适配器自己输出一行简版：显示名、effort、费用与额度。以下四种情形也退回简版，并在 stderr 写原因：渲染器非零退出、超时、退出 0 而 stdout 为空、命令里含 `claudex.statusline`（防递归）。

适配器按 60 秒节流，在后台拉起刷新程序 `python -m claudex.quota`，刷新额度、余额、目录与网关新版记录。

## 9. 计价与额度

- 每个模型的费用，按它的 `openrouter` slug 在 OpenRouter 目录里的单价计算，缓存读写按目录里的缓存单价计入。来源类型不是 `openrouter` 的，显示的是按 OpenRouter 标价折算的等价花费，前面标 ≈。没有 slug 或目录里没有价格的，不显示费用。
- OpenRouter 目录只有一份缓存，后台每 24 小时刷新一次；`claudex update` 强制刷新，缓存损坏时也用它修复。
- 状态栏只结算主对话的每次响应：subagent 与后台调用的用量不计入，显示的费用是会话花费的下限。
- 额度只有三种：Codex 与 Antigravity 的额度窗口与冷却，经网关管理接口取得；`openrouter` 类来源的账户余额，取 `GET /api/v1/credits` 的 `total_credits − total_usage`。某个来源失败时保留上一次成功的数据。
- 平时没有「过期」类提醒。只有真问题才报：profile 引用的模型不在网关里、订阅模型不在网关定义里、slug 不在目录里、配置错误。

## 10. 命令

| 命令 | 说明 |
| --- | --- |
| `claudex [选项…] [claude 参数…]` | 启动会话，见第 7 节 |
| `claudex init` | 建配置目录、生成 key、写起步文件，见第 4 节 |
| `claudex key set <来源名>` | 录入上游 key，见第 6 节 |
| `claudex probe <来源名> [模型 id…]` | 列上游模型、探测模型能力，见第 6 节 |
| `claudex login codex\|antigravity` | OAuth 登录 |
| `claudex profiles` | 列出 profile 与四档，缺省 profile 前标 `*` |
| `claudex status` | 运行态摘要，不联网，格式见 10.1 |
| `claudex preflight [--profile NAME] [--format json\|human] [--no-proxy-check]` | 检查配置、网关模型、订阅定义与目录条目，格式见 10.2 |
| `claudex update` | 强制刷新 OpenRouter 目录，查网关最新版本，列出本机版本之后的变更摘要；不改网关 |
| `claudex upgrade [VERSION] [--wait RUN_ID --timeout 秒]` | 网关受管升级，缺省升到最新版，见 10.3 |
| `claudex gateway start\|stop\|restart` | 拉起、停止、受管重启网关，见 10.3 |
| `claudex completion bash` | 输出 bash 补全脚本，补子命令、`@profile`、档位参数后的模型引用、`key set` 与 `probe` 后的来源名 |
| `claudex-client-key` | 独立命令：无参调用，输出下游 key 一行；key 文件缺失、权限不是 0600 或格式不对时，非零退出、stdout 为空、stderr 写原因 |

退出码：0 成功，1 失败，2 用法错误。补全可以这样启用：在 `~/.bashrc` 里加 `eval "$(claudex completion bash)"`。

### 10.1 `status` 的输出

每行以固定前缀开头：

- `runtime      : `：运行态信息，包括版本、配置与 profile、网关二进制与版本、网关进程、最近的快照、各来源的额度缓存、目录缓存时刻；
- `problem: `：真问题，每个一行；
- `note: `：说明，不算问题；现在只有一种，即后台刷新记录到比本机新的网关版本。

`status` 不联网，网关没在运行时也退出 0。

### 10.2 `preflight` 的 JSON

`--format json` 输出 schema 1：

```json
{"schema": 1, "profile": "daily", "online_checks": true,
 "errors": [], "warnings": [], "unavailable": []}
```

`errors`、`warnings`、`unavailable` 的每一条诊断都含 `category`、`message`、`model`、`profile` 四个字段。`category` 取以下之一：`config`、`catalog`、`catalog_entry`、`catalog_unparsed`、`context_missing`、`gateway_access`、`gateway_model`、`subscription_definition`。`--no-proxy-check` 跳过需要网关的检查，这时 `online_checks` 为 false。

退出码：0 无错误，1 有错误，2 用法错误。`preflight` 不读 `CLAUDEX_PROFILE`。

### 10.3 网关的启停与升级

- `gateway start` 幂等：先生成配置，网关已在跑且健康就直接返回 0。拉起时以 `-config <state>/gateway.yaml` 启动，并设 `WRITABLE_PATH=<state 目录>`，网关日志因此落在 state 目录的 `logs/`。等待网关就绪期间的连接失败不输出；健康检查最终不过时，报出最后一次失败的原因，结束拉起的进程并非零退出。
- `gateway stop` 按 PID 与进程身份核对后停止网关。
- `gateway restart` 与 `upgrade` 走同一套受管流程：由脱离当前会话的 worker 执行，核对旧服务身份，停旧，切换版本（仅升级时），起新，核对新服务；任一步失败，回退到旧二进制与旧服务。升级下载官方发布包，并按官方 `checksums.txt` 校验。
- `upgrade` 要求网关已安装且正在运行，否则报错。

`gateway stop` 与 `restart` 会切断所有正在经网关工作的会话与工具。

## 11. 文件与覆盖变量

| 位置 | 内容 |
| --- | --- |
| `~/.config/claudex/` | 第 5.1 节的配置文件 |
| `~/.local/state/claudex/gateway.yaml` | 生成的网关配置，含全部 key，0600 |
| `~/.local/state/claudex/gateway.pid`、`gateway-start.lock`、`gateway-bootstrap.log` | 网关进程记录、启动锁、拉起时的输出 |
| `~/.local/state/claudex/logs/` | 网关日志与失败快照 |
| `~/.local/state/claudex/sessions/` | 会话快照：`<profile>-<摘要>.profile.json` 与同名 `.settings.json`；状态栏读取时刷新修改时间，7 天未刷新的在下次启动时清理 |
| `~/.local/state/claudex/catalog.json` | OpenRouter 目录缓存 |
| `~/.local/state/claudex/quota.json`、`refresh.json`、`refresh.lock` | 额度与余额缓存、刷新的节流与尝试记录、刷新锁 |
| `~/.local/state/claudex/gateway-release.json` | 网关最新版本与查询时刻 |
| `~/.local/state/claudex/rollouts/`、`upgrade.lock` | 受管升级与重启的记录、并发锁 |
| `~/.local/share/claudex/auth/` | 网关的 OAuth 凭据 |
| `~/.local/bin/cli-proxy-api` → `~/.local/lib/cliproxyapi/<版本>/cli-proxy-api` | 网关二进制，由受管升级切换 |
| `$TMPDIR/claudex-sl-<摘要>.json` | 状态栏在会话内的费用结算状态 |

claudex 自己写的 state 文件都是 0600，网关日志的权限由网关决定。覆盖变量：

| 变量 | 作用 |
| --- | --- |
| `CLAUDEX_CONFIG_DIR` | 配置目录 |
| `CLAUDEX_STATE_DIR` | state 目录 |
| `CLAUDEX_NO_REFRESH=1` | 状态栏不拉起后台刷新，用于冒烟与测试 |

OAuth 目录、网关二进制与版本目录的位置固定，不受覆盖变量影响。`CLAUDEX_PROFILE` 与 `CLAUDEX_PROFILE_FILE` 由 claudex 写进会话，供外部识别 claudex 会话，不作输入。

## 12. 排障

- **`preflight` 报 `gateway_model`，或启动时报等不到模型注册**：先看 `claudex status` 的网关进程行。网关在跑却没加载新配置时，运行 `claudex gateway restart`。
- **报 `subscription_definition`**：订阅里没有这个模型，或登录已失效；重新 `claudex login`，或者把这个模型从 `claudex.toml` 里移除。
- **报 `catalog_entry` 或 `catalog_unparsed`**：OpenRouter 目录里没有这个 slug，或它的条目无法解析；核对 slug，或在配置里显式写 `context`。
- **目录缓存损坏**：`claudex update`。
- **`claudex upgrade` 报网关未运行**：先 `claudex gateway start`。报二进制不存在：按第 3 节首装。
- **状态栏退成一行简版**：stderr 里写着底层渲染器失败的原因。

## 13. 安全与备份

- key 只从文件读，不进命令行参数、日志、错误信息、快照与状态文件。
- `~/.local/state/claudex/gateway.yaml` 与 `~/.config/claudex/keys/`、`client.key`、`management.key` 含凭据，查看时只按键查询，不整文件输出；备份时排除它们，或以加密形式备份。`gateway.yaml` 可以随时由 claudex 重新生成，不必备份。
- `~/.local/share/claudex/auth/` 是网关的 OAuth 凭据，同样排除或加密备份；丢失后重新 `claudex login` 即可。
- `~/.local/state/claudex/logs/` 整个目录排除在备份之外：网关日志带 API key 的掩码片段与账户标签，失败快照含完整的请求内容。
- `claudex.toml`、`gateway.base.yaml` 与 `settings.base.json` 不含凭据，可以照常备份。
- 网关只监听 `127.0.0.1`；本机对网关的请求不走环境代理。

## 14. 开发

```bash
uv sync --locked --group dev --python 3.14
uv run --locked pytest
uv run --locked ruff check . && uv run --locked ruff format --check .
uv run --locked basedpyright --baselinemode=discard
shellcheck -x bin/claudex bin/claudex-client-key
```

源码树里的 `bin/claudex` 可以直接运行，它使用项目环境 `.venv` 的解释器。启动器测试在 `unshare` 建的独立用户与网络命名空间里运行，系统不允许非特权用户命名空间时，这些测试会被跳过，所以看测试结果时要核对跳过数为零。

## 15. 许可证

MIT，见 `LICENSE`。
