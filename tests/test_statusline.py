"""claudex.statusline：JSON 改写与 `claudex` 对象、费用结算、额度、底层渲染器与后台刷新。

快照与额度缓存在隔离的 state 目录里现场构造；底层渲染器是写在 `tmp_path` 下的 shell
脚本；后台刷新经 monkeypatch 替换 `claudex.statusline.spawn_refresh`，不真的起子进程。
`TMPDIR` 由 conftest 指到本用例的临时目录。
"""

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from claudex import jsonio, paths, render, statusline
from claudex.render import ModelSnapshot, PricingSnapshot, ProfileSnapshot

NOW = 1_800_000_000
PRICE_C: PricingSnapshot = {
    "prompt": 0.000002,
    "completion": 0.00001,
    "input_cache_read": 0.0000002,
    "input_cache_write": 0.0000025,
}
PRICE_A: PricingSnapshot = {
    "prompt": 0.000001,
    "completion": 0.000004,
    "input_cache_read": None,
    "input_cache_write": None,
}
CLAUDEX_KEYS = {
    "profile",
    "source",
    "source_type",
    "cost_estimated",
    "fast",
    "quota_label",
    "markers",
}


def _model(
    ref: str,
    source_type: str,
    gateway_id: str,
    *,
    context: int,
    efforts: list[str] | None = None,
    dynamic: bool = False,
    display: str | None = None,
    pricing: PricingSnapshot | None = None,
) -> ModelSnapshot:
    source, _, model_id = ref.partition("/")
    return {
        "ref": ref,
        "source": source,
        "source_type": source_type,
        "model_id": model_id,
        "gateway_id": gateway_id,
        "claude_id": gateway_id + ("[1m]" if context > 200_000 else ""),
        "openrouter": None,
        "context": context,
        "efforts": efforts,
        "dynamic_allowed": dynamic,
        "display": display or model_id,
        "pricing": pricing,
        "estimated": source_type != "openrouter",
    }


def _profile() -> ProfileSnapshot:
    models = [
        _model(
            "or/vendor/model-c",
            "openrouter",
            "or/vendor-model-c",
            context=400000,
            efforts=["low", "medium", "high"],
            display="Vendor: Model C",
            pricing=PRICE_C,
        ),
        _model(
            "sub/model-a",
            "codex",
            "model-a",
            context=272000,
            efforts=["low", "medium", "high", "xhigh"],
            pricing=PRICE_A,
        ),
        _model("plan/model-f", "anthropic", "plan/model-f", context=128000),
        _model(
            "ag/gemini-x",
            "antigravity",
            "gemini-x",
            context=1048576,
            efforts=["low", "high"],
            dynamic=True,
        ),
        _model("ag/model-z", "antigravity", "model-z", context=128000),
    ]
    return {
        "schema": 1,
        "profile": "daily",
        "tiers": {
            "fable": "or/vendor/model-c",
            "opus": "sub/model-a",
            "sonnet": "plan/model-f",
            "haiku": "ag/gemini-x",
        },
        "models": {model["ref"]: model for model in models},
        "compact_window": 121600,
    }


def _write_snapshot(profile: ProfileSnapshot | None = None) -> Path:
    directory = paths.sessions_dir()
    profile_file = directory / "daily-aaaaaaaaaaaa.profile.json"
    jsonio.write_json_atomic(profile_file, profile or _profile())
    jsonio.write_json_atomic(directory / "daily-aaaaaaaaaaaa.settings.json", {})
    return profile_file


def _payload(
    model_id: str = "claude-fable-5",
    *,
    api_ms: int = 0,
    native_cost: float | None = None,
    usage: dict[str, int] | None = None,
    total_in: int = 1000,
    total_out: int = 100,
    effort: str | None = None,
    session: str = "s1",
) -> dict[str, object]:
    cost: dict[str, object] = {"total_api_duration_ms": api_ms}
    if native_cost is not None:
        cost["total_cost_usd"] = native_cost
    payload: dict[str, object] = {
        "session_id": session,
        "model": {"id": model_id, "display_name": "Opus 5"},
        "cost": cost,
        "context_window": {
            "context_window_size": 200000,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "current_usage": usage,
        },
        "rate_limits": {"five_hour": {"used_percentage": 1.0, "resets_at": 1}},
        "prompt_cache": {"ttl": "5m", "expires_at": 1, "warm": True},
    }
    if effort is not None:
        payload["effort"] = {"level": effort}
    return payload


