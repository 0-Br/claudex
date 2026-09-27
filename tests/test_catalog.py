"""claudex.catalog：OpenRouter 目录的抓取与缓存、元数据取值、计价与 ≈ 判定。

抓取用 `http.server` 在 127.0.0.1 临时端口上起的假目录服务；`ensure_catalog` 与
`refresh_catalog_if_stale` 的用例用 monkeypatch 替换 `claudex.catalog.fetch_catalog`。
时间一律经 `now` 参数注入。
"""

import json
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claudex import catalog, paths
from claudex.catalog import Catalog, CatalogEntry, CatalogError, ModelMeta, Pricing
from claudex.config import Config, ModelRef, parse_config, resolve_ref

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
PROXY_ENV = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)

PRICE_C = Pricing(
    prompt=0.000002,
    completion=0.00001,
    input_cache_read=0.0000002,
    input_cache_write=0.0000025,
)
PRICE_F = Pricing(
    prompt=0.000001, completion=0.000004, input_cache_read=None, input_cache_write=None
)


def _config() -> Config:
    return parse_config(
        {
            "default_profile": "daily",
            "sources": {
                "sub": {
                    "type": "codex",
                    "models": [
                        "model-a",
                        {
                            "id": "model-b",
                            "context": 900000,
                            "efforts": ["low", "high"],
                            "display": "Model B",
                            "openrouter": "vendor/model-b",
                        },
                        {"id": "model-j", "openrouter": "vendor/model-c"},
                    ],
                },
                "ag": {"type": "antigravity", "models": ["model-g"]},
                "or": {
                    "type": "openrouter",
                    "models": [
                        "vendor/model-c",
                        "vendor/model-d",
                        {"id": "vendor/model-e", "display": "Custom E"},
                        "vendor/model-x",
                    ],
                },
                "beta": {
                    "type": "openai",
                    "base_url": "https://example.invalid/v1",
                    "models": [
                        "model-h",
                        {"id": "model-i", "openrouter": "vendor/model-c"},
                    ],
                },
                "plan": {
                    "type": "anthropic",
                    "base_url": "https://example.invalid/anthropic",
                    "models": [
                        {"id": "model-f", "openrouter": "vendor/model-f"},
                        {"id": "model-k", "context": 128000, "efforts": ["medium"]},
                    ],
                },
            },
            "profiles": {
                "daily": {
                    "fable": "or/vendor/model-c",
                    "opus": "sub/model-a",
                    "sonnet": "plan/model-f",
                    "haiku": "beta/model-h",
                },
            },
        }
    )


def _ref(text: str) -> ModelRef:
    return resolve_ref(_config(), text)


def _catalog(fetched_at: datetime = NOW) -> Catalog:
    return Catalog(
        fetched_at=fetched_at,
        models={
            "vendor/model-c": CatalogEntry(
                name="Vendor: Model C",
                context_length=400000,
                supported_parameters=("tools", "reasoning", "include_reasoning"),
                pricing=PRICE_C,
            ),
            "vendor/model-d": CatalogEntry(
                name="Vendor: Model D",
                context_length=131072,
                supported_parameters=("tools", "reasoning_effort"),
                pricing=None,
            ),
            "vendor/model-e": CatalogEntry(
                name="Vendor: Model E",
                context_length=None,
                supported_parameters=(),
                pricing=PRICE_C,
            ),
            "vendor/model-f": CatalogEntry(
                name="Vendor: Model F",
                context_length=200000,
                supported_parameters=("reasoning",),
                pricing=PRICE_F,
            ),
        },
    )


def _definitions() -> dict[str, dict[str, object]]:
    return {
        "codex": {
            "models": [
                {
                    "id": "model-a[1m]",
                    "context_length": 272000,
                    "thinking": {"levels": ["low", "medium", "high", "xhigh"]},
                },
                {"id": "model-j", "context_length": 64000, "thinking": {"levels": []}},
            ]
        },
        "antigravity": {
            "models": [
                {"id": "model-g(high)", "context_length": 1048576},
            ]
        },
    }


# -----------------------------------------------------------------------------
# 假目录服务


@dataclass
class _FakeCatalog:
    base_url: str
    status: int = 200
    body: bytes = b""
    requests: list[tuple[str, str]] = field(default_factory=list)


