"""claudex.probe：列上游模型、缺 context 放行与经网关的四项能力判定。

上游与网关都是本地假服务（`fake_service`）：通用来源的 `base_url` 直接指向它，
OpenRouter 经 monkeypatch `gateway.OPENROUTER_BASE_URL` 指向它，能力探测以
`base_url` 参数把网关换成它；订阅来源的模型定义经 monkeypatch
`gateway.fetch_model_definitions` 给出。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_service import FakeService, Request

from claudex import catalog, cli, gateway, paths, probe, render
from claudex.catalog import Catalog
from claudex.config import Config, ConfigError, parse_config
from claudex.gateway import GatewayError
from claudex.probe import ProbeError

CLIENT_KEY = "c0ffee11" * 8
MANAGEMENT_KEY = "0badf00d" * 8
ALPHA_KEY = "sk-FAKE-alpha-9z8x7c"
ACCEPTED_EFFORTS = {"low", "high"}
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def _write_key(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n", encoding="utf-8")
    path.chmod(mode)


def _config(base_url: str, source_type: str = "openai") -> Config:
    return parse_config(
        {
            "default_profile": "daily",
            "sources": {
                "alpha": {
                    "type": source_type,
                    "base_url": base_url,
                    "models": ["model-x", {"id": "model-y", "context": 64000}],
                },
                "beta": {
                    "type": "openai",
                    "base_url": "https://example.invalid/v1",
                    "models": [{"id": "model-b", "context": 64000}],
                },
                "sub": {"type": "codex", "models": ["model-s"]},
            },
            "profiles": {
                "daily": {
                    "fable": "alpha/model-x",
                    "opus": "alpha/model-y",
                    "sonnet": "beta/model-b",
                    "haiku": "beta/model-b",
                }
            },
        }
    )


def _all_keys() -> None:
    _write_key(paths.client_key_file(), CLIENT_KEY)
    _write_key(paths.management_key_file(), MANAGEMENT_KEY)
    _write_key(paths.source_key_file("alpha"), ALPHA_KEY)
    _write_key(paths.source_key_file("beta"), "sk-FAKE-beta-1q2w3e")


# -----------------------------------------------------------------------------
# 列上游模型


def test_list_openai_uses_base_url_and_only_this_key(
    fake_service: FakeService,
) -> None:
    fake_service.reply("GET", "/v1/models", {"data": [{"id": "z"}, {"id": "model-x"}]})
    _write_key(paths.source_key_file("alpha"), ALPHA_KEY)
    listed = probe.list_upstream_models(_config(f"{fake_service.url}/v1"), "alpha")
    assert listed == ["model-x", "z"]
    assert fake_service.requests[0].headers["authorization"] == f"Bearer {ALPHA_KEY}"


def test_list_openrouter_uses_openrouter_models(
    monkeypatch: pytest.MonkeyPatch, fake_service: FakeService
) -> None:
    fake_service.reply("GET", "/api/v1/models", {"data": [{"id": "vendor/model-c"}]})
    monkeypatch.setattr(gateway, "OPENROUTER_BASE_URL", f"{fake_service.url}/api/v1")
    config = parse_config(
        {
            "default_profile": "daily",
            "sources": {"or": {"type": "openrouter", "models": ["vendor/model-c"]}},
            "profiles": {
                "daily": dict.fromkeys(
                    ("fable", "opus", "sonnet", "haiku"), "or/vendor/model-c"
                )
            },
        }
    )
    _write_key(paths.source_key_file("or"), "sk-FAKE-or-5t6y7u")
    assert probe.list_upstream_models(config, "or") == ["vendor/model-c"]


def test_list_anthropic_sends_api_key_header(fake_service: FakeService) -> None:
    fake_service.reply("GET", "/v1/models", {"data": [{"id": "model-x"}]})
    _write_key(paths.source_key_file("alpha"), ALPHA_KEY)
    config = _config(fake_service.url, "anthropic")
    assert probe.list_upstream_models(config, "alpha") == ["model-x"]
    headers = fake_service.requests[0].headers
    assert headers["x-api-key"] == ALPHA_KEY
    assert "authorization" not in headers


def test_list_anthropic_without_models_endpoint_explains(
    fake_service: FakeService,
) -> None:
    _write_key(paths.source_key_file("alpha"), ALPHA_KEY)
    with pytest.raises(ProbeError, match="可能不提供模型列表接口"):
        probe.list_upstream_models(_config(fake_service.url, "anthropic"), "alpha")


def test_list_requires_this_sources_key(fake_service: FakeService) -> None:
    with pytest.raises(ProbeError, match="claudex key set alpha"):
        probe.list_upstream_models(_config(f"{fake_service.url}/v1"), "alpha")
    _write_key(paths.source_key_file("alpha"), ALPHA_KEY, mode=0o644)
    with pytest.raises(ProbeError, match="0600"):
        probe.list_upstream_models(_config(f"{fake_service.url}/v1"), "alpha")
    _write_key(paths.source_key_file("alpha"), ALPHA_KEY + "\r")
    with pytest.raises(ProbeError, match="格式不对"):
        probe.list_upstream_models(_config(f"{fake_service.url}/v1"), "alpha")
    assert not fake_service.requests


def test_list_subscription_reads_gateway_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _all_keys()
    channels: list[str] = []

    def fake_definitions(management_key: str, channel: str) -> dict[str, object]:
        assert management_key == MANAGEMENT_KEY
        channels.append(channel)
        return {"models": [{"id": "model-t"}, {"id": "model-s"}]}

    monkeypatch.setattr(gateway, "fetch_model_definitions", fake_definitions)
    config = _config("https://example.invalid/v1")
    assert probe.list_upstream_models(config, "sub") == ["model-s", "model-t"]
    assert channels == ["codex"]


def test_list_subscription_without_gateway_hints_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _all_keys()

    def unreachable(management_key: str, channel: str) -> dict[str, object]:
        del management_key, channel
        raise GatewayError("网关不可达")

    monkeypatch.setattr(gateway, "fetch_model_definitions", unreachable)
    with pytest.raises(ProbeError, match="claudex gateway start"):
        probe.list_upstream_models(_config("https://example.invalid/v1"), "sub")


def test_cli_probe_prints_upstream_models(
    fake_service: FakeService, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_service.reply("GET", "/v1/models", {"data": [{"id": "model-x"}]})
    _write_key(paths.source_key_file("alpha"), ALPHA_KEY)
    base_url = f"{fake_service.url}/v1"
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        f"""\
default_profile = "daily"