def _usage(inp: int, out: int, read: int = 0, write: int = 0) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
    }


def _prepare(
    payload: dict[str, object],
    state: dict[str, object] | None = None,
    *,
    quota: dict[str, object] | None = None,
    fast: bool = False,
    profile: ProfileSnapshot | None = None,
) -> dict[str, object]:
    return statusline.prepare(
        payload,
        profile if profile is not None else _profile(),
        quota,
        state if state is not None else statusline.empty_state(),
        NOW,
        fast=fast,
    )


def _claudex(result: dict[str, object]) -> dict[str, object]:
    obj = result["claudex"]
    assert isinstance(obj, dict)
    return obj


def _markers(result: dict[str, object]) -> list[object]:
    markers = _claudex(result)["markers"]
    assert isinstance(markers, list)
    return markers


def _cost(result: dict[str, object]) -> object:
    cost = result["cost"]
    assert isinstance(cost, dict)
    return cost.get("total_cost_usd")


# -----------------------------------------------------------------------------
# 改写与 claudex 对象


def test_claudex_object_has_exactly_the_frozen_fields() -> None:
    result = _prepare(_payload())
    assert _claudex(result) == {
        "profile": "daily",
        "source": "or",
        "source_type": "openrouter",
        "cost_estimated": False,
        "fast": False,
        "quota_label": "or",
        "markers": ["or n/a (no quota data)"],
    }
    assert set(_claudex(result)) == CLAUDEX_KEYS


def test_prompt_cache_and_native_fields_are_removed() -> None:
    result = _prepare(_payload(native_cost=3.5))
    assert "prompt_cache" not in result
    assert "rate_limits" not in result
    assert _cost(result) is None


def test_no_profile_only_sanitizes() -> None:
    result = statusline.prepare(
        _payload(native_cost=1.0), None, None, statusline.empty_state(), NOW, fast=True
    )
    assert "prompt_cache" not in result
    assert "rate_limits" not in result
    assert _cost(result) is None
    assert _claudex(result) == {
        "profile": None,
        "source": None,
        "source_type": None,
        "cost_estimated": False,
        "fast": False,
        "quota_label": None,
        "markers": ["profile unavailable"],
    }


def test_display_name_comes_from_snapshot() -> None:
    result = _prepare(_payload("claude-fable-5"))
    model = result["model"]
    assert isinstance(model, dict)
    assert model["display_name"] == "Vendor: Model C"


@pytest.mark.parametrize(
    ("model_id", "effort", "expected"),
    [
        ("claude-opus-5", "xhigh", "xhigh"),
        ("claude-fable-5", "xhigh", "high"),
        ("claude-haiku-4-5", "auto", "auto"),
        ("claude-haiku-4-5", "medium", "low"),
        ("claude-sonnet-5", "high", None),
    ],
)
def test_effort_uses_generator_clamp(
    model_id: str, effort: str, expected: str | None
) -> None:
    result = _prepare(_payload(model_id, effort=effort))
    if expected is None:
        assert "effort" not in result
    else:
        assert result["effort"] == {"level": expected}


@pytest.mark.parametrize(
    ("model_id", "ref"),
    [
        ("fable", "or/vendor/model-c"),
        ("claude-fable-5-1", "or/vendor/model-c"),
        ("claude-haiku-4-5-20251001", "ag/gemini-x"),
        ("model-a[1m]", "sub/model-a"),
        ("model-a", "sub/model-a"),
        ("or/vendor-model-c[1M]", "or/vendor/model-c"),
        ("model-z", "ag/model-z"),
    ],
)
def test_find_model_accepts_every_id_form(model_id: str, ref: str) -> None:
    found = statusline.find_model(_profile(), model_id)
    assert found is not None
    assert found["ref"] == ref


def test_unknown_model_leaves_source_empty() -> None:
    result = _prepare(_payload("claude-something-else"))
    obj = _claudex(result)
    assert obj["source"] is None
    assert obj["quota_label"] is None


