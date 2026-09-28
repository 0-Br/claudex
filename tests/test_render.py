"""claudex.render：快照命名与复用、派生 settings、档位夹紧、快照清理与 preflight。

网关经 monkeypatch 替换 `claudex.gateway.fetch_models` 与
`claudex.gateway.fetch_model_definitions`；OpenRouter 目录写成 state 目录下的合成缓存
（`catalog.save_catalog`），key 文件与 `settings.base.json` 写在隔离的配置根里。
"""

import json
import os
import shlex
import stat
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from claudex import catalog, gateway, paths, render
from claudex.catalog import Catalog, CatalogEntry, ModelMeta, Pricing
from claudex.config import Config, ConfigError, parse_config, resolve_ref
from claudex.gateway import GatewayError

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
CLIENT_KEY = "c0ffee11" * 8
MANAGEMENT_KEY = "0badf00d" * 8
PLAN_KEY = "sk-FAKE-plan-6u1i5o"
OR_KEY = "sk-FAKE-or-3k9j2h"
PRICE_C = Pricing(
    prompt=0.000002,
    completion=0.00001,
    input_cache_read=0.0000002,
    input_cache_write=None,
)
PRICE_F = Pricing(
    prompt=0.000001, completion=0.000004, input_cache_read=None, input_cache_write=None
)
REGISTERED = {
    "model-a",
    "model-b",
    "model-e",
    "model-g",
    "or/vendor-model-c",
    "or/vendor-model-d",
    "plan/model-f",
}
BASE_SETTINGS: dict[str, object] = {
    "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Read(./secret/**)"]},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "user-bash-hook"}],
            }
        ],
        "Notification": [{"hooks": [{"type": "command", "command": "notify"}]}],
    },
    "statusLine": {"type": "command", "command": "my-statusline --flag"},
    "env": {
        "USER_VAR": "keep",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "stale",
        "CLAUDEX_FAST": "1",
        "CLAUDEX_PROFILE": "stale",
        "ANTHROPIC_BASE_URL": "http://example.invalid",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1",
        "ANTHROPIC_CUSTOM_HEADERS": "X-Other: yes\nx-claudex-tier: fast",
    },
    "model": "stale",
    "apiKeyHelper": "stale",
    "theme": "dark",
}


def _config_data() -> dict[str, object]:
    return {
        "default_profile": "daily",
        "compact_window_factor": 0.95,
        "sources": {
            "sub": {
                "type": "codex",
                "models": [
                    "model-a",
                    {"id": "model-b", "context": 128000, "efforts": ["low", "high"]},
                    {"id": "model-e", "context": 64000},
                ],
            },
            "ag": {"type": "antigravity", "models": ["model-g"]},
            "or": {
                "type": "openrouter",
                "models": ["vendor/model-c", "vendor/model-d"],
            },
            "plan": {
                "type": "anthropic",
                "base_url": "https://example.invalid/anthropic",
                "models": [
                    {"id": "model-f", "openrouter": "vendor/model-f", "context": 256000}
                ],
            },
        },
        "profiles": {
            "daily": {
                "fable": "or/vendor/model-c",
                "opus": "sub/model-a",
                "sonnet": "plan/model-f",
                "haiku": "sub/model-b",
            },
            "alt": {
                "fable": "sub/model-a",
                "opus": "sub/model-b",
                "sonnet": "sub/model-a",
                "haiku": "ag/model-g",
            },
        },
    }


def _config(mutate: Callable[[dict[str, object]], None] | None = None) -> Config:
    data = _config_data()
    if mutate is not None:
        mutate(data)
    return parse_config(data)


def _sources(data: dict[str, object]) -> dict[str, dict[str, object]]:
    sources = data["sources"]
    assert isinstance(sources, dict)
    return sources


