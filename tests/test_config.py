"""claudex.config：`claudex.toml` 的结构校验与模型引用解析。"""

import copy
from collections.abc import Callable
from pathlib import Path

import pytest

from claudex import config
from claudex.config import ConfigError


def valid_data() -> dict[str, object]:
    """覆盖五种 type 的最小合法配置。"""
    return {
        "default_profile": "daily",
        "mcp_deny": ["mcp__alpha__*"],
        "compact_window_factor": 0.9,
        "sources": {
            "sub": {"type": "codex", "models": ["alpha-1", "alpha-2"]},
            "ag": {"type": "antigravity", "models": ["beta-1"]},
            "or": {
                "type": "openrouter",
                "models": [
                    "acme/gamma-1",
                    {"id": "acme/gamma-2", "openrouter": "acme/gamma-2-alt"},
                ],
            },
            "oa": {
                "type": "openai",
                "base_url": "https://oa.example.invalid/v1",
                "models": ["delta-1"],
            },
            "acme": {
                "type": "anthropic",
                "base_url": "http://acme.example.invalid/anthropic",
                "models": [
                    {"id": "epsilon-1", "openrouter": "acme/epsilon-1"},
                    {
                        "id": "epsilon-2",
                        "context": 256000,
                        "efforts": ["low", "high"],
                        "display": "Epsilon Two",
                    },
                ],
            },
        },
        "profiles": {
            "daily": {
                "fable": "or/acme/gamma-1",
                "opus": "sub/alpha-1",
                "sonnet": "acme/epsilon-2",
                "haiku": "ag/beta-1",
            },
            "alt": {
                "fable": "oa/delta-1",
                "opus": "sub/alpha-2",
                "sonnet": "or/acme/gamma-2",
                "haiku": "acme/epsilon-1",
            },
        },
    }


def _sources(data: dict[str, object]) -> dict[str, object]:
    sources = data["sources"]
    assert isinstance(sources, dict)
    return sources


def _source(data: dict[str, object], name: str) -> dict[str, object]:
    source = _sources(data)[name]
    assert isinstance(source, dict)
    return source


def _profile(data: dict[str, object], name: str) -> dict[str, object]:
    profiles = data["profiles"]
    assert isinstance(profiles, dict)
    profile = profiles[name]
    assert isinstance(profile, dict)
    return profile


def _models(data: dict[str, object], source: str) -> list[object]:
    models = _source(data, source)["models"]
    assert isinstance(models, list)
    return models


# -----------------------------------------------------------------------------
# 合法配置
# -----------------------------------------------------------------------------


def test_valid_config_parses_into_structure() -> None:
    parsed = config.parse_config(valid_data())
    assert parsed.default_profile == "daily"
    assert parsed.mcp_deny == ("mcp__alpha__*",)
    assert parsed.compact_window_factor == 0.9
    assert list(parsed.sources) == ["sub", "ag", "or", "oa", "acme"]
    assert parsed.sources["sub"] == config.Source(
        name="sub",
        type="codex",
        base_url=None,
        models=(
            config.ModelEntry("alpha-1", None, None, None, None),
            config.ModelEntry("alpha-2", None, None, None, None),
        ),
    )
    assert parsed.sources["oa"].base_url == "https://oa.example.invalid/v1"
    assert parsed.sources["acme"].models[1] == config.ModelEntry(
        id="epsilon-2",
        openrouter=None,
        context=256000,
        efforts=("low", "high"),
        display="Epsilon Two",
    )
    assert parsed.profiles["alt"] == config.Profile(
        name="alt",
        fable="oa/delta-1",
        opus="sub/alpha-2",
        sonnet="or/acme/gamma-2",
        haiku="acme/epsilon-1",
    )


def test_optional_top_level_keys_have_defaults() -> None:
    data = valid_data()
    del data["mcp_deny"]
    del data["compact_window_factor"]
    parsed = config.parse_config(data)
    assert parsed.mcp_deny == ()
    assert parsed.compact_window_factor == 0.95