def test_context_denominator_uses_compact_window() -> None:
    result = _prepare(_payload(total_in=60800))
    window = result["context_window"]
    assert isinstance(window, dict)
    assert window["context_window_size"] == 121600
    assert window["used_percentage"] == pytest.approx(50.0)
    assert window["remaining_percentage"] == pytest.approx(50.0)


# -----------------------------------------------------------------------------
# 费用结算


def test_native_cost_increase_settles_once() -> None:
    state = statusline.empty_state()
    usage = _usage(1000, 200, read=5000, write=400)
    first = _prepare(_payload(api_ms=100, native_cost=0.5, usage=usage), state)
    expected = 1000 * 0.000002 + 200 * 0.00001 + 5000 * 0.0000002 + 400 * 0.0000025
    assert _cost(first) == pytest.approx(expected)
    again = _prepare(_payload(api_ms=100, native_cost=0.5, usage=usage), state)
    assert _cost(again) == pytest.approx(expected)
    assert state["settled_events"] == 1


def test_native_cost_increase_without_new_usage_does_not_resettle() -> None:
    # subagent 与后台调用推高会话级原生费用，主对话最近一次 usage 不变
    state = statusline.empty_state()
    usage = _usage(1000, 200, read=5000, write=400)
    for native_cost in (0.5, 0.7, 0.9):
        _prepare(_payload(api_ms=100, native_cost=native_cost, usage=usage), state)
    assert state["settled_events"] == 1


def test_second_response_accumulates() -> None:
    state = statusline.empty_state()
    _prepare(_payload(api_ms=100, native_cost=0.5, usage=_usage(1000, 0)), state)
    result = _prepare(
        _payload(api_ms=250, native_cost=0.9, usage=_usage(0, 100)), state
    )
    assert _cost(result) == pytest.approx(1000 * 0.000002 + 100 * 0.00001)


def test_clear_resets_cost() -> None:
    state = statusline.empty_state()
    _prepare(_payload(api_ms=100, native_cost=0.5, usage=_usage(1000, 0)), state)
    cleared = _prepare(
        _payload(api_ms=0, native_cost=0.0, usage=_usage(1000, 0), total_in=0), state
    )
    assert _cost(cleared) is None
    after = _prepare(
        _payload(api_ms=50, native_cost=0.1, usage=_usage(0, 10), total_in=10), state
    )
    assert _cost(after) == pytest.approx(10 * 0.00001)


def test_fallback_without_native_cost_settles_on_new_usage() -> None:
    state = statusline.empty_state()
    first = _prepare(_payload(api_ms=100, usage=_usage(1000, 0)), state)
    assert _cost(first) == pytest.approx(1000 * 0.000002)
    repeat = _prepare(_payload(api_ms=100, usage=_usage(1000, 0)), state)
    assert _cost(repeat) == pytest.approx(1000 * 0.000002)
    failed_retry = _prepare(_payload(api_ms=180, usage=_usage(1000, 0)), state)
    assert _cost(failed_retry) == pytest.approx(1000 * 0.000002)
    second = _prepare(_payload(api_ms=300, usage=_usage(0, 50)), state)
    assert _cost(second) == pytest.approx(1000 * 0.000002 + 50 * 0.00001)


def test_fallback_clear_resets_cost() -> None:
    state = statusline.empty_state()
    _prepare(
        _payload(api_ms=100, usage=_usage(1000, 0), total_in=5000, total_out=500),
        state,
    )
    cleared = _prepare(
        _payload(api_ms=0, usage=_usage(1000, 0), total_in=0, total_out=0), state
    )
    assert _cost(cleared) is None
    assert state["settled_events"] == 0


def test_estimated_cost_for_non_openrouter_model() -> None:
    state = statusline.empty_state()
    result = _prepare(
        _payload("claude-opus-5", api_ms=100, native_cost=0.1, usage=_usage(1000, 0)),
        state,
    )
    assert _cost(result) == pytest.approx(1000 * 0.000001)
    assert _claudex(result)["cost_estimated"] is True
    assert statusline.builtin_line(result).count("≈$0.00") == 1