def _catalog() -> Catalog:
    return Catalog(
        fetched_at=NOW,
        models={
            "vendor/model-c": CatalogEntry(
                name="Vendor: Model C",
                context_length=400000,
                supported_parameters=("reasoning",),
                pricing=PRICE_C,
            ),
            "vendor/model-d": CatalogEntry(
                name="Vendor: Model D",
                context_length=131072,
                supported_parameters=("tools",),
                pricing=None,
            ),
            "vendor/model-f": CatalogEntry(
                name="Vendor: Model F",
                context_length=200000,
                supported_parameters=("reasoning_effort",),
                pricing=PRICE_F,
            ),
        },
    )


def _definitions() -> dict[str, dict[str, object]]:
    return {
        "codex": {
            "models": [
                {
                    "id": "model-a",
                    "context_length": 272000,
                    "thinking": {
                        "levels": ["none", "low", "medium", "high", "xhigh"],
                        "dynamic_allowed": False,
                    },
                },
                {"id": "model-b", "context_length": 128000},
                {"id": "model-e", "context_length": 64000},
            ]
        },
        "antigravity": {
            "models": [
                {
                    "id": "model-g",
                    "context_length": 1048576,
                    "thinking": {"levels": ["low", "high"], "dynamic_allowed": True},
                }
            ]
        },
    }


@dataclass
class _Gateway:
    """替身网关：记录调用，按字段返回或抛错。"""

    registered: set[str] = field(default_factory=lambda: set(REGISTERED))
    definitions: dict[str, dict[str, object]] = field(default_factory=_definitions)
    models_error: GatewayError | None = None
    definitions_error: GatewayError | None = None
    calls: list[tuple[str, str, str]] = field(default_factory=list)


def _write_key(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _write_base(settings: dict[str, object]) -> None:
    paths.settings_base_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_base_file().write_text(json.dumps(settings), encoding="utf-8")


@pytest.fixture
def fake_gateway(monkeypatch: pytest.MonkeyPatch) -> _Gateway:
    """写好 key 文件、基底 settings 与目录缓存，并替换两个网关查询函数。"""
    _write_key(paths.client_key_file(), CLIENT_KEY)
    _write_key(paths.management_key_file(), MANAGEMENT_KEY)
    _write_key(paths.source_key_file("or"), OR_KEY)
    _write_key(paths.source_key_file("plan"), PLAN_KEY)
    _write_base(BASE_SETTINGS)
    catalog.save_catalog(_catalog())
    state = _Gateway()

    def fetch_models(client_key: str, *, base_url: str = "") -> set[str]:
        del base_url
        state.calls.append(("models", client_key, ""))
        if state.models_error is not None:
            raise state.models_error
        return set(state.registered)

    def fetch_model_definitions(
        management_key: str, channel: str, *, base_url: str = ""
    ) -> dict[str, object]:
        del base_url
        state.calls.append(("definitions", management_key, channel))
        if state.definitions_error is not None:
            raise state.definitions_error
        return state.definitions[channel]

    monkeypatch.setattr(gateway, "fetch_models", fetch_models)
    monkeypatch.setattr(gateway, "fetch_model_definitions", fetch_model_definitions)
    return state


def _read(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _env(settings: dict[str, object]) -> dict[str, object]:
    env = settings["env"]
    assert isinstance(env, dict)
    return env


def _render(
    config: Config | None = None,
    profile: str = "daily",
    overrides: dict[str, str] | None = None,
    *,
    fast: bool = False,
) -> tuple[render.Snapshot, dict[str, object], dict[str, object]]:
    snapshot = render.render(config or _config(), profile, overrides or {}, fast=fast)
    return snapshot, _read(snapshot.profile_file), _read(snapshot.settings_file)


def _write_user_settings(data: dict[str, object]) -> None:
    path = render.user_settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# -----------------------------------------------------------------------------
# 快照命名与复用


def test_snapshot_pair_is_named_by_digest(fake_gateway: _Gateway) -> None:
    snapshot, _profile, _settings = _render()
    assert snapshot.profile_file.parent == paths.sessions_dir()
    stem = snapshot.profile_file.name.removesuffix(".profile.json")
    assert snapshot.settings_file.name == f"{stem}.settings.json"
    profile_name, _, digest = stem.rpartition("-")
    assert profile_name == "daily"
    assert len(digest) == 12
    assert int(digest, 16) >= 0
    for path in (snapshot.profile_file, snapshot.settings_file):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_identical_content_reuses_snapshot(fake_gateway: _Gateway) -> None:
    first, _p, _s = _render()
    second, _p2, _s2 = _render()
    assert first == second
    assert sorted(p.name for p in paths.sessions_dir().iterdir()) == sorted(
        [first.profile_file.name, first.settings_file.name]
    )


def test_changed_content_gets_new_snapshot(fake_gateway: _Gateway) -> None:
    first, _p, _s = _render()
    _write_base({**BASE_SETTINGS, "theme": "light"})
    second, _p2, settings = _render()
    assert second.profile_file != first.profile_file
    assert first.profile_file.exists()
    assert settings["theme"] == "light"


def test_settings_point_back_to_profile_snapshot(fake_gateway: _Gateway) -> None:
    snapshot, _profile, settings = _render()
    env = _env(settings)
    assert env["CLAUDEX_PROFILE_FILE"] == str(snapshot.profile_file)
    assert env["CLAUDEX_PROFILE"] == "daily"


# -----------------------------------------------------------------------------
# 派生 settings


def test_tier_env_uses_claude_ids_and_display(fake_gateway: _Gateway) -> None:
    _snapshot, _profile, settings = _render()
    env = _env(settings)
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "or/vendor-model-c[1m]"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL_NAME"] == "Vendor: Model C"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL_DESCRIPTION"] == (
        "or/vendor-model-c via or (openrouter)"
    )
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "model-a[1m]"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"] == "model-a"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "plan/model-f[1m]"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "model-b"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION"] == "model-b via sub (codex)"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8317"


