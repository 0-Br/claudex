# claudex

开始任何工作前，先列出并逐字读取 `.claude/rules/` 下全部 `.md` 文件（Claude Code 已自动加载该目录，无需重复读取）。

claudex 以专用 settings 启动 Claude Code，把模型请求经本机 CLIProxyAPI 网关转到所选后端；它由 bash 启动器 `bin/claudex` 与 Python 包 `claudex` 组成，是 uv 项目，Python 3.14。本文件只记在本仓库工作需要的信息；README 面向使用者，写安装、配置与对外接口，本文件面向在本仓库做开发的 agent，两者不互相复述。默认模式：开发模式。

## 首次阅读

开始工作前按顺序逐字读：

1. `docs/architecture.md`：模块分工、启动次序、数据落点与技术栈。
2. `docs/iteration.md`：当前状态、已知问题与路线图。
3. `README.md` 第 5 节到第 11 节：配置、状态栏对象、计价、命令、`status` 与 `preflight` 的输出格式、持久落点与覆盖变量，这是本包的对外接口，改动前必须知道现状。

`docs/decisions.md` 是决策日志，不通读，按关键词检索后读命中的条目。

## 受管接口

| 受管状态 | 唯一写入通道 |
| --- | --- |
| 项目环境 `.venv/` 与锁文件 `uv.lock` | uv 命令；运行与测试一律带 `--locked`，加减依赖与改锁（`uv add`、`uv remove`、`uv lock`）由维护者执行 |
| 类型检查基线 `.basedpyright/baseline.json` | `uv run --locked basedpyright --writebaseline`，只在存量诊断清掉一批之后重写并单独提交；没有存量时基线是空形态 `{"files": {}}`。basedpyright 不认配置文件里的基线模式键，平时运行一律带 `--baselinemode=discard`，否则它会自动改写基线 |
| `init` 的起步文件 | 正本在 `src/claudex/templates/`；`examples/` 下的同名文件与之逐字相同，由测试断言，改一处同步另一处 |
| 用户配置 `~/.config/claudex/` 与 state `~/.local/state/claudex/` | 只经 claudex 自身的命令写（`init`、`key set`、启动与刷新程序）；代码与测试在测试里一律经 `CLAUDEX_CONFIG_DIR`、`CLAUDEX_STATE_DIR` 指到临时目录 |

本表是完整枚举，新增受管状态在此加一行。

## 开发与验证

改动与验证本项目时用到的命令与约定：

| 项 | 取值 |
| --- | --- |
| 测试命令 | 全量：`uv run --locked pytest`；收集：`uv run --locked pytest --collect-only -q`。首次运行前 `uv sync --locked --group dev --python 3.14` |
| CLI 前缀 | 源码树的 `bin/claudex`（项目环境同步之后；启动器用 `.venv/bin/python`）；只调 Python 子命令时也可用 `uv run --locked python -P -m claudex.cli` |
| 必读文档清单 | 见首次阅读 |
| 变更同步矩阵（改了什么就要同步什么） | 见下方「变更同步矩阵」一节 |
| 记账载体（决策与状态记在哪里） | 决策日志 `docs/decisions.md`；状态快照、已知问题与路线图 `docs/iteration.md` |
| 批次验证映射（改了哪些文件就跑哪些测试） | `src/claudex/<模块>.py` → `tests/test_<模块>.py`；`paths.py`、`jsonio.py`、`config.py`、`pyproject.toml`、`tests/conftest.py`、`tests/fake_gateway.py`、`tests/fake_service.py`、`bin/claudex` 是共享底座，改了就跑全量。类型检查不收窄，每批都对整个项目跑 |
| lint 与 format 命令与政策 | `uv run --locked ruff check .` 与 `uv run --locked ruff format --check .`，零违规；`shellcheck -x bin/claudex bin/claudex-client-key` 零告警。ruff 配置写在 `pyproject.toml` 的 `[tool.ruff]`，自足、不继承别处的配置；ruff 版本由 `uv.lock` 钉住，只用项目环境里的 ruff |
| 机械核查命令（lint 之外的检查） | `uv run --locked basedpyright --baselinemode=discard`，standard 档，以退出码判定，相对基线不新增 error |
| 严重度本域举例（三级严重度在本项目里长什么样） | critical：key 进了 argv、日志、错误信息、快照或状态文件，`gateway.yaml` 或 key 文件权限不是 0600，写入 `gateway.yaml` 换掉了 inode 使网关热加载失效，状态栏费用重复计或漏计，受管升级失败后没有回退。warning：`status`、`preflight` 的输出格式与 README 第 10 节不一致，`claudex` 对象字段与 README 第 8 节不一致，测试依赖真实网关、真实 HOME 或网络，错误只报不报位置。info：提示措辞，帮助文本，文档排版 |

## 变更同步矩阵

本表是完整枚举。

| 变更 | 必须同步 |
| --- | --- |
| `claudex.toml` 的字段或校验规则 | `config.py`、`src/claudex/templates/claudex.toml` 与 `examples/claudex.toml`、README 第 5 节、`tests/test_config.py` |
| 网关配置的生成规则、禁止键或写入方式 | `gateway.py`、`templates/gateway.base.yaml` 与 `examples/`、README 第 5.1 节与第 10.3 节、`docs/architecture.md` 的启动次序、`tests/test_gateway.py` |
| 派生 settings 的键或快照形态 | `render.py`、`statusline.py` 的快照读取、README 第 5.4 节与第 11 节、`tests/test_render.py`、`tests/test_statusline.py` |
| `claudex` 对象字段、底层渲染器的调用方式 | `statusline.py`、README 第 8 节、`tests/test_statusline.py` |
| 子命令、参数、`status` 的行前缀、`preflight` 的 JSON 或诊断类别 | `cli.py`、`bin/claudex`、`templates/completion.bash`、README 第 7 节与第 10 节、`tests/test_cli.py` |
| 持久落点或覆盖变量 | `paths.py`、README 第 11 节、`docs/architecture.md` 的数据落点、`tests/test_paths.py` |
| 计价口径或额度来源 | `catalog.py`、`quota.py`、README 第 9 节、对应测试 |
| 运行依赖的版本 | `pyproject.toml` 的钉死版本；由维护者 `uv lock` 后重跑全部验证 |
| 发布新版本 | `src/claudex/__init__.py` 的 `__version__`、git tag、README 第 3 节安装命令里的 tag |
| 模块职责或设计原则 | `docs/architecture.md` |

## 测试约定

- 测试全部离线：网关与上游一律用 `tests/fake_gateway.py`、`tests/fake_service.py` 的本地假服务，或替换请求函数；不访问真实网络，不连真实的 8317 端口。
- 启动器用例以子进程运行源码树 `bin/claudex`，在 `unshare` 建的独立用户与网络命名空间里监听 8317；系统不允许非特权用户命名空间时这些用例会跳过，所以每次全量运行都核对跳过数为零（`pytest -rs`）。
- 配置、来源名与模型 id 由测试代码现场构造，一律用虚构名（来源名如 `alpha`、`sub`、`or`，模型 id 如 `model-a`、`vendor/model-b`），不写真实型号、价格与阵容；域名用 `example.invalid`；key 用一眼可见的占位值，不写真实 key。
- 根目录一律经 `CLAUDEX_CONFIG_DIR`、`CLAUDEX_STATE_DIR` 与假 HOME 指到 `tmp_path`，测试不写真实 HOME。
- 一个测试函数验证一个行为；涉及凭据的行为，同时断言 key 不出现在输出、异常与文件里。