def test_openrouter_cost_is_not_estimated() -> None:
    result = _prepare(_payload(api_ms=100, native_cost=0.1, usage=_usage(1000, 0)))
    assert _claudex(result)["cost_estimated"] is False


def test_unpriced_response_is_not_counted() -> None:
    state = statusline.empty_state()
    result = _prepare(
        _payload("claude-sonnet-5", api_ms=100, native_cost=0.1, usage=_usage(1000, 0)),
        state,
    )
    assert _cost(result) is None
    mixed = _prepare(
        _payload("claude-fable-5", api_ms=200, native_cost=0.2, usage=_usage(0, 10)),
        state,
    )
    assert _cost(mixed) == pytest.approx(10 * 0.00001)
    assert "cost partial" in _markers(mixed)


# -----------------------------------------------------------------------------
# 额度


def _quota(source: str, record: dict[str, object]) -> dict[str, object]:
    return {"schema": 1, "sources": {source: record}}


def _record(
    data: dict[str, object], *, age: int = 10, ok: bool = True, **extra: object
) -> dict[str, object]:
    return {"updated_at": NOW - age, "ok": ok, "error": None, "data": data, **extra}


CODEX_DATA: dict[str, object] = {
    "windows": {
        "five_hour": {"used_percentage": 12.0, "resets_at": NOW + 3 * 3600 + 300},
        "seven_day": {
            "used_percentage": 62.0,
            "resets_at": NOW + 2 * 86400 + 16 * 3600,
        },
    }
}


def test_codex_windows_become_rate_limits_and_marker() -> None:
    result = _prepare(
        _payload("claude-opus-5"), quota=_quota("sub", _record(CODEX_DATA))
    )
    assert result["rate_limits"] == {
        "five_hour": {"used_percentage": 12.0, "resets_at": NOW + 3 * 3600 + 300},
        "seven_day": {
            "used_percentage": 62.0,
            "resets_at": NOW + 2 * 86400 + 16 * 3600,
        },
    }
    obj = _claudex(result)
    assert obj["quota_label"] == "sub"
    assert obj["markers"] == ["sub 5h 12% 3h05m 7d 62% 2d16h"]


def test_stale_quota_is_marked() -> None:
    result = _prepare(
        _payload("claude-opus-5"),
        quota=_quota("sub", _record(CODEX_DATA, age=300, ok=False, error="upstream")),
    )
    assert "rate_limits" in result
    assert _claudex(result)["markers"] == ["sub 5h 12% 3h05m 7d 62% 2d16h?"]


def test_antigravity_buckets_by_family() -> None:
    data: dict[str, object] = {
        "buckets": [
            {"id": "gemini-pro", "remaining_fraction": 0.75, "reset_at": NOW + 3600},
            {"id": "gemini-flash", "remaining_fraction": 0.5, "reset_at": NOW + 4000},
            {
                "id": "3p-claude",
                "remaining_fraction": 0.1,
                "reset_at": NOW + 3 * 86400,
                "window_seconds": 604800,
            },
        ]
    }
    quota = _quota("ag", _record(data))
    gemini = _prepare(_payload("claude-haiku-4-5"), quota=quota)
    assert gemini["rate_limits"] == {
        "five_hour": {"used_percentage": 50.0, "resets_at": NOW + 4000}
    }
    other = _prepare(_payload("model-z"), quota=quota)
    assert other["rate_limits"] == {
        "seven_day": {
            "used_percentage": pytest.approx(90.0),
            "resets_at": NOW + 3 * 86400,
        }
    }


def test_openrouter_balance_marker_without_rate_limits() -> None:
    quota = _quota("or", _record({"total_credits": 20.0, "total_usage": 7.66}))
    result = _prepare(_payload(), quota=quota)
    assert "rate_limits" not in result
    assert _claudex(result)["markers"] == ["or $12.34 left"]


def test_old_openrouter_balance_is_not_shown() -> None:
    quota = _quota("or", _record({"total_credits": 20.0, "total_usage": 1.0}, age=700))
    result = _prepare(_payload(), quota=quota)
    assert _claudex(result)["markers"] == ["or n/a (stale)"]


def test_generic_source_without_quota_type_has_no_label() -> None:
    result = _prepare(_payload("claude-sonnet-5"))
    obj = _claudex(result)
    assert obj["quota_label"] is None
    assert obj["markers"] == []