def test_subscription_source_accepts_table_entries_without_slug_default() -> None:
    data = valid_data()
    _models(data, "sub")[1] = {
        "id": "alpha-2",
        "openrouter": "acme/alpha-2",
        "context": 400000,
        "efforts": ["low", "medium"],
        "display": "Alpha Two",
    }
    _models(data, "ag")[0] = {"id": "beta-1"}
    parsed = config.parse_config(data)
    assert parsed.sources["sub"].models[1] == config.ModelEntry(
        id="alpha-2",
        openrouter="acme/alpha-2",
        context=400000,
        efforts=("low", "medium"),
        display="Alpha Two",
    )
    assert parsed.sources["ag"].models[0].openrouter is None
    resolved = config.resolve_ref(parsed, "sub/alpha-2")
    assert resolved.gateway_id == "alpha-2"
    assert resolved.openrouter == "acme/alpha-2"


def test_integer_compact_window_factor_of_one_is_accepted() -> None:
    data = valid_data()
    data["compact_window_factor"] = 1
    assert config.parse_config(data).compact_window_factor == 1.0


def test_openrouter_slug_defaults_to_id_only_for_openrouter_sources() -> None:
    parsed = config.parse_config(valid_data())
    gamma_1, gamma_2 = parsed.sources["or"].models
    assert gamma_1.openrouter == "acme/gamma-1"
    assert gamma_2.openrouter == "acme/gamma-2-alt"
    assert parsed.sources["oa"].models[0].openrouter is None
    assert parsed.sources["acme"].models[1].openrouter is None


def test_load_config_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "claudex.toml"
    path.write_text(
        'default_profile = "p"\n'
        "[sources.or]\n"
        'type = "openrouter"\n'
        'models = ["acme/gamma-1"]\n'
        "[profiles.p]\n"
        'fable = "or/acme/gamma-1"\n'
        'opus = "or/acme/gamma-1"\n'
        'sonnet = "or/acme/gamma-1"\n'
        'haiku = "or/acme/gamma-1"\n',
        encoding="utf-8",
    )
    parsed = config.load_config(path)
    assert parsed.profiles["p"].fable == "or/acme/gamma-1"


def test_load_config_reports_toml_syntax_error_with_path(tmp_path: Path) -> None:
    path = tmp_path / "claudex.toml"
    path.write_text("default_profile = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML") as caught:
        config.load_config(path)
    assert str(path) in str(caught.value)


# -----------------------------------------------------------------------------
# 非法配置：每条规则至少一例，消息须含位置
# -----------------------------------------------------------------------------


def _set_top(key: str, value: object) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        data[key] = value

    return mutate


def _drop_top(key: str) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        del data[key]

    return mutate


def _set_source(name: str, value: object) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        _sources(data)[name] = value

    return mutate


def _set_source_key(
    name: str, key: str, value: object
) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        _source(data, name)[key] = value

    return mutate


def _drop_source_key(name: str, key: str) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        del _source(data, name)[key]

    return mutate


def _set_model(
    source: str, index: int, value: object
) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        _models(data, source)[index] = value

    return mutate


def _set_tier(
    profile: str, tier: str, value: object
) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        _profile(data, profile)[tier] = value

    return mutate


def _drop_tier(profile: str, tier: str) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        del _profile(data, profile)[tier]

    return mutate


def _clear_sources(data: dict[str, object]) -> None:
    data["sources"] = {}


def _clear_profiles(data: dict[str, object]) -> None:
    data["profiles"] = {}


def _rename_source(data: dict[str, object]) -> None:
    sources = _sources(data)
    sources["Bad_Name"] = sources.pop("ag")


def _append_model(source: str, value: object) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        _models(data, source).append(value)

    return mutate


