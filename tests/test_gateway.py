"""claudex.gateway：gateway.yaml 的生成与就地写入、key 读取、网关查询与等待注册。"""

import json
import os
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from claudex import gateway, paths
from claudex.config import Config, ConfigError, parse_config

CLIENT_KEY = "c0ffee11" * 8
MANAGEMENT_KEY = "0badf00d" * 8
ALPHA_KEY = "sk-FAKE-alpha-9q7w3e"
BETA_KEY = "sk-FAKE-beta-4r8t2y"
PLAN_KEY = "sk-FAKE-plan-6u1i5o"
ALL_KEYS = (CLIENT_KEY, MANAGEMENT_KEY, ALPHA_KEY, BETA_KEY, PLAN_KEY)


def _config() -> Config:
    """订阅、openrouter、openai、anthropic 四种来源各一个的最小配置。"""
    return parse_config(
        {
            "default_profile": "daily",
            "sources": {
                "sub": {"type": "codex", "models": ["model-a"]},
                "alpha": {
                    "type": "openrouter",
                    "models": [
                        "vendor/model-b",
                        {"id": "vendor/model-c", "context": 400000},
                    ],
                },
                "beta": {
                    "type": "openai",
                    "base_url": "https://example.invalid/v1",
                    "models": [
                        {"id": "model-d", "efforts": ["low", "high"]},
                    ],
                },
                "plan": {
                    "type": "anthropic",
                    "base_url": "https://example.invalid/anthropic",
                    "models": [
                        {"id": "model-e", "context": 256000},
                        {"id": "model-f", "efforts": ["medium"]},
                    ],
                },
            },
            "profiles": {
                "daily": {
                    "fable": "alpha/vendor/model-b",
                    "opus": "sub/model-a",
                    "sonnet": "plan/model-e",
                    "haiku": "beta/model-d",
                },
            },
        }
    )


def _subscription_only_config() -> Config:
    return parse_config(
        {
            "default_profile": "daily",
            "sources": {"sub": {"type": "codex", "models": ["model-a"]}},
            "profiles": {
                "daily": {
                    "fable": "sub/model-a",
                    "opus": "sub/model-a",
                    "sonnet": "sub/model-a",
                    "haiku": "sub/model-a",
                },
            },
        }
    )


def _secrets() -> gateway.GatewaySecrets:
    return gateway.GatewaySecrets(
        client_key=CLIENT_KEY,
        management_key=MANAGEMENT_KEY,
        source_keys={"alpha": ALPHA_KEY, "beta": BETA_KEY, "plan": PLAN_KEY},
    )