def test_one_m_suffix_only_above_200k(fake_gateway: _Gateway) -> None:
    _snapshot, profile, _settings = _render()
    models = profile["models"]
    assert isinstance(models, dict)
    ids = {ref: entry["claude_id"] for ref, entry in models.items()}
    assert ids["plan/model-f"] == "plan/model-f[1m]"
    assert ids["sub/model-b"] == "model-b"
    assert ids["or/vendor/model-d"] == "or/vendor-model-d"


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (200000, "plan/model-f"),
        (200001, "plan/model-f[1m]"),
        (None, "plan/model-f"),
    ],
)
def test_claude_model_id_threshold(context: int | None, expected: str) -> None:
    ref = resolve_ref(_config(), "plan/model-f")
    meta = ModelMeta(context=context, efforts=None, display="x")
    assert render.claude_model_id(ref, meta) == expected


def test_env_key_set_is_exact(fake_gateway: _Gateway) -> None:
    _snapshot, _profile, settings = _render()
    tier_keys = {
        f"ANTHROPIC_DEFAULT_{tier}_MODEL{suffix}"
        for tier in ("FABLE", "OPUS", "SONNET", "HAIKU")
        for suffix in ("", "_NAME", "_DESCRIPTION")
    }
    assert set(_env(settings)) == tier_keys | {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDEX_PROFILE",
        "CLAUDEX_PROFILE_FILE",
        "CLAUDEX_STATUSLINE_COMMAND",
        "USER_VAR",
    }


def test_model_available_models_and_overrides(fake_gateway: _Gateway) -> None:
    _snapshot, _profile, settings = _render()
    assert settings["model"] == "fable"
    assert settings["availableModels"] == [
        "fable",
        "opus",
        "sonnet",
        "haiku",
        "or/vendor-model-c[1m]",
        "model-a[1m]",
        "plan/model-f[1m]",
        "model-b",
        "model-e",
        "model-g[1m]",
        "or/vendor-model-d",
    ]
    assert settings["modelOverrides"] == {
        "claude-fable-5": "or/vendor-model-c[1m]",
        "claude-fable-5-1": "or/vendor-model-c[1m]",
        "claude-opus-5": "model-a[1m]",
        "claude-sonnet-5": "plan/model-f[1m]",
        "claude-haiku-4-5": "model-b",
        "claude-haiku-4-5-20251001": "model-b",
    }