def _rename_profile(new_name: str) -> Callable[[dict[str, object]], None]:
    def mutate(data: dict[str, object]) -> None:
        profiles = data["profiles"]
        assert isinstance(profiles, dict)
        profiles[new_name] = profiles.pop("alt")

    return mutate


INVALID_CASES: list[tuple[str, Callable[[dict[str, object]], None], str]] = [
    ("unknown-top-key", _set_top("extra", 1), "顶层"),
    ("missing-default-profile", _drop_top("default_profile"), "default_profile"),
    (
        "undefined-default-profile",
        _set_top("default_profile", "nope"),
        "default_profile",
    ),
    ("mcp-deny-not-list", _set_top("mcp_deny", "mcp__x__*"), "mcp_deny"),
    ("mcp-deny-non-string", _set_top("mcp_deny", ["ok", 3]), "mcp_deny[1]"),
    ("factor-zero", _set_top("compact_window_factor", 0), "compact_window_factor"),
    (
        "factor-above-one",
        _set_top("compact_window_factor", 1.5),
        "compact_window_factor",
    ),
    ("factor-bool", _set_top("compact_window_factor", True), "compact_window_factor"),
    ("missing-sources", _drop_top("sources"), "sources"),
    ("empty-sources", _clear_sources, "sources"),
    ("missing-profiles", _drop_top("profiles"), "profiles"),
    ("empty-profiles", _clear_profiles, "profiles"),
    ("bad-source-name", _rename_source, "sources.Bad_Name"),
    ("source-not-table", _set_source("ag", "beta-1"), "sources.ag"),
    ("unknown-type", _set_source_key("ag", "type", "vertex"), "sources.ag.type"),
    ("missing-type", _drop_source_key("ag", "type"), "sources.ag.type"),
    (
        "subscription-base-url",
        _set_source_key("sub", "base_url", "https://x.example.invalid"),
        "sources.sub",
    ),
    (
        "openrouter-base-url",
        _set_source_key("or", "base_url", "https://x.example.invalid"),
        "sources.or",
    ),
    ("unknown-source-key", _set_source_key("oa", "key", "secret"), "sources.oa"),
    ("missing-base-url", _drop_source_key("acme", "base_url"), "sources.acme.base_url"),
    (
        "base-url-scheme",
        _set_source_key("oa", "base_url", "ftp://oa.example.invalid"),
        "sources.oa.base_url",
    ),
    (
        "base-url-not-string",
        _set_source_key("oa", "base_url", 5),
        "sources.oa.base_url",
    ),
    ("missing-models", _drop_source_key("oa", "models"), "sources.oa.models"),
    ("empty-models", _set_source_key("oa", "models", []), "sources.oa.models"),
    (
        "subscription-model-unknown-key",
        _set_model("sub", 0, {"id": "alpha-1", "price": 1}),
        "sources.sub.models[0]",
    ),
    ("empty-model-string", _set_model("oa", 0, ""), "sources.oa.models[0]"),
    ("model-not-string-or-table", _set_model("oa", 0, 7), "sources.oa.models[0]"),
    (
        "model-missing-id",
        _set_model("oa", 0, {"context": 1000}),
        "sources.oa.models[0].id",
    ),
    ("model-empty-id", _set_model("oa", 0, {"id": ""}), "sources.oa.models[0].id"),
    (
        "model-unknown-key",
        _set_model("oa", 0, {"id": "delta-1", "price": 1}),
        "sources.oa.models[0]",
    ),
    (
        "model-empty-openrouter",
        _set_model("oa", 0, {"id": "delta-1", "openrouter": ""}),
        "sources.oa.models[0].openrouter",
    ),
    (
        "model-context-zero",
        _set_model("acme", 1, {"id": "epsilon-2", "context": 0}),
        "sources.acme.models[1].context",
    ),
    (
        "model-context-bool",
        _set_model("acme", 1, {"id": "epsilon-2", "context": True}),
        "sources.acme.models[1].context",
    ),
    (
        "model-efforts-empty",
        _set_model("acme", 1, {"id": "epsilon-2", "efforts": []}),
        "sources.acme.models[1].efforts",
    ),
    (
        "model-efforts-unknown",
        _set_model("acme", 1, {"id": "epsilon-2", "efforts": ["low", "ultra"]}),
        "sources.acme.models[1].efforts[1]",
    ),
    (
        "model-efforts-duplicate",
        _set_model("acme", 1, {"id": "epsilon-2", "efforts": ["low", "low"]}),
        "sources.acme.models[1].efforts",
    ),
    (
        "model-empty-display",
        _set_model("acme", 1, {"id": "epsilon-2", "display": ""}),
        "sources.acme.models[1].display",
    ),
    ("duplicate-model-id", _set_model("sub", 1, "alpha-1"), "sources.sub.models[1]"),
    ("profile-not-table", _set_top("profiles", {"daily": "x"}), "profiles.daily"),
    ("profile-missing-tier", _drop_tier("daily", "haiku"), "profiles.daily.haiku"),
    (
        "profile-unknown-tier",
        _set_tier("daily", "mini", "sub/alpha-1"),
        "profiles.daily",
    ),
    ("tier-not-string", _set_tier("daily", "opus", 1), "profiles.daily.opus"),
    (
        "tier-without-slash",
        _set_tier("daily", "opus", "alpha-1"),
        "profiles.daily.opus",
    ),
    ("tier-empty-model", _set_tier("daily", "opus", "sub/"), "profiles.daily.opus"),
    (
        "tier-unknown-source",
        _set_tier("daily", "opus", "nope/alpha-1"),
        "profiles.daily.opus",
    ),
    (
        "tier-unlisted-model",
        _set_tier("daily", "opus", "sub/alpha-9"),
        "profiles.daily.opus",
    ),
    (
        "openrouter-gateway-alias-collision",
        _append_model("or", "acme-gamma-1"),
        "sources.or.models[2]",
    ),
    (
        "openai-gateway-alias-collision",
        _append_model("oa", {"id": "delta/1"}),
        "sources.oa.models[1]",
    ),
    (
        "second-codex-source",
        _set_source("sub2", {"type": "codex", "models": ["alpha-3"]}),
        "sources.sub2",
    ),
    (
        "second-antigravity-source",
        _set_source("ag2", {"type": "antigravity", "models": ["beta-2"]}),
        "sources.ag2",
    ),
    ("profile-name-uppercase", _rename_profile("Alt"), "profiles.Alt"),
    ("profile-name-slash", _rename_profile("a/b"), "profiles.a/b"),
    ("profile-name-dots", _rename_profile(".."), "profiles.."),
]