def test_cooldown_marker_for_current_model() -> None:
    cooldowns = [
        {
            "scope": "model",
            "model_key": "other",
            "reason": "quota",
            "retry_at": NOW + 900,
        },
        {
            "scope": "model",
            "model_key": "model-a",
            "reason": "quota",
            "retry_at": NOW + 600,
        },
        {"scope": "credential", "reason": "unauthorized", "retry_at": NOW - 5},
    ]
    quota = _quota("sub", _record(CODEX_DATA, cooldowns=cooldowns))
    markers = _claudex(_prepare(_payload("claude-opus-5"), quota=quota))["markers"]
    assert markers == ["sub 5h 12% 3h05m 7d 62% 2d16h", "cooldown model quota 10:00"]


def test_quota_cache_with_wrong_schema_is_ignored() -> None:
    jsonio.write_json_atomic(paths.quota_file(), {"schema": 99, "sources": {}})
    assert statusline.load_quota() is None
    jsonio.write_json_atomic(paths.quota_file(), _quota("sub", _record(CODEX_DATA)))
    assert statusline.load_quota() == _quota("sub", _record(CODEX_DATA))


# -----------------------------------------------------------------------------
# Fast


def test_fast_on_codex_model() -> None:
    result = _prepare(_payload("claude-opus-5"), fast=True)
    assert _claudex(result)["fast"] is True
    assert "fast ×2.5 not in ≈" in _markers(result)
    assert statusline.builtin_line(result).startswith("model-a ↯fast")


def test_fast_ignored_on_non_codex_model() -> None:
    result = _prepare(_payload(), fast=True)
    assert _claudex(result)["fast"] is False
    assert "fast ×2.5 not in ≈" not in _markers(result)


# -----------------------------------------------------------------------------
# 底层渲染器


def _script(tmp_path: Path, body: str) -> str:
    script = tmp_path / "renderer.sh"
    script.write_text(body, encoding="utf-8")
    return f"sh {script}"


def _rendered_payload() -> dict[str, object]:
    return _prepare(_payload(api_ms=100, native_cost=0.1, usage=_usage(1000, 0)))


def test_renderer_output_passes_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = tmp_path / "seen.json"
    command = _script(
        tmp_path, f"cat > {seen}\nprintf 'line1\\nline2'\necho warn >&2\n"
    )
    payload = _rendered_payload()
    assert statusline.run_renderer(command, payload) == "line1\nline2"
    assert json.loads(seen.read_text(encoding="utf-8")) == payload
    assert capsys.readouterr().err == "warn\n"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("exit 3\n", "退出码 3"),
        ("true\n", "没有输出"),
        ("sleep 5\n", "未结束"),
    ],
)
def test_renderer_failures_fall_back_with_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    reason: str,
) -> None:
    monkeypatch.setattr(statusline, "RENDERER_TIMEOUT_SECONDS", 0.3)
    payload = _rendered_payload()
    output = statusline.run_renderer(_script(tmp_path, body), payload)
    assert output == statusline.builtin_line(payload)
    assert reason in capsys.readouterr().err


def test_recursive_renderer_is_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    command = f"touch {marker}; python -m claudex.statusline"
    payload = _rendered_payload()
    assert statusline.run_renderer(command, payload) == statusline.builtin_line(payload)
    assert not marker.exists()
    assert "claudex.statusline" in capsys.readouterr().err


def test_unconfigured_renderer_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    payload = _rendered_payload()
    assert statusline.run_renderer(None, payload) == statusline.builtin_line(payload)
    assert statusline.run_renderer("", payload) == statusline.builtin_line(payload)
    assert capsys.readouterr().err == ""


def test_builtin_line_content() -> None:
    quota = _quota("or", _record({"total_credits": 20.0, "total_usage": 7.66}))
    payload = _prepare(
        _payload(
            api_ms=100, native_cost=0.1, usage=_usage(100000, 1000), effort="high"
        ),
        quota=quota,
    )
    assert statusline.builtin_line(payload) == (
        "Vendor: Model C · high · $0.21 · or $12.34 left"
    )