def _write_key_file(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _write_key_files() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.keys_dir().mkdir(parents=True, exist_ok=True)
    _write_key_file(paths.client_key_file(), CLIENT_KEY + "\n")
    _write_key_file(paths.management_key_file(), MANAGEMENT_KEY)
    for name, key in (("alpha", ALPHA_KEY), ("beta", BETA_KEY), ("plan", PLAN_KEY)):
        _write_key_file(paths.source_key_file(name), key + "\n")


def _key_file(which: str) -> Path:
    return {
        "client": paths.client_key_file(),
        "management": paths.management_key_file(),
        "alpha": paths.source_key_file("alpha"),
    }[which]


def _section(content: dict[str, object], name: str) -> list[dict[str, object]]:
    section = content[name]
    assert isinstance(section, list)
    return section


def _assert_no_key(text: str) -> None:
    for key in ALL_KEYS:
        assert key not in text


# -----------------------------------------------------------------------------
# 生成


def test_openrouter_source_becomes_openai_compatibility_entry() -> None:
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    entries = _section(content, "openai-compatibility")
    assert entries[0] == {
        "name": "alpha",
        "prefix": "alpha",
        "base-url": "https://openrouter.ai/api/v1",
        "api-key-entries": [{"api-key": ALPHA_KEY}],
        "models": [
            {"name": "vendor/model-b", "alias": "vendor-model-b"},
            {
                "name": "vendor/model-c",
                "alias": "vendor-model-c",
                "max-context-length": 400000,
            },
        ],
    }


def test_openai_source_uses_its_base_url() -> None:
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    entries = _section(content, "openai-compatibility")
    assert [entry["name"] for entry in entries] == ["alpha", "beta"]
    assert entries[1]["base-url"] == "https://example.invalid/v1"
    assert entries[1]["prefix"] == "beta"
    assert entries[1]["api-key-entries"] == [{"api-key": BETA_KEY}]


def test_anthropic_source_becomes_claude_api_key_entry() -> None:
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    assert _section(content, "claude-api-key") == [
        {
            "api-key": PLAN_KEY,
            "prefix": "plan",
            "base-url": "https://example.invalid/anthropic",
            "models": [
                {"name": "model-e", "alias": "model-e", "max-context-length": 256000},
                {
                    "name": "model-f",
                    "alias": "model-f",
                    "thinking": {"levels": ["medium"]},
                },
            ],
        }
    ]


def test_models_with_efforts_declare_thinking_levels() -> None:
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    beta = _section(content, "openai-compatibility")[1]
    assert beta["models"] == [
        {"name": "model-d", "alias": "model-d", "thinking": {"levels": ["low", "high"]}}
    ]


def test_models_without_efforts_omit_thinking() -> None:
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    alpha = _section(content, "openai-compatibility")[0]
    models = alpha["models"]
    assert isinstance(models, list)
    assert all("thinking" not in model for model in models)


def test_efforts_mapping_gives_anthropic_model_thinking_levels() -> None:
    efforts = {
        "plan/model-e": ("low", "medium", "high"),
        "plan/model-f": ("low",),
        "alpha/vendor/model-b": (),
    }
    content = gateway.build_gateway_config(_config(), {}, _secrets(), efforts=efforts)
    plan = _section(content, "claude-api-key")[0]
    assert plan["models"] == [
        {
            "name": "model-e",
            "alias": "model-e",
            "max-context-length": 256000,
            "thinking": {"levels": ["low", "medium", "high"]},
        },
        {"name": "model-f", "alias": "model-f", "thinking": {"levels": ["medium"]}},
    ]
    alpha = _section(content, "openai-compatibility")[0]
    models = alpha["models"]
    assert isinstance(models, list)
    assert all("thinking" not in model for model in models)


def test_prepare_gateway_passes_efforts_through() -> None:
    _write_key_files()
    paths.gateway_base_file().write_text("", encoding="utf-8")
    gateway.prepare_gateway(_config(), efforts={"plan/model-e": ("high",)})
    written = yaml.safe_load(paths.gateway_config_file().read_text(encoding="utf-8"))
    assert written["claude-api-key"][0]["models"][0]["thinking"] == {"levels": ["high"]}


def test_contexts_fill_models_without_explicit_context() -> None:
    contexts = {
        "alpha/vendor/model-b": 131072,
        "alpha/vendor/model-c": 1,
        "sub/model-a": 272000,
    }
    content = gateway.build_gateway_config(_config(), {}, _secrets(), contexts=contexts)
    alpha = _section(content, "openai-compatibility")[0]
    assert alpha["models"] == [
        {
            "name": "vendor/model-b",
            "alias": "vendor-model-b",
            "max-context-length": 131072,
        },
        {
            "name": "vendor/model-c",
            "alias": "vendor-model-c",
            "max-context-length": 400000,
        },
    ]


def test_subscription_sources_generate_no_section() -> None:
    secrets = gateway.GatewaySecrets(
        client_key=CLIENT_KEY, management_key=MANAGEMENT_KEY, source_keys={}
    )
    content = gateway.build_gateway_config(_subscription_only_config(), {}, secrets)
    assert "openai-compatibility" not in content
    assert "claude-api-key" not in content


def test_generated_keys_follow_base_keys() -> None:
    base: dict[str, object] = {
        "proxy-url": "http://example.invalid:3128",
        "remote-management": {"allow-remote": False},
        "routing": {"strategy": "fill-first"},
    }
    content = gateway.build_gateway_config(_config(), base, _secrets())
    assert list(content) == [
        "proxy-url",
        "remote-management",
        "routing",
        "host",
        "port",
        "auth-dir",
        "api-keys",
        "openai-compatibility",
        "claude-api-key",
    ]
    assert content["host"] == "127.0.0.1"
    assert content["port"] == 8317
    assert content["auth-dir"] == str(paths.auth_dir())
    assert Path(str(content["auth-dir"])).is_absolute()
    assert content["api-keys"] == [CLIENT_KEY]
    assert content["remote-management"] == {
        "allow-remote": False,
        "secret-key": MANAGEMENT_KEY,
    }
    assert base["remote-management"] == {"allow-remote": False}


def test_remote_management_is_added_when_base_lacks_it() -> None:
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    assert content["remote-management"] == {"secret-key": MANAGEMENT_KEY}


@pytest.mark.parametrize(
    ("base", "named"),
    [
        ({"openai-compatibility": []}, "openai-compatibility"),
        ({"claude-api-key": []}, "claude-api-key"),
        ({"api-keys": ["x"]}, "api-keys"),
        ({"auth-dir": "/x"}, "auth-dir"),
        ({"host": "0.0.0.0"}, "host"),
        ({"port": 9000}, "port"),
        ({"remote-management": {"secret-key": "x"}}, "secret-key"),
    ],
)
def test_base_forbidden_keys_raise(base: dict[str, object], named: str) -> None:
    with pytest.raises(ConfigError, match=named):
        gateway.build_gateway_config(_config(), base, _secrets())


def test_base_remote_management_must_be_mapping() -> None:
    with pytest.raises(ConfigError, match="remote-management"):
        gateway.build_gateway_config(_config(), {"remote-management": "on"}, _secrets())


def test_missing_source_key_raises_without_key_text() -> None:
    secrets = gateway.GatewaySecrets(
        client_key=CLIENT_KEY,
        management_key=MANAGEMENT_KEY,
        source_keys={"alpha": ALPHA_KEY, "beta": BETA_KEY},
    )
    with pytest.raises(gateway.GatewayError, match="plan") as caught:
        gateway.build_gateway_config(_config(), {}, secrets)
    _assert_no_key(str(caught.value))


def test_secrets_repr_hides_keys() -> None:
    _assert_no_key(repr(_secrets()))


# -----------------------------------------------------------------------------
# 写入


def test_write_creates_file_with_0600_and_0700_parent(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "gateway.yaml"
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    assert gateway.write_gateway_config(content, target) is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == content
    assert [entry.name for entry in target.parent.iterdir()] == ["gateway.yaml"]


def test_rewrite_keeps_inode_and_mode(tmp_path: Path) -> None:
    target = tmp_path / "out" / "gateway.yaml"
    first = gateway.build_gateway_config(_config(), {}, _secrets())
    gateway.write_gateway_config(first, target)
    inode = os.stat(target).st_ino
    second = gateway.build_gateway_config(_config(), {"debug": True}, _secrets())
    assert gateway.write_gateway_config(second, target) is True
    assert os.stat(target).st_ino == inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == second
    assert [entry.name for entry in target.parent.iterdir()] == ["gateway.yaml"]


def test_unchanged_content_is_not_rewritten(tmp_path: Path) -> None:
    target = tmp_path / "gateway.yaml"
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    gateway.write_gateway_config(content, target)
    before = os.stat(target)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns - 10**9))
    stamped = os.stat(target).st_mtime_ns
    assert gateway.write_gateway_config(content, target) is False
    after = os.stat(target)
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == stamped