@pytest.mark.parametrize(
    ("mutate", "location"),
    [
        pytest.param(mutate, location, id=name)
        for name, mutate, location in INVALID_CASES
    ],
)
def test_invalid_config_is_rejected_with_location(
    mutate: Callable[[dict[str, object]], None], location: str
) -> None:
    data = copy.deepcopy(valid_data())
    mutate(data)
    with pytest.raises(ConfigError) as caught:
        config.parse_config(data)
    assert location in str(caught.value)


def test_gateway_alias_collision_names_both_ids() -> None:
    data = valid_data()
    _models(data, "or").append("acme-gamma-1")
    with pytest.raises(ConfigError) as caught:
        config.parse_config(data)
    message = str(caught.value)
    assert "'acme/gamma-1'" in message
    assert "'acme-gamma-1'" in message
    assert "or/acme-gamma-1" in message


def test_second_subscription_source_names_the_first() -> None:
    data = valid_data()
    _sources(data)["sub2"] = {"type": "codex", "models": ["alpha-3"]}
    with pytest.raises(ConfigError, match="'sub'"):
        config.parse_config(data)


@pytest.mark.parametrize(
    ("source", "entry", "location", "model_id"),
    [
        pytest.param("ag", "alpha-1", "sources.ag.models[1]", "alpha-1", id="string"),
        pytest.param(
            "ag", {"id": "alpha-2"}, "sources.ag.models[1]", "alpha-2", id="table"
        ),
        # 重复项追加在先列出的 sub 里时，报错位置仍落在后列出的 ag 上
        pytest.param(
            "sub", "beta-1", "sources.ag.models[0]", "beta-1", id="earlier-source"
        ),
    ],
)
def test_subscription_sources_must_not_share_model_id(
    source: str, entry: object, location: str, model_id: str
) -> None:
    data = valid_data()
    _models(data, source).append(entry)
    with pytest.raises(ConfigError) as caught:
        config.parse_config(data)
    message = str(caught.value)
    assert message.startswith(location)
    assert f"{model_id!r}" in message
    assert "'sub'" in message
    assert "'ag'" in message


