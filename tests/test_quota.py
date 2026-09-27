"""claudex.quota：三种额度解析、凭据选择、冷却投影、last-good、错误分类、换域名、
网关新版记录与 refresh.json 的读改写。

外部请求一律经 monkeypatch 替换 `claudex.quota.request_json`（按 method 与 URL 路由到
合成应答）；目录刷新替换 `claudex.catalog.refresh_catalog_if_stale`。只有
`request_json` 本身的用例在 127.0.0.1 临时端口上起假服务。时间一律经 `now` 注入。
"""

import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from claudex import catalog, jsonio, paths, quota, statusline
from claudex.quota import RefreshError, RequestError

NOW = 1_800_000_000
CLIENT_KEY = "c0ffee11" * 8
MANAGEMENT_KEY = "0badf00d" * 8
OR_KEY = "sk-FAKE-or-3k9j2h"
PLAN_KEY = "sk-FAKE-plan-6u1i5o"
ALL_KEYS = (CLIENT_KEY, MANAGEMENT_KEY, OR_KEY, PLAN_KEY)
GATEWAY = "http://127.0.0.1:8317"
AUTH_FILES = f"{GATEWAY}/v0/management/auth-files"
API_CALL = f"{GATEWAY}/v0/management/api-call"
CONFIG_TOML = """\
default_profile = "daily"

[sources.sub]
type = "codex"
models = ["model-a"]

[sources.ag]
type = "antigravity"
models = ["gemini-x"]

[sources.or]
type = "openrouter"
models = ["vendor/model-c"]

[sources.plan]
type = "anthropic"
base_url = "https://example.invalid/anthropic"
models = [{ id = "model-f", context = 128000 }]

[profiles.daily]
fable = "or/vendor/model-c"
opus = "sub/model-a"
sonnet = "plan/model-f"
haiku = "ag/gemini-x"
"""


def _codex_usage(used: float = 12.0) -> dict[str, object]:
    return {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "limit_window_seconds": 604800,
                "used_percent": used,
                "reset_at": NOW + 86400,
            },
            "secondary_window": None,
        },
    }


def _antigravity_summary() -> dict[str, object]:
    return {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-5h",
                        "remainingFraction": 0.75,
                        "resetTime": "2027-01-15T08:00:00Z",
                        "window": "5h",
                    },
                    {
                        "bucket_id": "gemini-weekly",
                        "remaining_fraction": 1.5,
                        "reset_time": NOW + 3 * 86400,
                        "window": "weekly",
                    },
                ],
            }
        ]
    }


def _auth_files() -> list[dict[str, object]]:
    return [
        {
            "provider": "codex",
            "disabled": False,
            "auth_index": "idx-codex",
            "account": "user@example.invalid",
            "cooldowns": [
                {
                    "scope": "model",
                    "model_key": "model-a",
                    "reason": "quota",
                    "retry_at": "2027-01-15T08:00:00+00:00",
                    "http_status": 429,
                    "backoff_level": 2,
                }
            ],
        },
        {
            "provider": "antigravity",
            "disabled": False,
            "auth_index": "idx-ag",
            "email": "user@example.invalid",
            "project_id": "project-1",
            "cooldowns": None,
        },
    ]


Responder = Callable[[str, str, Mapping[str, str], Mapping[str, object] | None], object]


@dataclass
class _Net:
    """替身网络：`routes` 以 (method, url) 为键，api-call 以上游 URL 为键。"""

    routes: dict[tuple[str, str], object] = field(default_factory=dict)
    upstream: dict[str, object] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, str], object]] = field(default_factory=list)

    def request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None = None,
        *,
        timeout: float,
    ) -> dict[str, object]:
        del timeout
        self.calls.append((method, url, dict(headers), body))
        if url == API_CALL:
            assert body is not None
            target = str(body["url"])
            result = self.upstream[target]
            if isinstance(result, list):
                result = result.pop(0)
        else:
            result = self.routes[(method, url)]
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, dict)
        return result

    def upstream_urls(self) -> list[str]:
        return [
            str(body["url"])
            for _method, url, _headers, body in self.calls
            if url == API_CALL and isinstance(body, dict)
        ]