def test_unchanged_content_still_tightens_mode(tmp_path: Path) -> None:
    target = tmp_path / "gateway.yaml"
    content = gateway.build_gateway_config(_config(), {}, _secrets())
    gateway.write_gateway_config(content, target)
    target.chmod(0o644)
    assert gateway.write_gateway_config(content, target) is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_output_is_unwrapped_unicode_yaml(tmp_path: Path) -> None:
    target = tmp_path / "gateway.yaml"
    long_value = "x" * 300
    content = gateway.build_gateway_config(
        _config(), {"note": "中文说明", "long": long_value}, _secrets()
    )
    gateway.write_gateway_config(content, target)
    text = target.read_text(encoding="utf-8")
    assert "中文说明" in text
    assert f"long: {long_value}\n" in text
    assert text.index("note:") < text.index("host:")


def test_prepare_gateway_writes_state_file_once() -> None:
    _write_key_files()
    paths.gateway_base_file().write_text(
        "routing:\n  strategy: fill-first\n", encoding="utf-8"
    )
    config = _config()
    assert gateway.prepare_gateway(config) is True
    target = paths.gateway_config_file()
    written = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert written["routing"] == {"strategy": "fill-first"}
    assert written["api-keys"] == [CLIENT_KEY]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert gateway.prepare_gateway(config) is False


def test_prepare_gateway_requires_base_file() -> None:
    _write_key_files()
    with pytest.raises(ConfigError, match=r"gateway\.base\.yaml"):
        gateway.prepare_gateway(_config())


def test_empty_base_file_is_empty_mapping() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.gateway_base_file().write_text("", encoding="utf-8")
    assert gateway.load_gateway_base(paths.gateway_base_file()) == {}