[sources.alpha]
type = "openai"
base_url = "{base_url}"
models = ["model-x"]

[profiles.daily]
fable = "alpha/model-x"
opus = "alpha/model-x"
sonnet = "alpha/model-x"
haiku = "alpha/model-x"
""",
        encoding="utf-8",
    )
    assert cli.main(["probe", "alpha"]) == 0
    assert capsys.readouterr().out == "model-x\n"


# -----------------------------------------------------------------------------
# 缺 context 放行只在 probe 路径


def test_missing_context_passes_gateway_config_but_not_preflight() -> None:
    _all_keys()
    paths.gateway_base_file().write_text("{}\n", encoding="utf-8")
    paths.settings_base_file().write_text("{}\n", encoding="utf-8")
    catalog.save_catalog(Catalog(fetched_at=NOW, models={}))
    config = _config("https://example.invalid/v1")
    assert cli.prepare_gateway_config(config) is True
    report = render.preflight(config, profile="daily", check_gateway=False)
    assert [
        item.model for item in report.errors if item.category == "context_missing"
    ] == ["alpha/model-x"]


# -----------------------------------------------------------------------------
# 经网关探测


def _gateway_messages(request: Request) -> tuple[int, object]:
    body = request.body
    assert isinstance(body, dict)
    assert request.headers["authorization"] == f"Bearer {CLIENT_KEY}"
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        accepted = output_config.get("effort") in ACCEPTED_EFFORTS
        return (200, {"content": []}) if accepted else (400, {"error": "effort"})
    messages = body["messages"]
    assert isinstance(messages, list)
    content = messages[0]["content"]
    if isinstance(content, list):
        return 400, {"error": "image not supported"}
    if "tools" in body:
        return 200, {"content": [{"type": "tool_use", "name": "echo", "input": {}}]}
    return 200, {"content": [{"type": "text", "text": "OK"}]}


def _fake_gateway(service: FakeService) -> None:
    service.reply("GET", "/v1/models", {"data": [{"id": "alpha/model-x"}]})
    service.routes[("POST", "/v1/messages")] = _gateway_messages


def test_probe_judges_four_capabilities_without_context(
    fake_service: FakeService,
) -> None:
    _all_keys()
    _fake_gateway(fake_service)
    config = _config("https://example.invalid/v1")
    [result] = probe.probe_models(
        config, "alpha", ["model-x"], base_url=fake_service.url
    )
    assert result.answered is True
    assert result.tools is True
    assert result.efforts == {
        "minimal": False,
        "low": True,
        "medium": False,
        "high": True,
        "xhigh": False,
        "max": False,
    }
    assert result.image is False
    models = {
        request.body["model"]
        for request in fake_service.requests
        if isinstance(request.body, dict)
    }
    assert models == {"alpha/model-x"}
    lines = probe.format_result(result)
    assert lines[-1] == (
        '  建议：{ id = "model-x", context = <查上游文档后填写>, '
        'efforts = ["low", "high"] }'
    )


def test_probe_stops_after_failed_answer(fake_service: FakeService) -> None:
    _all_keys()
    fake_service.reply("GET", "/v1/models", {"data": [{"id": "alpha/model-x"}]})
    fake_service.reply("POST", "/v1/messages", {"error": "down"}, status=500)
    config = _config("https://example.invalid/v1")
    [result] = probe.probe_models(
        config, "alpha", ["model-x"], base_url=fake_service.url
    )
    assert result.answered is False
    assert result.tools is None
    assert result.efforts == {}
    assert result.image is None
    assert len([r for r in fake_service.requests if r.method == "POST"]) == 1
    assert "应答：失败" in probe.format_result(result)[1]


def test_probe_requires_listed_model(fake_service: FakeService) -> None:
    _all_keys()
    _fake_gateway(fake_service)
    config = _config("https://example.invalid/v1")
    with pytest.raises(ConfigError):
        probe.probe_models(config, "alpha", ["model-q"], base_url=fake_service.url)


def test_probe_requires_model_in_gateway(fake_service: FakeService) -> None:
    _all_keys()
    _fake_gateway(fake_service)
    config = _config("https://example.invalid/v1")
    with pytest.raises(ProbeError, match="claudex gateway restart"):
        probe.probe_models(config, "alpha", ["model-y"], base_url=fake_service.url)


def test_probe_rejects_subscription_source(fake_service: FakeService) -> None:
    _all_keys()
    config = _config("https://example.invalid/v1")
    with pytest.raises(ProbeError, match="订阅来源"):
        probe.probe_models(config, "sub", ["model-s"], base_url=fake_service.url)
    assert not fake_service.requests