def _ok(body: dict[str, object], status: int = 200) -> dict[str, object]:
    return {"status_code": status, "body": json.dumps(body)}


def _write_key(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> _Net:
    """写好配置与 key 文件，替换请求函数与目录刷新，返回可调整的替身网络。"""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(CONFIG_TOML, encoding="utf-8")
    _write_key(paths.client_key_file(), CLIENT_KEY)
    _write_key(paths.management_key_file(), MANAGEMENT_KEY)
    _write_key(paths.source_key_file("or"), OR_KEY)
    _write_key(paths.source_key_file("plan"), PLAN_KEY)
    fake = _Net()
    fake.routes[("GET", AUTH_FILES)] = {"files": _auth_files()}
    fake.routes[("GET", quota.OPENROUTER_CREDITS_URL)] = {
        "data": {"total_credits": 20.0, "total_usage": 7.5}
    }
    fake.routes[("GET", quota.LATEST_RELEASE_URL)] = {"tag_name": "v7.3.20"}
    fake.upstream[quota.CODEX_USAGE_URL] = _ok(_codex_usage())
    fake.upstream[quota.ANTIGRAVITY_QUOTA_URLS[0]] = _ok(_antigravity_summary())
    monkeypatch.setattr(quota, "request_json", fake.request_json)
    monkeypatch.setattr(catalog, "refresh_catalog_if_stale", _catalog_ok)
    return fake


_CATALOG_CALLS: list[tuple[timedelta, datetime | None]] = []


def _catalog_ok(max_age: timedelta, *, now: datetime | None = None) -> bool:
    _CATALOG_CALLS.append((max_age, now))
    return True


@pytest.fixture(autouse=True)
def _reset_catalog_calls() -> Iterator[None]:
    _CATALOG_CALLS.clear()
    yield
    _CATALOG_CALLS.clear()


def _sources() -> dict[str, dict[str, object]]:
    data = jsonio.read_json_object(paths.quota_file())
    assert data is not None
    assert data["schema"] == 1
    sources = data["sources"]
    assert isinstance(sources, dict)
    return sources


def _refresh_record() -> dict[str, object]:
    record = jsonio.read_json_object(paths.refresh_file())
    assert record is not None
    return record


# -----------------------------------------------------------------------------
# 解析


def test_parse_codex_usage_maps_windows() -> None:
    body = _codex_usage()
    rate_limit = body["rate_limit"]
    assert isinstance(rate_limit, dict)
    rate_limit["secondary_window"] = {
        "limit_window_seconds": 18000,
        "used_percent": 40,
        "reset_at": NOW + 600,
    }
    assert quota.parse_codex_usage(body, NOW) == {
        "windows": {
            "seven_day": {"used_percentage": 12.0, "resets_at": NOW + 86400},
            "five_hour": {"used_percentage": 40.0, "resets_at": NOW + 600},
        },
        "plan_type": "pro",
    }


@pytest.mark.parametrize(
    "window",
    [
        None,
        {"limit_window_seconds": 3600, "used_percent": 1, "reset_at": NOW + 10},
        {"limit_window_seconds": 18000, "used_percent": 101, "reset_at": NOW + 10},
        {"limit_window_seconds": 18000, "used_percent": 1, "reset_at": NOW},
    ],
)
def test_parse_codex_usage_rejects_bad_windows(window: object) -> None:
    body = {"rate_limit": {"primary_window": window, "secondary_window": None}}
    with pytest.raises(ValueError, match="window"):
        quota.parse_codex_usage(body, NOW)


def test_parse_antigravity_quota_accepts_both_spellings() -> None:
    parsed = quota.parse_antigravity_quota(_antigravity_summary())
    assert parsed == {
        "buckets": [
            {
                "id": "gemini-5h",
                "display": "Gemini Models",
                "window_raw": "5h",
                "window_seconds": 18000,
                "reset_at": int(
                    datetime.fromisoformat("2027-01-15T08:00:00+00:00").timestamp()
                ),
                "remaining_fraction": 0.75,
            },
            {
                "id": "gemini-weekly",
                "display": "Gemini Models",
                "window_raw": "weekly",
                "window_seconds": 604800,
                "reset_at": NOW + 3 * 86400,
                "remaining_fraction": 1.0,
            },
        ]
    }


def test_parse_antigravity_quota_without_buckets_fails() -> None:
    with pytest.raises(ValueError, match="buckets"):
        quota.parse_antigravity_quota({"groups": [{"buckets": [{"id": "x"}]}]})


@pytest.mark.parametrize(
    "body",
    [
        {"data": {"total_credits": 20, "total_usage": 7.5}},
        {"total_credits": 20, "total_usage": 7.5},
    ],
)
def test_parse_openrouter_credits(body: dict[str, object]) -> None:
    assert quota.parse_openrouter_credits(body) == {
        "total_credits": 20.0,
        "total_usage": 7.5,
    }


def test_parse_openrouter_credits_rejects_missing_field() -> None:
    with pytest.raises(ValueError, match="total_usage"):
        quota.parse_openrouter_credits({"data": {"total_credits": 1}})


# -----------------------------------------------------------------------------
# 凭据选择与冷却投影


def _entry(**fields: object) -> dict[str, object]:
    return {
        "provider": "codex",
        "disabled": False,
        "auth_index": "i1",
        "account": "a@example.invalid",
        **fields,
    }


@pytest.mark.parametrize(
    ("entries", "provider", "kind"),
    [
        ("not a list", "codex", "management_error"),
        ([], "codex", "credential_missing"),
        ([_entry(disabled=True)], "codex", "credential_disabled"),
        ([_entry(auth_index="")], "codex", "credential_incomplete"),
        ([_entry(provider="antigravity")], "antigravity", "credential_incomplete"),
        (
            [_entry(), _entry(auth_index="i2", account="b@example.invalid")],
            "codex",
            "credential_multiple",
        ),
        (
            [
                _entry(provider="antigravity", project_id="p"),
                _entry(provider="antigravity", project_id="p", auth_index="i2"),
            ],
            "antigravity",
            "credential_multiple",
        ),
        (
            [
                _entry(created_at="2026-01-01T00:00:00+00:00"),
                _entry(auth_index="i2", created_at="2026-01-01T00:00:00+00:00"),
            ],
            "codex",
            "credential_multiple",
        ),
    ],
)
def test_select_credential_errors(entries: object, provider: str, kind: str) -> None:
    with pytest.raises(RefreshError) as caught:
        quota.select_credential(entries, provider)
    assert caught.value.kind == kind


def test_select_credential_picks_newest_codex_record() -> None:
    entries = [
        _entry(created_at="2026-01-01T00:00:00+00:00", auth_index="old"),
        _entry(modtime="2026-02-01T00:00:00+00:00", auth_index="new", cooldowns=[]),
    ]
    credential, cooldowns = quota.select_credential(entries, "codex")
    assert credential == {"auth_index": "new", "account": "a@example.invalid"}
    assert cooldowns == []


def test_cooldowns_are_projected() -> None:
    _credential, cooldowns = quota.select_credential(_auth_files(), "codex")
    assert cooldowns == [
        {
            "scope": "model",
            "model_key": "model-a",
            "reason": "quota",
            "retry_at": int(
                datetime.fromisoformat("2027-01-15T08:00:00+00:00").timestamp()
            ),
            "http_status": 429,
        }
    ]
    assert quota.parse_cooldown_views(
        [
            {
                "scope": "credential",
                "reason": "unauthorized",
                "retry_at": "2027-01-01T00:00:00Z",
            }
        ],
        "codex",
    ) == [
        {
            "scope": "credential",
            "model_key": "",
            "reason": "unauthorized",
            "retry_at": int(
                datetime.fromisoformat("2027-01-01T00:00:00+00:00").timestamp()
            ),
            "http_status": None,
        }
    ]
    assert quota.parse_cooldown_views(None, "codex") is None


@pytest.mark.parametrize(
    "raw",
    [
        "x",
        [{"scope": "global", "reason": "q", "retry_at": "2027-01-01T00:00:00Z"}],
        [{"scope": "model", "reason": "", "retry_at": "2027-01-01T00:00:00Z"}],
        [{"scope": "model", "reason": "q", "retry_at": "2027-01-01T00:00:00"}],
        [{"scope": "model", "reason": "q", "retry_at": "soon"}],
    ],
)
def test_invalid_cooldowns_are_response_invalid(raw: object) -> None:
    with pytest.raises(RefreshError) as caught:
        quota.parse_cooldown_views(raw, "codex")
    assert caught.value.kind == "response_invalid"


# -----------------------------------------------------------------------------
# 一次刷新


def test_refresh_once_writes_all_quota_sources(net: _Net) -> None:
    assert quota.refresh_once(now=NOW) == 0
    sources = _sources()
    assert set(sources) == {"sub", "ag", "or"}
    sub = sources["sub"]
    assert sub["ok"] is True
    assert sub["type"] == "codex"
    assert sub["updated_at"] == NOW
    assert sub["error"] is None
    assert sub["data"] == quota.parse_codex_usage(_codex_usage(), NOW)
    cooldowns = sub["cooldowns"]
    assert isinstance(cooldowns, list)
    assert cooldowns[0]["model_key"] == "model-a"
    assert sources["ag"]["data"] == quota.parse_antigravity_quota(
        _antigravity_summary()
    )
    assert sources["ag"]["cooldowns"] is None
    assert sources["or"]["data"] == {"total_credits": 20.0, "total_usage": 7.5}
    assert sources["or"]["cooldowns"] is None
    record = _refresh_record()
    assert record["attempted_at"] == NOW
    assert record["finished_at"] == NOW
    assert record["ok"] is True
    assert record["errors"] == {}


def test_upstream_calls_carry_expected_headers(net: _Net) -> None:
    quota.refresh_once(now=NOW)
    calls = {url: (headers, body) for _m, url, headers, body in net.calls}
    auth_headers, _ = calls[AUTH_FILES]
    assert auth_headers["Authorization"] == f"Bearer {MANAGEMENT_KEY}"
    credits_headers, _ = calls[quota.OPENROUTER_CREDITS_URL]
    assert credits_headers["Authorization"] == f"Bearer {OR_KEY}"
    codex_body = next(
        body
        for _m, url, _h, body in net.calls
        if url == API_CALL
        and isinstance(body, dict)
        and body["url"] == quota.CODEX_USAGE_URL
    )
    assert isinstance(codex_body, dict)
    assert codex_body["auth_index"] == "idx-codex"
    header = codex_body["header"]
    assert isinstance(header, dict)
    assert header["Authorization"] == "Bearer $TOKEN$"
    assert header["ChatGPT-Account-Id"] == "user@example.invalid"


def test_status_line_reads_what_refresh_writes(net: _Net) -> None:
    quota.refresh_once(now=NOW)
    data = quota.load_quota()
    view = statusline.quota_view(data, "sub", "codex", NOW + 30)
    assert view.status == "fresh"
    assert statusline.load_quota() == data


def test_failure_keeps_last_good_data(net: _Net) -> None:
    quota.refresh_once(now=NOW)
    net.upstream[quota.CODEX_USAGE_URL] = _ok({}, status=429)
    later = NOW + 120
    quota.refresh_once(now=later)
    sub = _sources()["sub"]
    assert sub["ok"] is False
    assert sub["error_kind"] == "credential_cooldown"
    assert sub["error"] == "credential cooldown"
    assert sub["failed_at"] == later
    assert sub["updated_at"] == NOW
    assert sub["data"] == quota.parse_codex_usage(_codex_usage(), NOW)
    view = statusline.quota_view(quota.load_quota(), "sub", "codex", later)
    assert (view.status, view.reason) == ("stale", "credential cooldown")
    assert _refresh_record()["errors"] == {
        "quota:sub": "codex usage upstream status 429"
    }
    assert _refresh_record()["ok"] is False


def test_failure_without_last_good_has_no_data(net: _Net) -> None:
    net.routes[("GET", quota.OPENROUTER_CREDITS_URL)] = RequestError(
        "http", f"{quota.OPENROUTER_CREDITS_URL} HTTP 401", 401
    )
    quota.refresh_once(now=NOW)
    record = _sources()["or"]
    assert record["ok"] is False
    assert record["error_kind"] == "upstream_error"
    assert record["data"] is None
    assert record["updated_at"] is None
    view = statusline.quota_view(quota.load_quota(), "or", "openrouter", NOW)
    assert view.data is None
    assert view.reason == "refresh failed"


@pytest.mark.parametrize(
    ("change", "source", "kind"),
    [
        (
            lambda n: n.upstream.__setitem__(
                quota.CODEX_USAGE_URL, _ok({}, status=500)
            ),
            "sub",
            "upstream_error",
        ),
        (
            lambda n: n.upstream.__setitem__(
                quota.CODEX_USAGE_URL, {"status_code": 200, "body": "not json"}
            ),
            "sub",
            "response_invalid",
        ),
        (
            lambda n: n.upstream.__setitem__(
                quota.CODEX_USAGE_URL, _ok({"rate_limit": {}})
            ),
            "sub",
            "response_invalid",
        ),
        (
            lambda n: n.routes.__setitem__(("GET", AUTH_FILES), {"files": []}),
            "ag",
            "credential_missing",
        ),
        (
            lambda n: n.routes.__setitem__(
                ("GET", quota.OPENROUTER_CREDITS_URL), {"data": {"total_credits": "x"}}
            ),
            "or",
            "response_invalid",
        ),
        (
            lambda n: n.routes.__setitem__(
                ("GET", quota.OPENROUTER_CREDITS_URL),
                RequestError("http", "credits HTTP 429", 429),
            ),
            "or",
            "credential_cooldown",
        ),
    ],
)
def test_error_classification(
    net: _Net, change: Callable[[_Net], None], source: str, kind: str
) -> None:
    change(net)
    quota.refresh_once(now=NOW)
    assert _sources()[source]["error_kind"] == kind


def test_management_unreachable_affects_only_subscriptions(net: _Net) -> None:
    quota.refresh_once(now=NOW)
    net.routes[("GET", AUTH_FILES)] = RequestError(
        "connection", f"{AUTH_FILES} refused"
    )
    quota.refresh_once(now=NOW + 120)
    sources = _sources()
    assert sources["sub"]["error_kind"] == "management_error"
    assert sources["ag"]["error_kind"] == "management_error"
    assert sources["or"]["ok"] is True
    # 选出凭据之前就失败：沿用上一次的冷却快照
    assert (
        sources["sub"]["cooldowns"]
        == quota.select_credential(_auth_files(), "codex")[1]
    )


def test_missing_key_file_is_config_error_for_all(net: _Net) -> None:
    paths.source_key_file("plan").unlink()
    quota.refresh_once(now=NOW)
    sources = _sources()
    assert {record["error_kind"] for record in sources.values()} == {"config_error"}
    assert [url for _method, url, _headers, _body in net.calls] == [
        quota.LATEST_RELEASE_URL
    ]


def test_unreadable_config_is_recorded(net: _Net) -> None:
    paths.config_file().write_text("not = [valid", encoding="utf-8")
    assert quota.refresh_once(now=NOW) == 0
    errors = _refresh_record()["errors"]
    assert isinstance(errors, dict)
    assert "config" in errors
    assert not paths.quota_file().exists()


def test_no_key_material_is_written(net: _Net) -> None:
    net.routes[("GET", AUTH_FILES)] = RequestError(
        "connection", f"{AUTH_FILES} refused"
    )
    net.routes[("GET", quota.OPENROUTER_CREDITS_URL)] = RequestError(
        "http", f"{quota.OPENROUTER_CREDITS_URL} HTTP 401", 401
    )
    quota.refresh_once(now=NOW)
    for path in (paths.quota_file(), paths.refresh_file()):
        text = path.read_text(encoding="utf-8")
        for key in ALL_KEYS:
            assert key not in text


# -----------------------------------------------------------------------------
# Antigravity 换域名


def test_antigravity_falls_back_across_domains(net: _Net) -> None:
    first, second, third = quota.ANTIGRAVITY_QUOTA_URLS
    net.upstream[first] = RequestError("timeout", f"{API_CALL} timeout: timed out")
    net.upstream[second] = _ok({}, status=503)
    net.upstream[third] = _ok(_antigravity_summary())
    quota.refresh_once(now=NOW)
    assert _sources()["ag"]["ok"] is True
    assert [url for url in net.upstream_urls() if url != quota.CODEX_USAGE_URL] == [
        first,
        second,
        third,
    ]


def test_antigravity_all_domains_fail_with_last_reason(net: _Net) -> None:
    for url in quota.ANTIGRAVITY_QUOTA_URLS:
        net.upstream[url] = _ok({}, status=429)
    quota.refresh_once(now=NOW)
    record = _sources()["ag"]
    assert record["error_kind"] == "credential_cooldown"
    assert quota.ANTIGRAVITY_QUOTA_URLS[-1] in str(record["detail"])


def test_antigravity_gateway_unreachable_does_not_retry(net: _Net) -> None:
    first = quota.ANTIGRAVITY_QUOTA_URLS[0]
    net.upstream[first] = RequestError("connection", f"{API_CALL} refused")
    quota.refresh_once(now=NOW)
    assert _sources()["ag"]["error_kind"] == "management_error"
    assert [url for url in net.upstream_urls() if url != quota.CODEX_USAGE_URL] == [
        first
    ]


# -----------------------------------------------------------------------------
# 节流、锁与 refresh.json


def test_recent_attempt_skips_refresh(net: _Net) -> None:
    jsonio.write_json_atomic(paths.refresh_file(), {"attempted_at": NOW - 59})
    assert quota.refresh_once(now=NOW) == 0
    assert net.calls == []
    assert not paths.quota_file().exists()


def test_held_lock_skips_refresh(net: _Net) -> None:
    with quota.refresh_lock(blocking=False) as held:
        assert held
        assert quota.refresh_once(now=NOW) == 0
    assert net.calls == []


def test_claim_refresh_is_throttled_and_records_spawn() -> None:
    assert quota.claim_refresh(NOW) is True
    assert quota.claim_refresh(NOW + 59) is False
    assert quota.claim_refresh(NOW + 60) is True
    assert quota.read_refresh_record() == {"spawned_at": NOW + 60}


def test_claim_refresh_refuses_while_refresh_holds_lock() -> None:
    with quota.refresh_lock(blocking=False) as held:
        assert held
        assert quota.claim_refresh(NOW) is False
    assert not paths.refresh_file().exists()


def test_both_writers_keep_each_others_keys(net: _Net) -> None:
    assert quota.claim_refresh(NOW - 120) is True
    quota.refresh_once(now=NOW)
    record = _refresh_record()
    assert record["spawned_at"] == NOW - 120
    assert record["attempted_at"] == NOW
    assert quota.claim_refresh(NOW + 60) is True
    record = _refresh_record()
    assert record["attempted_at"] == NOW
    assert record["errors"] == {}
    assert record["spawned_at"] == NOW + 60


def test_corrupt_refresh_record_is_replaced(net: _Net) -> None:
    paths.refresh_file().parent.mkdir(parents=True, exist_ok=True)
    paths.refresh_file().write_text("{", encoding="utf-8")
    assert quota.refresh_once(now=NOW) == 0
    assert _refresh_record()["attempted_at"] == NOW


def test_status_line_spawn_goes_through_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        statusline, "spawn_refresh", lambda cmd: spawned.append(tuple(cmd))
    )
    with quota.refresh_lock(blocking=False):
        assert statusline.maybe_spawn_refresh(NOW) is False
    assert statusline.maybe_spawn_refresh(NOW) is True
    assert len(spawned) == 1