def test_unregistered_non_tier_model_is_not_selectable(fake_gateway: _Gateway) -> None:
    fake_gateway.registered.discard("model-g")
    _snapshot, profile, settings = _render()
    available = settings["availableModels"]
    assert isinstance(available, list)
    assert "model-g[1m]" not in available
    models = profile["models"]
    assert isinstance(models, dict)
    assert "ag/model-g" not in models


def test_commands_use_current_interpreter(fake_gateway: _Gateway) -> None:
    _snapshot, _profile, settings = _render()
    helper = Path(sysconfig.get_path("scripts")) / "claudex-client-key"
    assert settings["apiKeyHelper"] == shlex.quote(str(helper))
    # 安装形态真的把入口放在了这里，不只是两处用了同一个算法
    assert helper.is_file()
    assert os.access(helper, os.X_OK)
    status_line = settings["statusLine"]
    assert status_line == {
        "type": "command",
        "refreshInterval": 1,
        "command": f"{shlex.quote(sys.executable)} -P -m claudex.statusline",
    }


def test_base_passthrough_keeps_user_config(fake_gateway: _Gateway) -> None:
    _snapshot, _profile, settings = _render()
    assert settings["permissions"] == BASE_SETTINGS["permissions"]
    assert settings["hooks"] == BASE_SETTINGS["hooks"]
    assert settings["theme"] == "dark"
    env = _env(settings)
    assert env["USER_VAR"] == "keep"
    assert env["CLAUDEX_STATUSLINE_COMMAND"] == "my-statusline --flag"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Other: yes"
    assert "CLAUDEX_FAST" not in env


def test_statusline_defaults_filled_and_original_command_optional(
    fake_gateway: _Gateway,
) -> None:
    _write_base(
        {
            "statusLine": {"padding": 2},
            "env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Claudex-Tier: fast"},
        }
    )
    _snapshot, _profile, settings = _render()
    status_line = settings["statusLine"]
    assert isinstance(status_line, dict)
    assert status_line["padding"] == 2
    assert status_line["type"] == "command"
    assert status_line["refreshInterval"] == 1
    env = _env(settings)
    assert "CLAUDEX_STATUSLINE_COMMAND" not in env
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env


def test_fast_appends_header_after_base_headers(fake_gateway: _Gateway) -> None:
    fast_snapshot, _p, settings = _render(fast=True)
    env = _env(settings)
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Other: yes\nX-Claudex-Tier: fast"
    assert env["CLAUDEX_FAST"] == "1"
    plain_snapshot, _p2, plain = _render()
    assert _env(plain)["ANTHROPIC_CUSTOM_HEADERS"] == "X-Other: yes"
    assert "CLAUDEX_FAST" not in _env(plain)
    assert plain_snapshot != fast_snapshot


def test_fast_without_base_headers(fake_gateway: _Gateway) -> None:
    _write_base({"env": {"ANTHROPIC_CUSTOM_HEADERS": "x-claudex-tier: fast"}})
    _snapshot, _profile, settings = _render(fast=True)
    env = _env(settings)
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Claudex-Tier: fast"
    assert env["CLAUDEX_FAST"] == "1"


def test_missing_settings_base_is_config_error(fake_gateway: _Gateway) -> None:
    paths.settings_base_file().unlink()
    with pytest.raises(ConfigError, match="claudex init"):
        _render()


# -----------------------------------------------------------------------------
# 档位


def test_model_settings_clamp_user_effort(fake_gateway: _Gateway) -> None:
    _write_user_settings(
        {
            "effortLevel": "xhigh",
            "modelSettings": {"claude-opus-5": {"effortLevel": "low"}},
        }
    )
    _snapshot, _profile, settings = _render()
    assert settings["modelSettings"] == {
        "claude-fable-5": {"effortLevel": "high"},
        "claude-fable-5-1": {"effortLevel": "high"},
        "claude-opus-5": {"effortLevel": "low"},
        "claude-sonnet-5": {"effortLevel": "high"},
        "claude-haiku-4-5": {"effortLevel": "high"},
        "claude-haiku-4-5-20251001": {"effortLevel": "high"},
    }