def test_generic_source_may_reuse_subscription_model_id() -> None:
    data = valid_data()
    _models(data, "oa").append("alpha-1")
    parsed = config.parse_config(data)
    assert config.resolve_ref(parsed, "oa/alpha-1").gateway_id == "oa/alpha-1"


def test_valid_profile_name_with_digits_and_hyphen_is_accepted() -> None:
    data = valid_data()
    _rename_profile("alt-2")(data)
    assert "alt-2" in config.parse_config(data).profiles


# -----------------------------------------------------------------------------
# 引用解析
# -----------------------------------------------------------------------------


def test_resolve_ref_for_subscription_source_uses_upstream_id() -> None:
    parsed = config.parse_config(valid_data())
    resolved = config.resolve_ref(parsed, "sub/alpha-1")
    assert resolved == config.ModelRef(
        ref="sub/alpha-1",
        source="sub",
        source_type="codex",
        model_id="alpha-1",
        gateway_id="alpha-1",
        openrouter=None,
        context=None,
        efforts=None,
        display=None,
    )


def test_resolve_ref_for_generic_source_keeps_slash_in_model_id() -> None:
    parsed = config.parse_config(valid_data())
    resolved = config.resolve_ref(parsed, "or/acme/gamma-1")
    assert resolved.source == "or"
    assert resolved.source_type == "openrouter"
    assert resolved.model_id == "acme/gamma-1"
    assert resolved.gateway_id == "or/acme-gamma-1"
    assert resolved.openrouter == "acme/gamma-1"


def test_resolve_ref_carries_explicit_metadata() -> None:
    parsed = config.parse_config(valid_data())
    resolved = config.resolve_ref(parsed, "acme/epsilon-2")
    assert resolved.gateway_id == "acme/epsilon-2"
    assert resolved.context == 256000
    assert resolved.efforts == ("low", "high")
    assert resolved.display == "Epsilon Two"


@pytest.mark.parametrize(
    "ref", ["sub/alpha-9", "nope/alpha-1", "alpha-1", "/alpha-1", "sub/"]
)
def test_resolve_ref_rejects_invalid_reference(ref: str) -> None:
    parsed = config.parse_config(valid_data())
    with pytest.raises(ConfigError, match="模型引用"):
        config.resolve_ref(parsed, ref)


def test_profile_refs_maps_four_tiers() -> None:
    parsed = config.parse_config(valid_data())
    refs = config.profile_refs(parsed, "daily")
    assert list(refs) == ["fable", "opus", "sonnet", "haiku"]
    assert {tier: ref.gateway_id for tier, ref in refs.items()} == {
        "fable": "or/acme-gamma-1",
        "opus": "alpha-1",
        "sonnet": "acme/epsilon-2",
        "haiku": "beta-1",
    }


def test_profile_refs_rejects_unknown_profile() -> None:
    parsed = config.parse_config(valid_data())
    with pytest.raises(ConfigError, match="nope"):
        config.profile_refs(parsed, "nope")