# -----------------------------------------------------------------------------
# 目录与网关新版


def test_catalog_refresh_uses_24_hours(net: _Net) -> None:
    quota.refresh_once(now=NOW)
    assert len(_CATALOG_CALLS) == 1
    max_age, now = _CATALOG_CALLS[0]
    assert max_age == timedelta(hours=24)
    assert now is not None
    assert int(now.timestamp()) == NOW


def test_catalog_failure_does_not_block_quota(
    net: _Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(max_age: timedelta, *, now: datetime | None = None) -> bool:
        del max_age, now
        raise catalog.CatalogError("OpenRouter 目录 x 请求失败：offline")

    monkeypatch.setattr(catalog, "refresh_catalog_if_stale", failing)
    quota.refresh_once(now=NOW)
    assert _sources()["sub"]["ok"] is True
    assert "offline" in str(_refresh_record()["errors"])


def test_release_record_written_and_throttled(net: _Net) -> None:
    quota.refresh_once(now=NOW)
    assert quota.load_release_record() == {
        "schema": 1,
        "version": "7.3.20",
        "checked_at": NOW,
    }
    net.routes[("GET", quota.LATEST_RELEASE_URL)] = {"tag_name": "v7.3.21"}
    assert quota.refresh_release_if_stale(NOW + 86399) is False
    assert quota.refresh_release_if_stale(NOW + 86400) is True
    record = quota.load_release_record()
    assert record is not None
    assert (record["version"], record["checked_at"]) == ("7.3.21", NOW + 86400)


def test_release_failure_keeps_old_record(net: _Net) -> None:
    quota.write_release_record("7.3.19", NOW - 90000)
    net.routes[("GET", quota.LATEST_RELEASE_URL)] = RequestError(
        "timeout", "github timeout"
    )
    quota.refresh_once(now=NOW)
    record = quota.load_release_record()
    assert record is not None
    assert record["version"] == "7.3.19"
    assert "github timeout" in str(_refresh_record()["errors"])


@pytest.mark.parametrize("tag", ["latest", None, "7.3"])
def test_fetch_latest_release_rejects_bad_tag(net: _Net, tag: object) -> None:
    net.routes[("GET", quota.LATEST_RELEASE_URL)] = {"tag_name": tag}
    with pytest.raises(ValueError, match="tag_name"):
        quota.fetch_latest_release()


def test_fetch_latest_release_strips_prefix(net: _Net) -> None:
    assert quota.fetch_latest_release() == "7.3.20"
    headers = net.calls[-1][2]
    assert headers["Accept"] == "application/vnd.github+json"


def test_timeouts() -> None:
    assert quota.QUOTA_TIMEOUT_SECONDS == 15
    assert quota.RELEASE_TIMEOUT_SECONDS == 10


# -----------------------------------------------------------------------------
# request_json 本身（本地假服务）


@dataclass
class _Server:
    base_url: str
    status: int = 200
    body: bytes = b"{}"
    delay: float = 0.0


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    state = _Server(base_url="")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if state.delay:
                time.sleep(state.delay)
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, format: str, *args: object) -> None:
            # 不向测试输出写访问日志
            del format, args

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    state.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_request_json_loopback_bypasses_proxy(server: _Server) -> None:
    server.body = b'{"ok": true}'
    assert quota.request_json("GET", server.base_url, {}, timeout=2) == {"ok": True}


@pytest.mark.parametrize(
    ("status", "body", "delay", "kind"),
    [
        (500, b"{}", 0.0, "http"),
        (200, b"nope", 0.0, "invalid"),
        (200, b"[1]", 0.0, "invalid"),
        (200, b"{}", 1.0, "timeout"),
    ],
)
def test_request_json_error_kinds(
    server: _Server, status: int, body: bytes, delay: float, kind: str
) -> None:
    server.status, server.body, server.delay = status, body, delay
    with pytest.raises(RequestError) as caught:
        quota.request_json(
            "GET", server.base_url, {"Authorization": "Bearer secret-x"}, timeout=0.3
        )
    assert caught.value.kind == kind
    assert "secret-x" not in str(caught.value)
    if kind == "http":
        assert caught.value.status == 500


def test_request_json_connection_refused() -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = httpd.server_address[1]
    httpd.server_close()
    with pytest.raises(RequestError) as caught:
        quota.request_json("GET", f"http://127.0.0.1:{port}", {}, timeout=1)
    assert caught.value.kind == "connection"


def test_main_returns_zero(net: _Net) -> None:
    assert quota.main() == 0
