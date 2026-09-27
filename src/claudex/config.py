"""用户配置 `claudex.toml` 的解析与校验，以及模型引用的解析。

配置声明来源（五种 type）、每个来源挑选的模型与 profile 的四档引用；这里只做结构
校验与引用解析，不访问网络。校验失败一律抛 `ConfigError`，消息写明位置、期望与实际。
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

SUBSCRIPTION_TYPES = ("codex", "antigravity")
GENERIC_TYPES = ("openrouter", "openai", "anthropic")
SOURCE_TYPES = (*SUBSCRIPTION_TYPES, *GENERIC_TYPES)
TIERS = ("fable", "opus", "sonnet", "haiku")
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_COMPACT_WINDOW_FACTOR = 0.95
TOP_LEVEL_KEYS = (
    "default_profile",
    "mcp_deny",
    "compact_window_factor",
    "sources",
    "profiles",
)
MODEL_KEYS = ("id", "openrouter", "context", "efforts", "display")
# 各 type 的来源表允许出现的键
SOURCE_KEYS = {
    "codex": ("type", "models"),
    "antigravity": ("type", "models"),
    "openrouter": ("type", "models"),
    "openai": ("type", "base_url", "models"),
    "anthropic": ("type", "base_url", "models"),
}
# 来源名与 profile 名共用的字符规则
_NAME = re.compile(r"[a-z0-9-]+")
_URL_SCHEMES = ("http://", "https://")


class ConfigError(ValueError):
    """`claudex.toml` 的结构或引用错误。"""


@dataclass(frozen=True)
class ModelEntry:
    """来源里列出的一个模型。"""

    id: str
    openrouter: str | None
    context: int | None
    efforts: tuple[str, ...] | None
    display: str | None


@dataclass(frozen=True)
class Source:
    """一个来源：type、可选 base_url 与挑选的模型。"""

    name: str
    type: str
    base_url: str | None
    models: tuple[ModelEntry, ...]


@dataclass(frozen=True)
class Profile:
    """一个 profile：四档各一个 `<来源名>/<模型 id>` 引用。"""

    name: str
    fable: str
    opus: str
    sonnet: str
    haiku: str


@dataclass(frozen=True)
class Config:
    """校验过的完整配置。"""

    default_profile: str
    mcp_deny: tuple[str, ...]
    compact_window_factor: float
    sources: dict[str, Source]
    profiles: dict[str, Profile]


@dataclass(frozen=True)
class ModelRef:
    """解析后的模型引用。

    `gateway_id` 是模型在网关里的 id：通用来源为 `<来源名>/<网关别名>`，网关别名由
    模型 id 把 `/` 换成 `-` 得到；订阅来源为上游 id 本身。
    """

    ref: str
    source: str
    source_type: str
    model_id: str
    gateway_id: str
    openrouter: str | None
    context: int | None
    efforts: tuple[str, ...] | None
    display: str | None


def _kind(value: object) -> str:
    return type(value).__name__


def _require_table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} 必须是表，收到 {_kind(value)} {value!r}")
    return value


def _reject_unknown_keys(
    table: dict[str, object], allowed: tuple[str, ...], where: str
) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ConfigError(f"{where} 含未知键 {unknown}，只接受 {list(allowed)}")


def _non_empty_string(value: object, where: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ConfigError(f"{where} 必须是非空字符串，收到 {value!r}")
    return value


def _positive_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where} 必须是正整数，收到 {value!r}")
    return value


def _parse_efforts(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} 必须是非空列表，收到 {value!r}")
    for index, level in enumerate(value):
        if level not in EFFORT_LEVELS:
            raise ConfigError(
                f"{where}[{index}] 必须取自 {list(EFFORT_LEVELS)}，收到 {level!r}"
            )
    if len(set(value)) != len(value):
        raise ConfigError(f"{where} 不得有重复档位，收到 {value!r}")
    return tuple(str(level) for level in value)


def _parse_model(value: object, source_type: str, where: str) -> ModelEntry:
    default_slug_from_id = source_type == "openrouter"
    if isinstance(value, str):
        model_id = _non_empty_string(value, where)
        return ModelEntry(
            id=model_id,
            openrouter=model_id if default_slug_from_id else None,
            context=None,
            efforts=None,
            display=None,
        )
    table = _require_table(value, where)
    _reject_unknown_keys(table, MODEL_KEYS, where)
    if "id" not in table:
        raise ConfigError(f"{where}.id 必填")
    model_id = _non_empty_string(table["id"], f"{where}.id")
    slug = table.get("openrouter")
    openrouter = (
        _non_empty_string(slug, f"{where}.openrouter")
        if "openrouter" in table
        else None
    )
    if openrouter is None and default_slug_from_id:
        openrouter = model_id
    return ModelEntry(
        id=model_id,
        openrouter=openrouter,
        context=(
            _positive_int(table["context"], f"{where}.context")
            if "context" in table
            else None
        ),
        efforts=(
            _parse_efforts(table["efforts"], f"{where}.efforts")
            if "efforts" in table
            else None
        ),
        display=(
            _non_empty_string(table["display"], f"{where}.display")
            if "display" in table
            else None
        ),
    )


def _gateway_alias(model_id: str) -> str:
    """通用来源的模型在网关里的别名：id 里的 `/` 换成 `-`。"""
    return model_id.replace("/", "-")


def _parse_source(name: str, value: object) -> Source:
    where = f"sources.{name}"
    if _NAME.fullmatch(name) is None:
        raise ConfigError(f"{where}：来源名必须匹配 [a-z0-9-]+，收到 {name!r}")
    table = _require_table(value, where)
    source_type = table.get("type")
    if source_type not in SOURCE_TYPES:
        raise ConfigError(
            f"{where}.type 必须是 {list(SOURCE_TYPES)} 之一，收到 {source_type!r}"
        )
    assert isinstance(source_type, str)
    _reject_unknown_keys(table, SOURCE_KEYS[source_type], where)
    base_url: str | None = None
    if "base_url" in SOURCE_KEYS[source_type]:
        if "base_url" not in table:
            raise ConfigError(f"{where}.base_url 对 {source_type} 来源必填")
        raw_url = table["base_url"]
        if not isinstance(raw_url, str) or not raw_url.startswith(_URL_SCHEMES):
            raise ConfigError(
                f"{where}.base_url 必须是以 http:// 或 https:// 开头的字符串，"
                f"收到 {raw_url!r}"
            )
        base_url = raw_url
    raw_models = table.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ConfigError(f"{where}.models 必须是非空列表，收到 {raw_models!r}")
    models: list[ModelEntry] = []
    seen: set[str] = set()
    # 通用来源的网关别名由 id 把 `/` 换成 `-` 得到，不同 id 可能落到同一别名
    aliases: dict[str, str] = {}
    for index, raw_model in enumerate(raw_models):
        model_where = f"{where}.models[{index}]"
        model = _parse_model(raw_model, source_type, model_where)
        if model.id in seen:
            raise ConfigError(f"{model_where}：模型 id {model.id!r} 在该来源里重复")
        seen.add(model.id)
        if source_type in GENERIC_TYPES:
            alias = _gateway_alias(model.id)
            if alias in aliases:
                raise ConfigError(
                    f"{model_where}：模型 id {model.id!r} 与 {aliases[alias]!r} "
                    f"生成同一个网关 id {name}/{alias}"
                )
            aliases[alias] = model.id
        models.append(model)
    return Source(name=name, type=source_type, base_url=base_url, models=tuple(models))


def _resolve(sources: dict[str, Source], ref: str, where: str) -> ModelRef:
    source_name, separator, model_id = ref.partition("/")
    if not separator or not source_name or not model_id:
        raise ConfigError(f"{where} 必须是 <来源名>/<模型 id>，收到 {ref!r}")
    source = sources.get(source_name)
    if source is None:
        raise ConfigError(
            f"{where} 引用的来源 {source_name!r} 不存在，已定义 {sorted(sources)}"
        )
    entry = next((model for model in source.models if model.id == model_id), None)
    if entry is None:
        listed = [model.id for model in source.models]
        raise ConfigError(
            f"{where} 引用的模型 {model_id!r} 不在来源 {source_name!r} 的 models 里，"
            f"已列出 {listed}"
        )
    gateway_id = (
        model_id
        if source.type in SUBSCRIPTION_TYPES
        else f"{source_name}/{_gateway_alias(model_id)}"
    )
    return ModelRef(
        ref=ref,
        source=source_name,
        source_type=source.type,
        model_id=model_id,
        gateway_id=gateway_id,
        openrouter=entry.openrouter,
        context=entry.context,
        efforts=entry.efforts,
        display=entry.display,
    )


def _parse_profile(name: str, value: object, sources: dict[str, Source]) -> Profile:
    where = f"profiles.{name}"
    # profile 名会拼进会话快照的文件名，规则与来源名相同
    if _NAME.fullmatch(name) is None:
        raise ConfigError(f"{where}：profile 名必须匹配 [a-z0-9-]+，收到 {name!r}")
    table = _require_table(value, where)
    _reject_unknown_keys(table, TIERS, where)
    refs: dict[str, str] = {}
    for tier in TIERS:
        tier_where = f"{where}.{tier}"
        if tier not in table:
            raise ConfigError(f"{tier_where} 必填")
        ref = _non_empty_string(table[tier], tier_where)
        _resolve(sources, ref, tier_where)
        refs[tier] = ref
    return Profile(
        name=name,
        fable=refs["fable"],
        opus=refs["opus"],
        sonnet=refs["sonnet"],
        haiku=refs["haiku"],
    )


def _reject_duplicate_subscription_sources(sources: dict[str, Source]) -> None:
    """每种订阅 type 最多一个来源：订阅模型在网关里的 id 就是上游 id，两个同 type
    来源无法区分。"""
    first_by_type: dict[str, str] = {}
    for name, source in sources.items():
        if source.type not in SUBSCRIPTION_TYPES:
            continue
        first = first_by_type.get(source.type)
        if first is not None:
            raise ConfigError(
                f"sources.{name}：{source.type} 类来源最多一个，已有 {first!r}"
            )
        first_by_type[source.type] = name


def _reject_shared_subscription_model_ids(sources: dict[str, Source]) -> None:
    """不同订阅来源不得列出同一个上游 id：订阅模型在网关里的 id 就是上游 id，网关会
    把同名模型并成一条、在两个 OAuth 通道之间路由，无法指定走哪个来源。"""
    owner_by_id: dict[str, str] = {}
    for name, source in sources.items():
        if source.type not in SUBSCRIPTION_TYPES:
            continue
        for index, model in enumerate(source.models):
            owner = owner_by_id.get(model.id)
            if owner is not None:
                raise ConfigError(
                    f"sources.{name}.models[{index}]：模型 id {model.id!r} "
                    f"已由订阅来源 {owner!r} 列出，订阅来源 {name!r} 不得再列；"
                    "两者在网关里是同一个 id"
                )
            owner_by_id[model.id] = name


def _parse_mcp_deny(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"mcp_deny 必须是字符串列表，收到 {value!r}")
    for index, pattern in enumerate(value):
        if not isinstance(pattern, str):
            raise ConfigError(f"mcp_deny[{index}] 必须是字符串，收到 {pattern!r}")
    return tuple(value)


def _parse_compact_window_factor(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= 1
    ):
        raise ConfigError(f"compact_window_factor 必须落在 (0, 1]，收到 {value!r}")
    return float(value)


def parse_config(data: dict[str, object]) -> Config:
    """校验已解析的 TOML 数据并返回 `Config`。

    参数
    ----------
    data : dict[str, object]
        `tomllib` 的解析结果，或测试里直接构造的同形字典。

    返回
    ----------
    Config
        来源与 profile 按配置里的顺序保留；`openrouter` 类来源的模型未写
        `openrouter` 时取其 `id`。

    异常
    ----------
    ConfigError
        任一结构规则或引用规则不满足。
    """
    _reject_unknown_keys(data, TOP_LEVEL_KEYS, "顶层")
    raw_sources = _require_table(data.get("sources"), "sources")
    if not raw_sources:
        raise ConfigError("sources 至少要定义一个来源")
    sources = {
        str(name): _parse_source(str(name), value)
        for name, value in raw_sources.items()
    }
    _reject_duplicate_subscription_sources(sources)
    _reject_shared_subscription_model_ids(sources)
    raw_profiles = _require_table(data.get("profiles"), "profiles")
    if not raw_profiles:
        raise ConfigError("profiles 至少要定义一个 profile")
    profiles = {
        str(name): _parse_profile(str(name), value, sources)
        for name, value in raw_profiles.items()
    }
    if "default_profile" not in data:
        raise ConfigError("default_profile 必填")
    default_profile = data["default_profile"]
    if default_profile not in profiles:
        raise ConfigError(
            f"default_profile 必须是已定义的 profile {sorted(profiles)} 之一，"
            f"收到 {default_profile!r}"
        )
    assert isinstance(default_profile, str)
    return Config(
        default_profile=default_profile,
        mcp_deny=_parse_mcp_deny(data["mcp_deny"]) if "mcp_deny" in data else (),
        compact_window_factor=(
            _parse_compact_window_factor(data["compact_window_factor"])
            if "compact_window_factor" in data
            else DEFAULT_COMPACT_WINDOW_FACTOR
        ),
        sources=sources,
        profiles=profiles,
    )


def load_config(path: Path) -> Config:
    """读取并校验 `claudex.toml`。

    异常
    ----------
    OSError
        文件读不到（含不存在），原样抛出。
    ConfigError
        TOML 语法错误（消息含路径）或 `parse_config` 的校验失败。
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path}: TOML 语法错误：{err}") from err
    return parse_config(data)


def resolve_ref(config: Config, ref: str) -> ModelRef:
    """解析 `<来源名>/<模型 id>` 引用；在第一个 `/` 处切开，模型 id 可再含 `/`。

    异常
    ----------
    ConfigError
        形态不对、来源不存在，或模型不在该来源的 models 里。
    """
    return _resolve(config.sources, ref, "模型引用")


def profile_refs(config: Config, name: str) -> dict[str, ModelRef]:
    """返回 profile `name` 四档到解析后引用的映射，按 fable、opus、sonnet、haiku 排列。

    异常
    ----------
    ConfigError
        profile 不存在。
    """
    profile = config.profiles.get(name)
    if profile is None:
        raise ConfigError(f"profile {name!r} 不存在，已定义 {sorted(config.profiles)}")
    return {
        tier: _resolve(
            config.sources, getattr(profile, tier), f"profiles.{name}.{tier}"
        )
        for tier in TIERS
    }