def _payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "vendor/model-c",
                "name": "Vendor: Model C",
                "context_length": 400000,
                "supported_parameters": ["tools", "reasoning"],
                "pricing": {
                    "prompt": "0.000002",
                    "completion": "0.00001",
                    "input_cache_read": "0.0000002",
                    "input_cache_write": "0.0000025",
                    "request": "0",
                },
            },
            {
                "id": "vendor/model-f",
                "name": "Vendor: Model F",
                "context_length": 200000,
                "supported_parameters": ["reasoning"],
                "pricing": {"prompt": "0.000001", "completion": "0.000004"},
            },
            {
                "id": "vendor/router",
                "name": "Vendor: Router",
                "context_length": None,
                "pricing": {"prompt": "-1", "completion": "-1"},
            },
            {"id": "vendor/bare"},
        ]
    }


def _handler_for(state: _FakeCatalog) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append((self.path, self.headers.get("User-Agent", "")))
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, format: str, *args: object) -> None:
            # 不向测试输出写访问日志
            del format, args

    return Handler


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeCatalog]:
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    state = _FakeCatalog(base_url="", body=json.dumps(_payload()).encode())
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


def _replace_fetch(
    monkeypatch: pytest.MonkeyPatch, result: Catalog | CatalogError
) -> list[datetime | None]:
    """把 `claudex.catalog.fetch_catalog` 换成返回 `result`（或抛出它）的替身。"""
    calls: list[datetime | None] = []

    def fake_fetch(*, now: datetime | None = None, url: str = "") -> Catalog:
        del url
        calls.append(now)
        if isinstance(result, CatalogError):
            raise result
        return result

    monkeypatch.setattr(catalog, "fetch_catalog", fake_fetch)
    return calls


# -----------------------------------------------------------------------------
# 抓取


def test_fetch_catalog_parses_entries_and_prices(fake_catalog: _FakeCatalog) -> None:
    fetched = catalog.fetch_catalog(
        now=NOW.replace(microsecond=123456), url=f"{fake_catalog.base_url}/models"
    )
    assert fetched.fetched_at == NOW
    assert fetched.models["vendor/model-c"] == CatalogEntry(
        name="Vendor: Model C",
        context_length=400000,
        supported_parameters=("tools", "reasoning"),
        pricing=PRICE_C,
    )
    assert fetched.models["vendor/model-f"].pricing == PRICE_F
    assert fetched.models["vendor/router"] == CatalogEntry(
        name="Vendor: Router",
        context_length=None,
        supported_parameters=(),
        pricing=None,
    )
    assert fetched.models["vendor/bare"] == CatalogEntry(
        name=None, context_length=None, supported_parameters=(), pricing=None
    )
    assert fake_catalog.requests[0][1].startswith("claudex/")


def test_fetch_catalog_goes_through_environment_proxy(
    fake_catalog: _FakeCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", fake_catalog.base_url)
    monkeypatch.setenv("http_proxy", fake_catalog.base_url)
    catalog.fetch_catalog(now=NOW, url="http://catalog.example.invalid/api/v1/models")
    assert fake_catalog.requests[0][0] == "http://catalog.example.invalid/api/v1/models"


def test_fetch_timeout_is_ten_seconds() -> None:
    assert catalog.FETCH_TIMEOUT_SECONDS == 10


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, b"{}"),
        (200, b"not json"),
        (200, b'{"data": {}}'),
        (200, b"[]"),
    ],
)
def test_fetch_catalog_failures_raise_catalog_error(
    fake_catalog: _FakeCatalog, status: int, body: bytes
) -> None:
    fake_catalog.status = status
    fake_catalog.body = body
    with pytest.raises(CatalogError):
        catalog.fetch_catalog(now=NOW, url=fake_catalog.base_url)


@pytest.mark.parametrize(
    ("bad", "recorded"),
    [
        ({"id": "vendor/x", "context_length": "big"}, "vendor/x"),
        ({"id": "vendor/x", "pricing": {"prompt": "0.1"}}, "vendor/x"),
        ({"id": "vendor/x", "pricing": {"prompt": "a", "completion": "1"}}, "vendor/x"),
        ({"id": "vendor/x", "supported_parameters": "tools"}, "vendor/x"),
        ({"id": "vendor/x", "name": 3}, "vendor/x"),
        ({"name": "no id"}, "data[1]"),
        ("not an object", "data[1]"),
    ],
)
def test_malformed_entry_is_skipped_and_recorded(
    fake_catalog: _FakeCatalog, bad: object, recorded: str
) -> None:
    payload = _payload()
    data = payload["data"]
    assert isinstance(data, list)
    data.insert(1, bad)
    fake_catalog.body = json.dumps(payload).encode()
    fetched = catalog.fetch_catalog(now=NOW, url=fake_catalog.base_url)
    assert fetched.skipped == (recorded,)
    assert set(fetched.models) == {
        "vendor/model-c",
        "vendor/model-f",
        "vendor/router",
        "vendor/bare",
    }
    assert fetched.models["vendor/model-c"].pricing == PRICE_C


