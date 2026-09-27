"""启动器测试用的假网关：替代 CLIProxyAPI，在假 HOME 里经 `install` 装成
`~/.local/bin/cli-proxy-api`。

启动器按 `/proc/<pid>/exe` 核对网关进程的身份，脚本的 exe 是解释器而不是脚本本身。
所以 `install` 把 `cli-proxy-api` 做成指向真实 Python 解释器的 symlink，并在它的上一级
目录放 `pyvenv.cfg`，使它按一个独立的虚拟环境启动；该环境 site-packages 里的
`sitecustomize.py` 在解释器初始化时发现命令行是 `-config <gateway.yaml>`，就运行
`serve` 并直接退出进程。这样 exe 与 symlink 解析到同一个原生二进制，身份核对照常生效，
仓库里也不需要编译器。

`serve` 的行为：

- 按 gateway.yaml 的 `host`、`port` 监听；`/v1/models` 核对 `api-keys` 里的 Bearer，
  返回配置里全部通用来源模型（`<prefix>/<alias>`）；`/v0/management/*` 核对
  `remote-management.secret-key`，模型定义一律为空列表；其余路径 404。
- `FAKE_GATEWAY_UNHEALTHY=1` 时进程照常监听，但 `/v1/models` 一律 503，模拟起得来却始终
  不健康的网关。
- 配置文件内容变化后，按 `FAKE_GATEWAY_RELOAD_DELAY` 秒（缺省 0）才换成新的模型集合；
  取值 `never` 时永远不换，用来模拟等不到注册。
- `FAKE_GATEWAY_LOG` 指向的文件记一行启动信息（`start cwd=… writable=…`）与每个请求的
  `方法 路径 auth=yes|no`，不记请求头的值。
"""

import hashlib
import json
import os
import sys
import sysconfig
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

RELOAD_DELAY_ENV = "FAKE_GATEWAY_RELOAD_DELAY"
UNHEALTHY_ENV = "FAKE_GATEWAY_UNHEALTHY"
LOG_ENV = "FAKE_GATEWAY_LOG"
NEVER = "never"
SECTION_KEYS = ("openai-compatibility", "claude-api-key")
_SITECUSTOMIZE = """\
import sys

if sys.orig_argv[1:2] == ["-config"] and len(sys.orig_argv) >= 3:
    import fake_gateway

    fake_gateway.serve(sys.orig_argv[2])
"""


def _log(line: str) -> None:
    target = os.environ.get(LOG_ENV)
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(f"{line}\n")


def _load(path: Path) -> tuple[bytes, dict[str, object]]:
    raw = path.read_bytes()
    loaded: object = yaml.safe_load(raw)
    if not isinstance(loaded, dict):
        raise SystemExit(f"fake gateway: {path} is not a mapping")
    return raw, {str(key): value for key, value in loaded.items()}


def _models(content: dict[str, object]) -> list[str]:
    ids: list[str] = []
    for section in SECTION_KEYS:
        entries = content.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            prefix = entry.get("prefix")
            models = entry.get("models")
            if not isinstance(models, list):
                continue
            ids.extend(
                f"{prefix}/{model['alias']}"
                for model in models
                if isinstance(model, dict) and "alias" in model
            )
    return ids


def _secret(content: dict[str, object]) -> str:
    management = content.get("remote-management")
    if isinstance(management, dict):
        value = management.get("secret-key")
        if isinstance(value, str):
            return value
    return ""


class _State:
    """当前生效的配置与待生效的新配置。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        raw, self.content = _load(path)
        self.digest = hashlib.sha256(raw).hexdigest()
        self.pending_since: float | None = None
        self.delay = os.environ.get(RELOAD_DELAY_ENV, "0")

    def current(self) -> dict[str, object]:
        with self.lock:
            raw = self.path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest == self.digest:
                self.pending_since = None
                return self.content
            now = time.monotonic()
            if self.pending_since is None:
                self.pending_since = now
            if self.delay != NEVER and now - self.pending_since >= float(self.delay):
                _raw, self.content = _load(self.path)
                self.digest = digest
                self.pending_since = None
            return self.content


def _handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _reply(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            content = state.current()
            bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if self.path.startswith("/v0/management/"):
                allowed = bool(bearer) and bearer == _secret(content)
            else:
                keys = content.get("api-keys")
                allowed = isinstance(keys, list) and bearer in keys
            _log(f"GET {self.path} auth={'yes' if allowed else 'no'}")
            if not allowed:
                self._reply(401, {"error": "unauthorized"})
            elif self.path == "/v1/models" and os.environ.get(UNHEALTHY_ENV) == "1":
                self._reply(503, {"error": "unhealthy"})
            elif self.path == "/v1/models":
                data = [{"id": model_id} for model_id in _models(content)]
                self._reply(200, {"object": "list", "data": data})
            elif self.path.startswith("/v0/management/model-definitions/"):
                self._reply(200, {"models": []})
            else:
                self._reply(404, {"error": "not found"})

    return Handler


def serve(config_path: str) -> None:
    """以 `config_path` 为配置运行假网关，直到进程被信号终止。"""
    state = _State(Path(config_path))
    host = str(state.content.get("host", "127.0.0.1"))
    port = int(str(state.content["port"]))
    server = ThreadingHTTPServer((host, port), _handler(state))
    _log(f"start cwd={Path.cwd()} writable={os.environ.get('WRITABLE_PATH', '')}")
    try:
        server.serve_forever()
    finally:
        os._exit(0)


def install(home: Path) -> Path:
    """在假 HOME 里装好假网关，返回 `~/.local/bin/cli-proxy-api` 的路径。"""
    root = home / ".local"
    binary = root / "bin" / "cli-proxy-api"
    binary.parent.mkdir(parents=True, exist_ok=True)
    interpreter = Path(sys.executable).resolve()
    binary.symlink_to(interpreter)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    (root / "pyvenv.cfg").write_text(
        f"home = {interpreter.parent}\ninclude-system-site-packages = false\n"
        f"version = {version}\n",
        encoding="utf-8",
    )
    site_packages = root / "lib" / f"python{version}" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    # 本模块所在目录与项目环境的 site-packages（取 PyYAML）
    (site_packages / "fake_gateway.pth").write_text(
        f"{Path(__file__).resolve().parent}\n{sysconfig.get_path('purelib')}\n",
        encoding="utf-8",
    )
    (site_packages / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return binary