def test_tier_without_efforts_is_not_written(fake_gateway: _Gateway) -> None:
    _snapshot, _profile, settings = _render(overrides={"haiku": "or/vendor/model-d"})
    model_settings = settings["modelSettings"]
    assert isinstance(model_settings, dict)
    assert "claude-haiku-4-5" not in model_settings
    assert model_settings["claude-opus-5"] == {"effortLevel": "high"}


@pytest.mark.parametrize(
    ("requested", "levels", "dynamic", "expected"),
    [
        ("high", ("low", "medium", "high"), False, "high"),
        ("xhigh", ("low", "medium", "high"), False, "high"),
        ("medium", ("low", "high"), False, "low"),
        ("low", ("medium", "high"), False, "medium"),
        ("auto", ("low", "high"), True, "auto"),
        ("auto", ("low", "medium", "high"), False, "medium"),
        ("auto", ("low", "high"), False, "low"),
        ("bogus", ("medium", "high"), False, "medium"),
        ("high", None, False, None),
        ("high", (), True, None),
    ],
)
def test_effective_effort(
    requested: str,
    levels: tuple[str, ...] | None,
    dynamic: bool,
    expected: str | None,
) -> None:
    assert (
        render.effective_effort(requested, levels, dynamic_allowed=dynamic) == expected
    )


def test_profile_snapshot_carries_tier_metadata(fake_gateway: _Gateway) -> None:
    _snapshot, profile, _settings = _render(profile="alt")
    assert profile["schema"] == 1
    assert profile["profile"] == "alt"
    assert profile["tiers"] == {
        "fable": "sub/model-a",
        "opus": "sub/model-b",
        "sonnet": "sub/model-a",
        "haiku": "ag/model-g",
    }
    models = profile["models"]
    assert isinstance(models, dict)
    assert models["sub/model-a"] == {
        "ref": "sub/model-a",
        "source": "sub",
        "source_type": "codex",
        "model_id": "model-a",
        "gateway_id": "model-a",
        "claude_id": "model-a[1m]",
        "openrouter": None,
        "context": 272000,
        "efforts": ["low", "medium", "high", "xhigh"],
        "dynamic_allowed": False,
        "display": "model-a",
        "pricing": None,
        "estimated": True,
    }
    assert models["ag/model-g"]["dynamic_allowed"] is True
    assert models["ag/model-g"]["efforts"] == ["low", "high"]


def test_profile_snapshot_pricing_and_estimate(fake_gateway: _Gateway) -> None:
    _snapshot, profile, _settings = _render()
    models = profile["models"]
    assert isinstance(models, dict)
    assert models["or/vendor/model-c"]["pricing"] == {
        "prompt": 0.000002,
        "completion": 0.00001,
        "input_cache_read": 0.0000002,
        "input_cache_write": None,
    }
    assert models["or/vendor/model-c"]["estimated"] is False
    assert models["plan/model-f"]["estimated"] is True
    assert models["plan/model-f"]["efforts"] == ["low", "medium", "high"]


# -----------------------------------------------------------------------------
# compact 窗口


@pytest.mark.parametrize(
    ("contexts", "factor", "expected"),
    [
        ((128000, 400000), 0.95, 121600),
        ((1_000_000,), 0.999, 999000),
        ((105_264,), 0.95, 100000),
        ((105_263,), 0.95, None),
        ((1_000_000,), 1.0, None),
    ],
)
def test_compact_window_bounds(
    contexts: tuple[int, ...], factor: float, expected: int | None
) -> None:
    assert render.compact_window(contexts, factor) == expected