def test_skipped_slugs_are_sorted_and_cached(fake_catalog: _FakeCatalog) -> None:
    payload = _payload()
    data = payload["data"]
    assert isinstance(data, list)
    data.extend(
        [
            {"id": "vendor/zeta", "context_length": -1},
            {"id": "vendor/alpha", "context_length": "x"},
        ]
    )
    fake_catalog.body = json.dumps(payload).encode()
    fetched = catalog.fetch_catalog(now=NOW, url=fake_catalog.base_url)
    assert fetched.skipped == ("vendor/alpha", "vendor/zeta")
    catalog.save_catalog(fetched)
    cached = json.loads(paths.catalog_file().read_text(encoding="utf-8"))
    assert cached["skipped"] == ["vendor/alpha", "vendor/zeta"]
    assert catalog.load_catalog() == fetched


def test_fetch_catalog_unreachable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()
    with pytest.raises(CatalogError, match="请求失败"):
        catalog.fetch_catalog(now=NOW, url=f"http://127.0.0.1:{port}/models")


# -----------------------------------------------------------------------------
# 缓存


def test_save_and_load_round_trip() -> None:
    original = _catalog()
    assert catalog.save_catalog(original) is True
    assert catalog.load_catalog() == original
    assert catalog.save_catalog(original) is False


def test_saved_file_shape_and_mode() -> None:
    catalog.save_catalog(_catalog())
    path = paths.catalog_file()
    assert path.name == "catalog.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["fetched_at"] == "2026-03-01T12:00:00Z"
    assert data["models"]["vendor/model-f"] == {
        "name": "Vendor: Model F",
        "context_length": 200000,
        "supported_parameters": ["reasoning"],
        "pricing": {
            "prompt": 0.000001,
            "completion": 0.000004,
            "input_cache_read": None,
            "input_cache_write": None,
        },
    }
    assert data["models"]["vendor/model-d"]["pricing"] is None
    assert data["skipped"] == []


def test_load_catalog_without_file_is_none() -> None:
    assert catalog.load_catalog() is None


def _cache_with(mutate: Callable[[dict[str, object]], None]) -> None:
    catalog.save_catalog(_catalog())
    data: dict[str, object] = json.loads(
        paths.catalog_file().read_text(encoding="utf-8")
    )
    mutate(data)
    paths.catalog_file().write_text(json.dumps(data), encoding="utf-8")