def test_base_file_top_level_must_be_mapping() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.gateway_base_file().write_text("- a\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="映射"):
        gateway.load_gateway_base(paths.gateway_base_file())


# -----------------------------------------------------------------------------
# key 文件


def test_load_secrets_reads_all_key_files() -> None:
    _write_key_files()
    secrets = gateway.load_secrets(_config())
    assert secrets.client_key == CLIENT_KEY
    assert secrets.management_key == MANAGEMENT_KEY
    assert secrets.source_keys == {
        "alpha": ALPHA_KEY,
        "beta": BETA_KEY,
        "plan": PLAN_KEY,
    }


def test_load_secrets_reports_missing_source_key_path() -> None:
    _write_key_files()
    paths.source_key_file("beta").unlink()
    with pytest.raises(gateway.GatewayError, match="claudex key set beta") as caught:
        gateway.load_secrets(_config())
    assert str(paths.source_key_file("beta")) in str(caught.value)


@pytest.mark.parametrize(
    ("which", "bad"),
    [
        ("client", "ZZ" + CLIENT_KEY[2:]),
        ("client", CLIENT_KEY.upper()),
        ("management", MANAGEMENT_KEY[:-1]),
        ("alpha", ALPHA_KEY + " trailing"),
        ("alpha", ""),
    ],
)
def test_malformed_key_file_error_omits_content(which: str, bad: str) -> None:
    _write_key_files()
    target = _key_file(which)
    _write_key_file(target, bad + "\n")
    with pytest.raises(gateway.GatewayError, match="格式不对") as caught:
        gateway.load_secrets(_config())
    message = str(caught.value)
    assert str(target) in message
    if bad:
        assert bad not in message
    _assert_no_key(message)


@pytest.mark.parametrize("which", ["client", "management", "alpha"])
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400])
def test_key_file_with_wrong_mode_is_rejected(which: str, mode: int) -> None:
    _write_key_files()
    target = _key_file(which)
    target.chmod(mode)
    with pytest.raises(gateway.GatewayError) as caught:
        gateway.load_secrets(_config())
    message = str(caught.value)
    assert str(target) in message
    assert "0600" in message
    assert f"{mode:04o}" in message
    _assert_no_key(message)


# -----------------------------------------------------------------------------
# 假网关


@dataclass
class _FakeGateway:
    """在临时端口上应答 `/v1/models` 与 model-definitions 的本地假网关。"""

    base_url: str
    models: list[str] = field(default_factory=list)
    definitions: dict[str, object] = field(default_factory=dict)
    status: int = 200
    body: bytes | None = None
    delay: float = 0.0
    # 第 n 次 /v1/models 请求起才返回 late_models（从 1 计）
    late_models: list[str] = field(default_factory=list)
    late_after: int = 0
    requests: list[tuple[str, str]] = field(default_factory=list)


def _handler_for(state: _FakeGateway) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append((self.path, self.headers.get("Authorization", "")))
            if state.delay:
                time.sleep(state.delay)
            if state.body is not None:
                payload = state.body
            elif self.path == "/v1/models":
                count = sum(1 for path, _ in state.requests if path == "/v1/models")
                ids = list(state.models)
                if state.late_after and count >= state.late_after:
                    ids += state.late_models
                payload = json.dumps({"data": [{"id": i} for i in ids]}).encode()
            else:
                payload = json.dumps(state.definitions).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            # 不向测试输出写访问日志
            del format, args

    return Handler


@pytest.fixture
def fake_gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeGateway]:
    # 环境代理指向不可用地址：查询函数必须直连回环
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    state = _FakeGateway(base_url="")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
    server.daemon_threads = True
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()
    return f"http://127.0.0.1:{port}"


def test_fetch_models_returns_ids_with_client_key(fake_gateway: _FakeGateway) -> None:
    fake_gateway.models = ["model-a", "alpha/vendor-model-b"]
    models = gateway.fetch_models(CLIENT_KEY, base_url=fake_gateway.base_url)
    assert models == {"model-a", "alpha/vendor-model-b"}
    assert fake_gateway.requests == [("/v1/models", f"Bearer {CLIENT_KEY}")]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, b'{"error": "unauthorized"}'),
        (500, b"{}"),
        (200, b"not json"),
        (200, b'{"data": "x"}'),
        (200, b'{"data": [{"name": "model-a"}]}'),
    ],
)
def test_fetch_models_failures_raise_without_key(
    fake_gateway: _FakeGateway, status: int, body: bytes
) -> None:
    fake_gateway.status = status
    fake_gateway.body = body
    with pytest.raises(gateway.GatewayError) as caught:
        gateway.fetch_models(CLIENT_KEY, base_url=fake_gateway.base_url)
    _assert_no_key(str(caught.value))