def test_compact_window_env_written_in_range(fake_gateway: _Gateway) -> None:
    _snapshot, profile, settings = _render()
    assert _env(settings)["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "121600"
    assert profile["compact_window"] == 121600


def test_compact_window_out_of_range_not_written(fake_gateway: _Gateway) -> None:
    _snapshot, profile, settings = _render(overrides={"haiku": "sub/model-e"})
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in _env(settings)
    assert profile["compact_window"] is None


# -----------------------------------------------------------------------------
# 临时替换与错误


def test_override_applies_only_to_this_snapshot(fake_gateway: _Gateway) -> None:
    config = _config()
    _snapshot, profile, settings = _render(config, overrides={"fable": "ag/model-g"})
    assert _env(settings)["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "model-g[1m]"
    tiers = profile["tiers"]
    assert isinstance(tiers, dict)
    assert tiers["fable"] == "ag/model-g"
    assert config.profiles["daily"].fable == "or/vendor/model-c"
    _snapshot2, profile2, _settings2 = _render(config)
    tiers2 = profile2["tiers"]
    assert isinstance(tiers2, dict)
    assert tiers2["fable"] == "or/vendor/model-c"


@pytest.mark.parametrize(
    "overrides",
    [{"turbo": "sub/model-a"}, {"fable": "sub/model-zzz"}, {"fable": "nope"}],
)
def test_invalid_override_raises(
    fake_gateway: _Gateway, overrides: dict[str, str]
) -> None:
    with pytest.raises(ConfigError):
        _render(overrides=overrides)


def test_definitions_failure_raises_without_snapshot(fake_gateway: _Gateway) -> None:
    fake_gateway.definitions_error = GatewayError("网关 x 请求失败：refused")
    with pytest.raises(GatewayError, match="refused"):
        _render()
    assert not paths.sessions_dir().exists()


def test_models_failure_raises(fake_gateway: _Gateway) -> None:
    fake_gateway.models_error = GatewayError("网关 y 返回 HTTP 500")
    with pytest.raises(GatewayError, match="500"):
        _render()


def test_definitions_are_fetched_per_channel_with_management_key(
    fake_gateway: _Gateway,
) -> None:
    _render()
    assert sorted(call for call in fake_gateway.calls if call[0] == "definitions") == [
        ("definitions", MANAGEMENT_KEY, "antigravity"),
        ("definitions", MANAGEMENT_KEY, "codex"),
    ]
    assert ("models", CLIENT_KEY, "") in fake_gateway.calls


def test_unregistered_tier_model_raises(fake_gateway: _Gateway) -> None:
    fake_gateway.registered.discard("plan/model-f")
    with pytest.raises(render.RenderError, match="claudex gateway restart"):
        _render()


def _add_contextless_model(data: dict[str, object]) -> None:
    _sources(data)["beta"] = {
        "type": "openai",
        "base_url": "https://example.invalid/v1",
        "models": ["model-h"],
    }


def test_missing_context_is_config_error(fake_gateway: _Gateway) -> None:
    _write_key(paths.source_key_file("beta"), "sk-FAKE-beta")
    with pytest.raises(ConfigError, match="beta/model-h"):
        _render(_config(_add_contextless_model))


# -----------------------------------------------------------------------------
# 元数据输入


def test_model_inputs_filter_unknown_effort_levels() -> None:
    metas = render.model_inputs(_config(), _catalog(), _definitions())
    assert metas["sub/model-a"].efforts == ("low", "medium", "high", "xhigh")
    assert metas["sub/model-e"].efforts is None
    assert metas["or/vendor/model-d"] == ModelMeta(
        context=131072, efforts=None, display="Vendor: Model D"
    )
    assert set(metas) == {
        "sub/model-a",
        "sub/model-b",
        "sub/model-e",
        "ag/model-g",
        "or/vendor/model-c",
        "or/vendor/model-d",
        "plan/model-f",
    }


def test_model_inputs_without_definitions_skip_subscription_models() -> None:
    metas = render.model_inputs(_config(), _catalog(), {})
    assert set(metas) == {
        "sub/model-b",
        "or/vendor/model-c",
        "or/vendor/model-d",
        "plan/model-f",
    }


def test_gateway_inputs_feed_prepare_gateway() -> None:
    metas = render.model_inputs(_config(), _catalog(), {})
    contexts, efforts = render.gateway_inputs(metas)
    assert contexts["or/vendor/model-c"] == 400000
    assert contexts["plan/model-f"] == 256000
    assert efforts == {
        "sub/model-b": ("low", "high"),
        "or/vendor/model-c": ("low", "medium", "high"),
        "plan/model-f": ("low", "medium", "high"),
    }


# -----------------------------------------------------------------------------
# 修改时间与清理


def _age(path: Path, days: float) -> None:
    moment = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (moment, moment))


def _fake_snapshot(stem: str) -> render.Snapshot:
    directory = paths.sessions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = render.Snapshot(
        profile_file=directory / f"{stem}.profile.json",
        settings_file=directory / f"{stem}.settings.json",
    )
    snapshot.profile_file.write_text("{}", encoding="utf-8")
    snapshot.settings_file.write_text("{}", encoding="utf-8")
    return snapshot


def test_prune_removes_only_stale_snapshots() -> None:
    stale = _fake_snapshot("daily-aaaaaaaaaaaa")
    fresh = _fake_snapshot("daily-bbbbbbbbbbbb")
    other = paths.sessions_dir() / "notes.json"
    other.write_text("{}", encoding="utf-8")
    for path in (stale.profile_file, stale.settings_file, other):
        _age(path, 8)
    removed = render.prune_snapshots()
    assert removed == sorted([stale.profile_file, stale.settings_file])
    assert fresh.profile_file.exists()
    assert fresh.settings_file.exists()
    assert other.exists()


def test_touched_snapshot_survives_prune() -> None:
    kept = _fake_snapshot("daily-cccccccccccc")
    for path in (kept.profile_file, kept.settings_file):
        _age(path, 30)
    render.touch_snapshot(kept.profile_file)
    assert render.prune_snapshots() == []
    assert kept.settings_file.exists()


def test_prune_honours_max_age_and_now() -> None:
    snapshot = _fake_snapshot("alt-dddddddddddd")
    for path in (snapshot.profile_file, snapshot.settings_file):
        _age(path, 2)
    assert render.prune_snapshots(timedelta(days=3)) == []
    later = datetime.now(UTC) + timedelta(days=2)
    assert len(render.prune_snapshots(timedelta(days=3), now=later)) == 2


def test_render_prunes_stale_and_refreshes_reused(fake_gateway: _Gateway) -> None:
    stale = _fake_snapshot("old-eeeeeeeeeeee")
    first, _p, _s = _render()
    for path in (stale.profile_file, stale.settings_file, first.profile_file):
        _age(path, 8)
    _age(first.settings_file, 8)
    second, _p2, _s2 = _render()
    assert second == first
    assert first.profile_file.exists()
    assert first.settings_file.exists()
    assert not stale.profile_file.exists()
    assert not stale.settings_file.exists()


# -----------------------------------------------------------------------------
# Fast 提示


def test_fast_notice_when_fable_is_not_codex(fake_gateway: _Gateway) -> None:
    _snapshot, profile, _settings = _render()
    notice = render.fast_notice(cast("render.ProfileSnapshot", profile))
    assert notice is not None
    assert "or/vendor-model-c" in notice
    assert "opus=model-a" in notice
    assert "haiku=model-b" in notice
    assert "model-e" in notice


def test_no_fast_notice_when_fable_is_codex(fake_gateway: _Gateway) -> None:
    _snapshot, profile, _settings = _render(profile="alt")
    assert render.fast_notice(cast("render.ProfileSnapshot", profile)) is None


# -----------------------------------------------------------------------------
# preflight


def _categories(items: list[render.Diagnostic]) -> list[str]:
    return [item.category for item in items]


def test_preflight_clean_profile(fake_gateway: _Gateway) -> None:
    report = render.preflight(_config(), profile="daily", check_gateway=True)
    assert report.errors == []
    assert report.unavailable == []
    assert set(_categories(report.warnings)) == {"config"}
    assert report.exit_code() == 0
    data = report.to_json()
    assert list(data) == [
        "schema",
        "profile",
        "online_checks",
        "errors",
        "warnings",
        "unavailable",
    ]
    assert data["schema"] == 1
    assert data["profile"] == "daily"
    assert data["online_checks"] is True
    warnings = data["warnings"]
    assert isinstance(warnings, list)
    assert set(warnings[0]) == {"category", "message", "model", "profile"}
    json.dumps(data)


def test_preflight_reports_generated_base_keys(fake_gateway: _Gateway) -> None:
    report = render.preflight(_config(), profile="daily", check_gateway=True)
    messages = " ".join(item.message for item in report.warnings)
    assert "env.CLAUDEX_FAST" in messages
    assert "statusLine" not in messages


def test_preflight_gateway_model_missing(fake_gateway: _Gateway) -> None:
    fake_gateway.registered -= {"plan/model-f", "or/vendor-model-d"}
    report = render.preflight(_config(), profile="daily", check_gateway=True)
    assert [(d.category, d.model, d.profile) for d in report.errors] == [
        ("gateway_model", "plan/model-f", "daily")
    ]
    assert [(d.category, d.model, d.profile) for d in report.unavailable] == [
        ("gateway_model", "or/vendor/model-d", None)
    ]
    assert report.exit_code() == 1


def test_preflight_subscription_definition_missing(fake_gateway: _Gateway) -> None:
    fake_gateway.definitions["codex"] = {
        "models": [{"id": "model-b", "context_length": 128000}]
    }
    report = render.preflight(_config(), profile="daily", check_gateway=True)
    errors = [(d.category, d.model) for d in report.errors]
    assert ("subscription_definition", "sub/model-a") in errors
    assert ("context_missing", "sub/model-a") in errors
    assert ("subscription_definition", "sub/model-e") in [
        (d.category, d.model) for d in report.unavailable
    ]


def test_preflight_catalog_entry_problems(fake_gateway: _Gateway) -> None:
    broken = Catalog(
        fetched_at=NOW,
        models={
            key: value
            for key, value in _catalog().models.items()
            if key not in ("vendor/model-d", "vendor/model-f")
        },
        skipped=("vendor/model-f",),
    )
    catalog.save_catalog(broken)
    report = render.preflight(_config(), profile="daily", check_gateway=True)
    warnings = [(d.category, d.model, d.profile) for d in report.warnings]
    assert ("catalog_entry", "or/vendor/model-d", None) in warnings
    assert ("catalog_unparsed", "plan/model-f", "daily") in warnings
    assert ("context_missing", "or/vendor/model-d") in [
        (d.category, d.model) for d in report.errors
    ]


def test_preflight_offline_skips_gateway(fake_gateway: _Gateway) -> None:
    report = render.preflight(_config(), profile="daily", check_gateway=False)
    assert fake_gateway.calls == []
    assert report.online_checks is False
    assert report.to_json()["online_checks"] is False
    assert report.errors == []


def test_preflight_offline_without_catalog_cache(fake_gateway: _Gateway) -> None:
    paths.catalog_file().unlink()
    report = render.preflight(_config(), profile="daily", check_gateway=False)
    assert _categories(report.errors) == ["catalog"]


def test_preflight_gateway_unreachable(fake_gateway: _Gateway) -> None:
    fake_gateway.models_error = GatewayError("网关 z 请求失败：refused")
    report = render.preflight(_config(), profile="daily", check_gateway=True)
    assert "gateway_access" in _categories(report.errors)
    assert report.exit_code() == 1


def test_preflight_unknown_profile(fake_gateway: _Gateway) -> None:
    report = render.preflight(_config(), profile="nope", check_gateway=True)
    assert [(d.category, d.profile) for d in report.errors] == [("config", "nope")]
    assert report.exit_code() == 1


def test_preflight_categories_are_closed() -> None:
    with pytest.raises(ValueError, match="诊断类别"):
        render.Diagnostic("made_up", "x")