def _entry(data: dict[str, object]) -> dict[str, object]:
    models = data["models"]
    assert isinstance(models, dict)
    entry = models["vendor/model-c"]
    assert isinstance(entry, dict)
    return entry


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data.update(schema=2), id="schema"),
        pytest.param(lambda data: data.pop("schema"), id="no-schema"),
        pytest.param(lambda data: data.update(fetched_at="yesterday"), id="time"),
        pytest.param(lambda data: data.update(models=[]), id="models-list"),
        pytest.param(lambda data: _entry(data).pop("name"), id="entry-key"),
        pytest.param(lambda data: _entry(data).update(context_length=0), id="context"),
        pytest.param(
            lambda data: _entry(data).update(pricing={"prompt": 1.0}), id="pricing"
        ),
        pytest.param(
            lambda data: _entry(data).update(supported_parameters="reasoning"),
            id="parameters",
        ),
        pytest.param(lambda data: data.pop("skipped"), id="no-skipped"),
        pytest.param(lambda data: data.update(skipped="vendor/x"), id="skipped"),
    ],
)
def test_load_catalog_rejects_malformed_cache(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    _cache_with(mutate)
    with pytest.raises(ValueError, match=r"；运行 claudex update 重新抓取$") as caught:
        catalog.load_catalog()
    assert str(paths.catalog_file()) in str(caught.value)


def test_load_catalog_rejects_non_json() -> None:
    paths.catalog_file().parent.mkdir(parents=True, exist_ok=True)
    paths.catalog_file().write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON") as caught:
        catalog.load_catalog()
    assert str(caught.value).endswith("；运行 claudex update 重新抓取")


def test_ensure_catalog_uses_cache_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog.save_catalog(_catalog())
    calls = _replace_fetch(monkeypatch, CatalogError("不应抓取"))
    assert catalog.ensure_catalog(now=NOW) == _catalog()
    assert calls == []


def test_ensure_catalog_fetches_and_saves_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _replace_fetch(monkeypatch, _catalog())
    assert catalog.ensure_catalog(now=NOW) == _catalog()
    assert calls == [NOW]
    assert catalog.load_catalog() == _catalog()


def test_ensure_catalog_without_cache_propagates_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_fetch(monkeypatch, CatalogError("离线"))
    with pytest.raises(CatalogError, match="离线"):
        catalog.ensure_catalog(now=NOW)
    assert not paths.catalog_file().exists()


def test_refresh_skips_fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog.save_catalog(_catalog(fetched_at=NOW - timedelta(hours=23)))
    calls = _replace_fetch(monkeypatch, _catalog())
    assert catalog.refresh_catalog_if_stale(timedelta(hours=24), now=NOW) is False
    assert calls == []


def test_refresh_fetches_stale_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog.save_catalog(_catalog(fetched_at=NOW - timedelta(hours=24)))
    calls = _replace_fetch(monkeypatch, _catalog(fetched_at=NOW))
    assert catalog.refresh_catalog_if_stale(timedelta(hours=24), now=NOW) is True
    assert calls == [NOW]
    loaded = catalog.load_catalog()
    assert loaded is not None
    assert loaded.fetched_at == NOW


def test_refresh_fetches_when_cache_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _replace_fetch(monkeypatch, _catalog())
    assert catalog.refresh_catalog_if_stale(timedelta(hours=24), now=NOW) is True
    assert catalog.load_catalog() == _catalog()


def test_refresh_failure_keeps_old_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog.save_catalog(_catalog(fetched_at=NOW - timedelta(days=3)))
    before = paths.catalog_file().read_bytes()
    _replace_fetch(monkeypatch, CatalogError("上游 503"))
    with pytest.raises(CatalogError, match="503"):
        catalog.refresh_catalog_if_stale(timedelta(hours=24), now=NOW)
    assert paths.catalog_file().read_bytes() == before


# -----------------------------------------------------------------------------
# 元数据


def test_explicit_values_win_for_subscription_model() -> None:
    meta = catalog.model_metadata(_ref("sub/model-b"), _catalog(), {})
    assert meta == ModelMeta(context=900000, efforts=("low", "high"), display="Model B")


def test_explicit_values_win_for_generic_model() -> None:
    meta = catalog.model_metadata(_ref("plan/model-k"), _catalog(), {})
    assert meta == ModelMeta(context=128000, efforts=("medium",), display="model-k")


def test_subscription_model_takes_gateway_definition() -> None:
    meta = catalog.model_metadata(_ref("sub/model-a"), _catalog(), _definitions())
    assert meta == ModelMeta(
        context=272000,
        efforts=("low", "medium", "high", "xhigh"),
        display="model-a",
    )


def test_definition_id_suffixes_are_ignored() -> None:
    meta = catalog.model_metadata(_ref("ag/model-g"), _catalog(), _definitions())
    assert meta == ModelMeta(context=1048576, efforts=None, display="model-g")


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([{"id": "model-a"}], 0),
        ([{"id": "model-a[1m]"}], 0),
        ([{"id": "model-a[1M][1m]"}], 0),
        ([{"id": "model-a(high)"}], 0),
        ([{"id": "model-a[1m]", "n": 1}, {"id": "model-a", "n": 2}], 1),
        ([{"id": 7}, {"id": "model-a"}], 1),
        ([{"id": "model-ab"}], None),
        ([], None),
    ],
)
def test_match_definition(
    entries: list[dict[str, object]], expected: int | None
) -> None:
    found = catalog.match_definition("model-a", entries)
    assert found is (None if expected is None else entries[expected])


def test_subscription_display_falls_back_to_catalog_name() -> None:
    meta = catalog.model_metadata(_ref("sub/model-j"), _catalog(), _definitions())
    assert meta == ModelMeta(context=64000, efforts=None, display="Vendor: Model C")


def test_subscription_model_absent_from_definitions_has_no_metadata() -> None:
    definitions = _definitions()
    definitions["codex"] = {"models": []}
    meta = catalog.model_metadata(_ref("sub/model-a"), _catalog(), definitions)
    assert meta == ModelMeta(context=None, efforts=None, display="model-a")