def test_fetch_models_unreachable_raises() -> None:
    with pytest.raises(gateway.GatewayError, match="请求失败") as caught:
        gateway.fetch_models(CLIENT_KEY, base_url=_closed_port_url())
    _assert_no_key(str(caught.value))


def test_fetch_models_times_out(
    fake_gateway: _FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "REQUEST_TIMEOUT_SECONDS", 0.2)
    fake_gateway.delay = 1.0
    with pytest.raises(gateway.GatewayError, match="请求失败"):
        gateway.fetch_models(CLIENT_KEY, base_url=fake_gateway.base_url)


def test_request_timeout_is_two_seconds() -> None:
    assert gateway.REQUEST_TIMEOUT_SECONDS == 2


def test_fetch_model_definitions_uses_management_key(
    fake_gateway: _FakeGateway,
) -> None:
    fake_gateway.definitions = {"models": [{"id": "model-a", "context_length": 272000}]}
    payload = gateway.fetch_model_definitions(
        MANAGEMENT_KEY, "codex", base_url=fake_gateway.base_url
    )
    assert payload == fake_gateway.definitions
    assert fake_gateway.requests == [
        ("/v0/management/model-definitions/codex", f"Bearer {MANAGEMENT_KEY}")
    ]


@pytest.mark.parametrize(("status", "body"), [(403, b"{}"), (200, b"[1, 2]")])
def test_fetch_model_definitions_failures_raise_without_key(
    fake_gateway: _FakeGateway, status: int, body: bytes
) -> None:
    fake_gateway.status = status
    fake_gateway.body = body
    with pytest.raises(gateway.GatewayError) as caught:
        gateway.fetch_model_definitions(
            MANAGEMENT_KEY, "antigravity", base_url=fake_gateway.base_url
        )
    _assert_no_key(str(caught.value))


# -----------------------------------------------------------------------------
# 等待注册


def test_wait_for_models_succeeds_after_delayed_registration(
    fake_gateway: _FakeGateway,
) -> None:
    fake_gateway.models = ["model-a"]
    fake_gateway.late_models = ["alpha/vendor-model-b"]
    fake_gateway.late_after = 3
    gateway.wait_for_models(
        ["model-a", "alpha/vendor-model-b"],
        CLIENT_KEY,
        attempts=5,
        interval=0.01,
        base_url=fake_gateway.base_url,
    )
    assert len(fake_gateway.requests) == 3


def test_wait_for_models_times_out_with_restart_hint(
    fake_gateway: _FakeGateway,
) -> None:
    fake_gateway.models = ["model-a"]
    with pytest.raises(gateway.GatewayError) as caught:
        gateway.wait_for_models(
            ["model-a", "beta/model-d"],
            CLIENT_KEY,
            attempts=3,
            interval=0.01,
            base_url=fake_gateway.base_url,
        )
    message = str(caught.value)
    assert "beta/model-d" in message
    assert "claudex gateway restart" in message
    assert len(fake_gateway.requests) == 3
    _assert_no_key(message)


def test_wait_for_models_reports_last_query_failure() -> None:
    with pytest.raises(gateway.GatewayError, match="最后一次查询失败") as caught:
        gateway.wait_for_models(
            ["model-a"],
            CLIENT_KEY,
            attempts=2,
            interval=0.01,
            base_url=_closed_port_url(),
        )
    _assert_no_key(str(caught.value))


def test_wait_for_models_with_nothing_expected_sends_no_request(
    fake_gateway: _FakeGateway,
) -> None:
    gateway.wait_for_models(
        [], CLIENT_KEY, attempts=1, interval=0.01, base_url=fake_gateway.base_url
    )
    assert fake_gateway.requests == []


def test_wait_for_models_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError, match="attempts"):
        gateway.wait_for_models(["model-a"], CLIENT_KEY, attempts=0)


def test_wait_defaults_are_named_constants() -> None:
    assert (gateway.WAIT_ATTEMPTS, gateway.WAIT_INTERVAL_SECONDS) == (20, 0.5)