# -----------------------------------------------------------------------------
# 后台刷新


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        statusline, "spawn_refresh", lambda command: calls.append(tuple(command))
    )
    return calls


def test_refresh_spawns_quota_module(spawned: list[tuple[str, ...]]) -> None:
    assert statusline.maybe_spawn_refresh(NOW) is True
    assert spawned == [(sys.executable, "-P", "-m", "claudex.quota")]
    assert jsonio.read_json_object(paths.refresh_file()) == {"spawned_at": NOW}


def test_refresh_is_throttled_for_sixty_seconds(spawned: list[tuple[str, ...]]) -> None:
    statusline.maybe_spawn_refresh(NOW)
    assert statusline.maybe_spawn_refresh(NOW + 59) is False
    assert statusline.maybe_spawn_refresh(NOW + 60) is True
    assert len(spawned) == 2


def test_refresh_respects_attempt_record_and_keeps_its_keys(
    spawned: list[tuple[str, ...]],
) -> None:
    jsonio.write_json_atomic(
        paths.refresh_file(), {"attempted_at": NOW - 30, "result": "ok"}
    )
    assert statusline.maybe_spawn_refresh(NOW) is False
    assert statusline.maybe_spawn_refresh(NOW + 30) is True
    assert jsonio.read_json_object(paths.refresh_file()) == {
        "attempted_at": NOW - 30,
        "result": "ok",
        "spawned_at": NOW + 30,
    }


def test_no_refresh_env_disables_spawn(
    spawned: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDEX_NO_REFRESH", "1")
    assert statusline.maybe_spawn_refresh(NOW) is False
    assert spawned == []
    assert not paths.refresh_file().exists()


# -----------------------------------------------------------------------------
# 入口


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> tuple[str, str]:
    monkeypatch.setenv("CLAUDEX_NO_REFRESH", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert statusline.main() == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_main_refreshes_snapshot_mtime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_file = _write_snapshot()
    settings_file = profile_file.with_name("daily-aaaaaaaaaaaa.settings.json")
    old = 1_000_000_000
    for path in (profile_file, settings_file):
        os.utime(path, (old, old))
    monkeypatch.setenv("CLAUDEX_PROFILE_FILE", str(profile_file))
    out, err = _run_main(monkeypatch, capsys, _payload())
    assert out.startswith("Vendor: Model C")
    assert err == ""
    assert profile_file.stat().st_mtime > old
    assert settings_file.stat().st_mtime > old
    assert render.prune_snapshots() == []


def test_main_writes_session_state_in_tmpdir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDEX_PROFILE_FILE", str(_write_snapshot()))
    _run_main(
        monkeypatch,
        capsys,
        _payload(api_ms=100, native_cost=0.1, usage=_usage(1000, 0), session="abc"),
    )
    state_file = statusline.state_path("abc")
    assert state_file.parent == Path(os.environ["TMPDIR"])
    assert state_file.name.startswith("claudex-sl-")
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert statusline.load_state(state_file)["settled_events"] == 1


def test_main_with_unreadable_profile_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = paths.sessions_dir() / "daily-ffffffffffff.profile.json"
    monkeypatch.setenv("CLAUDEX_PROFILE_FILE", str(missing))
    out, err = _run_main(monkeypatch, capsys, _payload())
    assert "profile unreadable" in err
    assert "profile unavailable" in out


def test_main_uses_configured_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDEX_PROFILE_FILE", str(_write_snapshot()))
    seen = tmp_path / "seen.json"
    monkeypatch.setenv(
        "CLAUDEX_STATUSLINE_COMMAND", _script(tmp_path, f"cat > {seen}\nprintf ok\n")
    )
    out, _err = _run_main(monkeypatch, capsys, _payload())
    assert out == "ok"
    received = json.loads(seen.read_text(encoding="utf-8"))
    assert set(received["claudex"]) == CLAUDEX_KEYS
    assert "prompt_cache" not in received


def test_load_profile_snapshot_rejects_bad_structure(tmp_path: Path) -> None:
    broken = dict(_profile())
    broken["tiers"] = {"fable": "missing/ref"}
    path = tmp_path / "x.profile.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError, match="tiers"):
        statusline.load_profile_snapshot(path)