def test_missing_definition_channel_raises() -> None:
    with pytest.raises(ValueError, match="antigravity"):
        catalog.model_metadata(_ref("ag/model-g"), _catalog(), {"codex": {}})


def test_malformed_definitions_raise() -> None:
    with pytest.raises(ValueError, match="models"):
        catalog.model_metadata(_ref("sub/model-a"), _catalog(), {"codex": {}})


def test_generic_model_takes_catalog_entry_with_reasoning() -> None:
    meta = catalog.model_metadata(_ref("or/vendor/model-c"), _catalog(), {})
    assert meta == ModelMeta(
        context=400000, efforts=("low", "medium", "high"), display="Vendor: Model C"
    )


@pytest.mark.parametrize(
    ("parameters", "efforts"),
    [
        (("tools", "reasoning"), ("low", "medium", "high")),
        (("tools", "reasoning_effort"), ("low", "medium", "high")),
        (("reasoning", "reasoning_effort"), ("low", "medium", "high")),
        (("tools", "include_reasoning"), None),
        ((), None),
    ],
)
def test_reasoning_parameters_decide_default_efforts(
    parameters: tuple[str, ...], efforts: tuple[str, ...] | None
) -> None:
    base = _catalog()
    models = dict(base.models)
    models["vendor/model-d"] = CatalogEntry(
        name="Vendor: Model D",
        context_length=131072,
        supported_parameters=parameters,
        pricing=None,
    )
    meta = catalog.model_metadata(
        _ref("or/vendor/model-d"), Catalog(fetched_at=NOW, models=models), {}
    )
    assert meta == ModelMeta(context=131072, efforts=efforts, display="Vendor: Model D")


def test_explicit_display_beats_catalog_name() -> None:
    meta = catalog.model_metadata(_ref("or/vendor/model-e"), _catalog(), {})
    assert meta == ModelMeta(context=None, efforts=None, display="Custom E")


def test_generic_model_with_slug_missing_from_catalog() -> None:
    meta = catalog.model_metadata(_ref("or/vendor/model-x"), _catalog(), {})
    assert meta == ModelMeta(context=None, efforts=None, display="vendor/model-x")


def test_generic_model_without_slug_has_only_id() -> None:
    meta = catalog.model_metadata(_ref("beta/model-h"), _catalog(), {})
    assert meta == ModelMeta(context=None, efforts=None, display="model-h")


def test_non_openrouter_generic_model_uses_its_slug() -> None:
    meta = catalog.model_metadata(_ref("plan/model-f"), _catalog(), {})
    assert meta == ModelMeta(
        context=200000, efforts=("low", "medium", "high"), display="Vendor: Model F"
    )


# -----------------------------------------------------------------------------
# 计价


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("or/vendor/model-c", PRICE_C),
        ("beta/model-i", PRICE_C),
        ("plan/model-f", PRICE_F),
        ("sub/model-j", PRICE_C),
        ("or/vendor/model-d", None),
        ("or/vendor/model-x", None),
        ("beta/model-h", None),
        ("sub/model-a", None),
    ],
)
def test_pricing_for(ref: str, expected: Pricing | None) -> None:
    assert catalog.pricing_for(_ref(ref), _catalog()) == expected


def test_usage_cost_includes_cache_read_and_write() -> None:
    usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 5000,
        "cache_creation_input_tokens": 400,
    }
    expected = 1000 * 0.000002 + 200 * 0.00001 + 5000 * 0.0000002 + 400 * 0.0000025
    assert catalog.usage_cost_usd(usage, PRICE_C) == pytest.approx(expected)


def test_missing_cache_prices_fall_back_to_prompt_price() -> None:
    usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 5000,
        "cache_creation_input_tokens": 400,
    }
    expected = (1000 + 5000 + 400) * 0.000001 + 200 * 0.000004
    assert catalog.usage_cost_usd(usage, PRICE_F) == pytest.approx(expected)


def test_missing_or_invalid_usage_fields_count_as_zero() -> None:
    usage: dict[str, object] = {"input_tokens": 1000, "output_tokens": True}
    assert catalog.usage_cost_usd(usage, PRICE_C) == pytest.approx(1000 * 0.000002)


@pytest.mark.parametrize(
    ("ref", "estimated"),
    [
        ("or/vendor/model-c", False),
        ("beta/model-i", True),
        ("plan/model-f", True),
        ("sub/model-j", True),
        ("ag/model-g", True),
    ],
)
def test_is_estimated(ref: str, estimated: bool) -> None:
    assert catalog.is_estimated(_ref(ref)) is estimated
