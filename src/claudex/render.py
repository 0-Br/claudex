"""会话快照的生成、派生 settings、快照清理与 preflight 诊断。

每次启动由 `render` 生成一份不可变快照：`<profile>-<摘要前 12 位>.profile.json`（状态栏
读的模型表）与同名 `.settings.json`（交给 Claude Code 的派生 settings），内容相同时复用
已有文件。元数据按 `catalog.model_metadata` 的取值顺序取，订阅来源的定义与网关模型列表
每次在线取，取不到即报错，不做离线降级。
"""

import hashlib
import json
import os
import re
import shlex
import sys
import sysconfig
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from claudex import catalog, gateway, jsonio, paths
from claudex.catalog import Catalog, CatalogError, ModelMeta
from claudex.config import (
    EFFORT_LEVELS,
    SUBSCRIPTION_TYPES,
    TIERS,
    Config,
    ConfigError,
    ModelRef,
    profile_refs,
    resolve_ref,
)
from claudex.gateway import GatewayError

PROFILE_SNAPSHOT_SCHEMA = 1
PREFLIGHT_SCHEMA = 1
SNAPSHOT_DIGEST_LENGTH = 12
SNAPSHOT_MAX_AGE = timedelta(days=7)
PROFILE_SUFFIX = ".profile.json"
SETTINGS_SUFFIX = ".settings.json"
# context 大于这个值的模型在交给 Claude Code 的 id 末尾加 `[1m]`：带 `[1m]` 的 id 按
# 1M 管理，不带的未知 id 按 200k；Claude Code 发请求前会剥掉后缀，网关收到的名字不变
CONTEXT_1M_THRESHOLD = 200_000
ONE_M_SUFFIX = "[1m]"
# Claude Code 把四档的规范 id 经 `modelOverrides` 映射到后端，`/effort` 也按这些 id 记
MODEL_OVERRIDE_KEYS = {
    "fable": ("claude-fable-5", "claude-fable-5-1"),
    "opus": ("claude-opus-5",),
    "sonnet": ("claude-sonnet-5",),
    "haiku": ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
}
# 生成器产出、不从基底透传的键
GENERATED_TOP_KEYS = (
    "model",
    "availableModels",
    "modelOverrides",
    "modelSettings",
    "apiKeyHelper",
)
GENERATED_ENV_PREFIXES = ("ANTHROPIC_DEFAULT_", "CLAUDEX_")
GENERATED_ENV_KEYS = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_AUTO_COMPACT_WINDOW")
CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"
# Fast 档的请求头由生成器按 `fast` 参数写进派生 env，基底里的同名头不透传
FAST_HEADER = "x-claudex-tier"
FAST_HEADER_LINE = "X-Claudex-Tier: fast"
FAST_ENV = "CLAUDEX_FAST"
PROFILE_ENV = "CLAUDEX_PROFILE"
PROFILE_FILE_ENV = "CLAUDEX_PROFILE_FILE"
STATUSLINE_COMMAND_ENV = "CLAUDEX_STATUSLINE_COMMAND"
COMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
COMPACT_WINDOW_MIN = 100_000
COMPACT_WINDOW_MAX = 1_000_000
STATUSLINE_DEFAULTS: dict[str, object] = {"type": "command", "refreshInterval": 1}
# Claude Code 持久化档位只接受这四个；DEFAULT 是用户没设档位时的客户端默认
CLIENT_EFFORT_LEVELS = ("low", "medium", "high", "xhigh")
DEFAULT_CLIENT_EFFORT = "high"
AUTO_EFFORT = "auto"
AUTO_FALLBACK_EFFORT = "medium"
FAST_SOURCE_TYPE = "codex"
PREFLIGHT_CATEGORIES = (
    "config",
    "catalog",
    "catalog_entry",
    "catalog_unparsed",
    "context_missing",
    "gateway_access",
    "gateway_model",
    "subscription_definition",
)
_SNAPSHOT_NAME = re.compile(
    rf"[a-z0-9-]+-[0-9a-f]{{{SNAPSHOT_DIGEST_LENGTH}}}"
    rf"({re.escape(PROFILE_SUFFIX)}|{re.escape(SETTINGS_SUFFIX)})"
)


class RenderError(RuntimeError):
    """渲染快照时发现的运行问题，例如 profile 引用的模型不在网关里。"""


class PricingSnapshot(TypedDict):
    """每 token 的美元单价，缓存单价缺失为 None。"""

    prompt: float
    completion: float
    input_cache_read: float | None
    input_cache_write: float | None


class ModelSnapshot(TypedDict):
    """快照里一个模型的全部信息。

    `claude_id` 是交给 Claude Code 的 id（context 大于 200000 时带 `[1m]`），
    `gateway_id` 是网关里的 id；`efforts` 为 None 表示不支持档位；
    `dynamic_allowed` 为真时 `auto` 档原样生效，否则按 medium 处理；`estimated` 为真
    时费用是等价花费（状态栏标 ≈）；`pricing` 为 None 时不显示费用。
    """

    ref: str
    source: str
    source_type: str
    model_id: str
    gateway_id: str
    claude_id: str
    openrouter: str | None
    context: int
    efforts: list[str] | None
    dynamic_allowed: bool
    display: str
    pricing: PricingSnapshot | None
    estimated: bool


class ProfileSnapshot(TypedDict):
    """profile 快照（`*.profile.json`）的形态，状态栏按它读。

    `tiers` 为 fable、opus、sonnet、haiku 到模型引用的映射；`models` 以模型引用为键，
    含四档与会话里 `/model` 能切到的全部模型；`compact_window` 为写进
    `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 的值，越界未写时为 None。
    """

    schema: int
    profile: str
    tiers: dict[str, str]
    models: dict[str, ModelSnapshot]
    compact_window: int | None


@dataclass(frozen=True)
class Snapshot:
    """一次渲染的两份快照文件。"""

    profile_file: Path
    settings_file: Path


@dataclass(frozen=True)
class Diagnostic:
    """preflight 的一条诊断；`category` 取自 `PREFLIGHT_CATEGORIES`。"""

    category: str
    message: str
    model: str | None = None
    profile: str | None = None

    def __post_init__(self) -> None:
        if self.category not in PREFLIGHT_CATEGORIES:
            raise ValueError(
                f"诊断类别必须取自 {list(PREFLIGHT_CATEGORIES)}，收到 {self.category!r}"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "category": self.category,
            "message": self.message,
            "model": self.model,
            "profile": self.profile,
        }


@dataclass
class Report:
    """preflight 的结果：错误、警告与不可用模型三组诊断。"""

    profile: str
    online_checks: bool
    errors: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    unavailable: list[Diagnostic] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        """preflight JSON schema 1。"""
        return {
            "schema": PREFLIGHT_SCHEMA,
            "profile": self.profile,
            "online_checks": self.online_checks,
            "errors": [item.to_json() for item in self.errors],
            "warnings": [item.to_json() for item in self.warnings],
            "unavailable": [item.to_json() for item in self.unavailable],
        }

    def exit_code(self) -> int:
        """有错误为 1，否则 0；用法错误的 2 由命令层给。"""
        return 1 if self.errors else 0


# -----------------------------------------------------------------------------
# 小工具


def _all_refs(config: Config) -> list[ModelRef]:
    """配置里列出的全部模型，按来源与模型的配置顺序。"""
    return [
        resolve_ref(config, f"{source.name}/{model.id}")
        for source in config.sources.values()
        for model in source.models
    ]


def _subscription_channels(config: Config) -> list[str]:
    return sorted(
        {
            source.type
            for source in config.sources.values()
            if source.type in SUBSCRIPTION_TYPES
        }
    )


def _definition_entry(
    ref: ModelRef, definitions: Mapping[str, Mapping[str, object]]
) -> Mapping[str, object] | None:
    """在订阅通道的定义里找该模型，匹配规则见 `catalog.match_definition`。

    通道缺失或定义缺 `models` 列表时返回 None；列表里不是对象的条目跳过。
    """
    payload = definitions.get(ref.source_type)
    models = payload.get("models") if payload is not None else None
    if not isinstance(models, list):
        return None
    entries = [entry for entry in models if isinstance(entry, dict)]
    return catalog.match_definition(ref.model_id, entries)


def _dynamic_allowed(
    ref: ModelRef, definitions: Mapping[str, Mapping[str, object]]
) -> bool:
    """订阅模型的 `thinking.dynamic_allowed`；通用来源与缺字段时为 False。"""
    if ref.source_type not in SUBSCRIPTION_TYPES:
        return False
    entry = _definition_entry(ref, definitions)
    thinking = entry.get("thinking") if entry is not None else None
    allowed = thinking.get("dynamic_allowed") if isinstance(thinking, dict) else None
    return allowed is True


def _filter_efforts(efforts: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if efforts is None:
        return None
    kept = tuple(level for level in efforts if level in EFFORT_LEVELS)
    return kept or None


def claude_model_id(ref: ModelRef, meta: ModelMeta) -> str:
    """交给 Claude Code 的模型 id：网关 id，context 大于 200000 时加 `[1m]`。"""
    if meta.context is not None and meta.context > CONTEXT_1M_THRESHOLD:
        return f"{ref.gateway_id}{ONE_M_SUFFIX}"
    return ref.gateway_id


def effective_effort(
    requested: str, levels: Iterable[str] | None, *, dynamic_allowed: bool
) -> str | None:
    """把客户端请求的档位按后端支持的档位换算成实际生效的档位。

    参数
    ----------
    requested : str
        客户端的档位，可以是 `auto`。
    levels : Iterable[str] | None
        后端支持的档位；None 或空表示不支持档位。
    dynamic_allowed : bool
        后端是否接受动态档位；为真时 `auto` 原样生效，否则按 medium 换算。

    返回
    ----------
    str | None
        生效档位；后端不支持档位时为 None。请求档位不在支持集合里时，取不高于它的
        最高支持档，没有则取最低支持档；不认识的档位取最低支持档。
    """
    supported = [level for level in EFFORT_LEVELS if level in set(levels or ())]
    if not supported:
        return None
    if requested == AUTO_EFFORT:
        if dynamic_allowed:
            return AUTO_EFFORT
        requested = AUTO_FALLBACK_EFFORT
    if requested in supported:
        return requested
    if requested not in EFFORT_LEVELS:
        return supported[0]
    rank = EFFORT_LEVELS.index(requested)
    below = [level for level in supported if EFFORT_LEVELS.index(level) < rank]
    return below[-1] if below else supported[0]


def compact_window(contexts: Iterable[int], factor: float) -> int | None:
    """`floor(min(contexts) × factor)`，落在 [100000, 1000000) 之外时为 None。"""
    window = int(min(contexts) * factor)
    if not COMPACT_WINDOW_MIN <= window < COMPACT_WINDOW_MAX:
        return None
    return window


def key_helper_command() -> str:
    """派生 settings 的 `apiKeyHelper`：同一环境 scripts 目录下的 key 读取命令。"""
    return shlex.quote(str(Path(sysconfig.get_path("scripts")) / "claudex-client-key"))


def statusline_command() -> str:
    """派生 settings 的 `statusLine.command`：同一解释器以模块方式运行状态栏适配器。"""
    return f"{shlex.quote(sys.executable)} -P -m claudex.statusline"


def user_settings_file() -> Path:
    """Claude Code 的用户级 settings，从中读持久化的档位。"""
    return Path.home() / ".claude" / "settings.json"


def load_user_effort_table(path: Path) -> dict[str, str]:
    """读用户 settings 里持久化的档位，返回型号 id 到档位的表。

    参数
    ----------
    path : Path
        用户级 settings.json；不存在、读不了或不是 JSON 对象时返回空表。

    返回
    ----------
    dict[str, str]
        `modelSettings.<id>.effortLevel` 以型号 id 为键；顶层 `effortLevel` 以空字符串
        为键。只收 `CLIENT_EFFORT_LEVELS` 里的值。
    """
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    table: dict[str, str] = {}
    top = raw.get("effortLevel")
    if isinstance(top, str) and top in CLIENT_EFFORT_LEVELS:
        table[""] = top
    per_model = raw.get("modelSettings")
    if isinstance(per_model, dict):
        for model_id, entry in per_model.items():
            level = entry.get("effortLevel") if isinstance(entry, dict) else None
            if isinstance(level, str) and level in CLIENT_EFFORT_LEVELS:
                table[str(model_id)] = level
    return table


def _strip_fast_header(value: object) -> object | None:
    """去掉自定义请求头里的 Fast 档头，其余行原样保留；什么都不剩时返回 None。"""
    if not isinstance(value, str):
        return value
    kept = [
        line
        for line in value.splitlines()
        if line.partition(":")[0].strip().lower() != FAST_HEADER
    ]
    return "\n".join(kept) if any(line.strip() for line in kept) else None


def strip_generated_keys(
    base: Mapping[str, object],
) -> tuple[dict[str, object], list[str]]:
    """去掉基底里本应由生成器产出的键，返回清理后的副本与被去掉的键名。

    `permissions`、`hooks` 等其余键原样保留；`statusLine.command` 与基底 env 里的
    `ANTHROPIC_DEFAULT_*`、`CLAUDEX_*`、`ANTHROPIC_BASE_URL`、
    `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 去掉，`ANTHROPIC_CUSTOM_HEADERS` 里只去掉
    Fast 档头。`statusLine.command` 不计入被去掉的键名：它的原值作为底层渲染器
    记进 `CLAUDEX_STATUSLINE_COMMAND`，没有丢失。

    异常
    ----------
    ConfigError
        `env` 或 `statusLine` 不是对象。
    """
    cleaned = {
        key: value for key, value in base.items() if key not in GENERATED_TOP_KEYS
    }
    removed = [key for key in base if key in GENERATED_TOP_KEYS]
    env = base.get("env")
    if env is not None:
        if not isinstance(env, dict):
            raise ConfigError(f"settings.base.json 的 env 必须是对象，收到 {env!r}")
        kept: dict[str, object] = {}
        for key, value in env.items():
            if key.startswith(GENERATED_ENV_PREFIXES) or key in GENERATED_ENV_KEYS:
                removed.append(f"env.{key}")
                continue
            if key == CUSTOM_HEADERS_ENV:
                stripped = _strip_fast_header(value)
                if stripped != value:
                    removed.append(f"env.{key} 里的 X-Claudex-Tier 头")
                if stripped is None:
                    continue
                value = stripped
            kept[str(key)] = value
        cleaned["env"] = kept
    status_line = base.get("statusLine")
    if status_line is not None:
        if not isinstance(status_line, dict):
            raise ConfigError(
                f"settings.base.json 的 statusLine 必须是对象，收到 {status_line!r}"
            )
        cleaned["statusLine"] = {
            key: value for key, value in status_line.items() if key != "command"
        }
    return cleaned, removed


def load_settings_base() -> dict[str, object]:
    """读取 `settings.base.json`。

    异常
    ----------
    ConfigError
        文件不存在、不是 JSON 对象。
    """
    path = paths.settings_base_file()
    try:
        data = jsonio.read_json_object(path)
    except ValueError as err:
        raise ConfigError(str(err)) from err
    if data is None:
        raise ConfigError(f"{path} 不存在；运行 claudex init 生成起步文件")
    return data


def _base_statusline_command(base: Mapping[str, object]) -> str | None:
    status_line = base.get("statusLine")
    command = status_line.get("command") if isinstance(status_line, dict) else None
    if command is None:
        return None
    if not isinstance(command, str):
        raise ConfigError(
            f"settings.base.json 的 statusLine.command 必须是字符串，收到 {command!r}"
        )
    return command or None


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _settings_sibling(profile_file: Path) -> Path:
    name = profile_file.name.removesuffix(PROFILE_SUFFIX) + SETTINGS_SUFFIX
    return profile_file.with_name(name)


# -----------------------------------------------------------------------------
# 元数据


def model_inputs(
    config: Config,
    catalog_data: Catalog,
    definitions: Mapping[str, Mapping[str, object]],
) -> dict[str, ModelMeta]:
    """配置里全部已列出模型的元数据，以模型引用为键。

    参数
    ----------
    definitions : Mapping[str, Mapping[str, object]]
        订阅通道名到 `gateway.fetch_model_definitions` 返回值的映射。缺某个通道时，
        该通道下需要定义才能取值的订阅模型不出现在结果里（网关起来之前生成网关配置
        时就是这样，那时只用得到通用来源的值）。

    返回
    ----------
    dict[str, ModelMeta]
        档位只留 `config.EFFORT_LEVELS` 里的值，过滤后为空即 None。
    """
    metas: dict[str, ModelMeta] = {}
    for ref in _all_refs(config):
        needs_definition = ref.source_type in SUBSCRIPTION_TYPES and (
            ref.context is None or ref.efforts is None
        )
        if needs_definition and ref.source_type not in definitions:
            continue
        meta = catalog.model_metadata(ref, catalog_data, definitions)
        metas[ref.ref] = ModelMeta(
            context=meta.context,
            efforts=_filter_efforts(meta.efforts),
            display=meta.display,
        )
    return metas


def gateway_inputs(
    metas: Mapping[str, ModelMeta],
) -> tuple[dict[str, int], dict[str, tuple[str, ...]]]:
    """把元数据整理成 `gateway.prepare_gateway` 的 `contexts` 与 `efforts`。"""
    contexts = {ref: meta.context for ref, meta in metas.items() if meta.context}
    efforts = {ref: meta.efforts for ref, meta in metas.items() if meta.efforts}
    return contexts, efforts


# -----------------------------------------------------------------------------
# 快照


def _model_snapshot(
    ref: ModelRef,
    meta: ModelMeta,
    catalog_data: Catalog,
    definitions: Mapping[str, Mapping[str, object]],
) -> ModelSnapshot:
    assert meta.context is not None
    pricing = catalog.pricing_for(ref, catalog_data)
    return {
        "ref": ref.ref,
        "source": ref.source,
        "source_type": ref.source_type,
        "model_id": ref.model_id,
        "gateway_id": ref.gateway_id,
        "claude_id": claude_model_id(ref, meta),
        "openrouter": ref.openrouter,
        "context": meta.context,
        "efforts": list(meta.efforts) if meta.efforts is not None else None,
        "dynamic_allowed": _dynamic_allowed(ref, definitions),
        "display": meta.display,
        "pricing": None
        if pricing is None
        else {
            "prompt": pricing.prompt,
            "completion": pricing.completion,
            "input_cache_read": pricing.input_cache_read,
            "input_cache_write": pricing.input_cache_write,
        },
        "estimated": catalog.is_estimated(ref),
    }


def _model_settings(
    profile_data: ProfileSnapshot, user_effort: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    """按四档后端的档位夹紧用户档位，生成 `modelSettings`。

    Claude Code 把 `/effort` 写到 `modelSettings.<规范 id>.effortLevel`，四档的规范 id
    见 `MODEL_OVERRIDE_KEYS`。用户为该 id 设的档位，缺省取顶层 `effortLevel`，再缺省取
    `DEFAULT_CLIENT_EFFORT`，经 `effective_effort` 换算；结果不在 Claude Code 接受的
    四档里（不支持档位、`auto`、`minimal`、`max`）时不写该 id。
    """
    derived: dict[str, dict[str, str]] = {}
    default_level = user_effort.get("", DEFAULT_CLIENT_EFFORT)
    for tier in TIERS:
        model = profile_data["models"][profile_data["tiers"][tier]]
        requested = next(
            (
                user_effort[key]
                for key in MODEL_OVERRIDE_KEYS[tier]
                if key in user_effort
            ),
            default_level,
        )
        effective = effective_effort(
            requested, model["efforts"], dynamic_allowed=model["dynamic_allowed"]
        )
        if effective not in CLIENT_EFFORT_LEVELS:
            continue
        assert effective is not None
        for key in MODEL_OVERRIDE_KEYS[tier]:
            derived[key] = {"effortLevel": effective}
    return derived


def build_settings(
    base: Mapping[str, object],
    profile_data: ProfileSnapshot,
    user_effort: Mapping[str, str],
    *,
    fast: bool = False,
) -> dict[str, object]:
    """把基底与 profile 快照合并成派生 settings；不含 `CLAUDEX_PROFILE_FILE`。

    `availableModels` 为四个档位名加 `profile_data["models"]` 里全部模型的
    `claude_id`；`CLAUDEX_STATUSLINE_COMMAND` 取基底 `statusLine.command` 的原值，
    基底没有时不写。`fast` 为真时 `ANTHROPIC_CUSTOM_HEADERS` 为基底的值（已去掉
    `X-Claudex-Tier` 行）再加一行 `X-Claudex-Tier: fast`，并写 `CLAUDEX_FAST=1`；
    派生 settings 的 env 会盖过进程环境，所以 Fast 头只能在这里给。
    """
    original_command = _base_statusline_command(base)
    settings, _removed = strip_generated_keys(base)
    env_raw = settings.get("env")
    env: dict[str, object] = dict(env_raw) if isinstance(env_raw, dict) else {}
    models = profile_data["models"]
    for tier in TIERS:
        model = models[profile_data["tiers"][tier]]
        upper = tier.upper()
        env[f"ANTHROPIC_DEFAULT_{upper}_MODEL"] = model["claude_id"]
        env[f"ANTHROPIC_DEFAULT_{upper}_MODEL_NAME"] = model["display"]
        env[f"ANTHROPIC_DEFAULT_{upper}_MODEL_DESCRIPTION"] = (
            f"{model['gateway_id']} via {model['source']} ({model['source_type']})"
        )
    env["ANTHROPIC_BASE_URL"] = gateway.GATEWAY_URL
    if profile_data["compact_window"] is not None:
        env[COMPACT_WINDOW_ENV] = str(profile_data["compact_window"])
    env[PROFILE_ENV] = profile_data["profile"]
    if original_command is not None:
        env[STATUSLINE_COMMAND_ENV] = original_command
    if fast:
        headers = env.get(CUSTOM_HEADERS_ENV)
        prefix = f"{headers}\n" if isinstance(headers, str) and headers else ""
        env[CUSTOM_HEADERS_ENV] = f"{prefix}{FAST_HEADER_LINE}"
        env[FAST_ENV] = "1"
    settings["env"] = env
    settings["model"] = "fable"
    available: list[str] = list(TIERS)
    for model in models.values():
        if model["claude_id"] not in available:
            available.append(model["claude_id"])
    settings["availableModels"] = available
    settings["modelOverrides"] = {
        key: models[profile_data["tiers"][tier]]["claude_id"]
        for tier in TIERS
        for key in MODEL_OVERRIDE_KEYS[tier]
    }
    model_settings = _model_settings(profile_data, user_effort)
    if model_settings:
        settings["modelSettings"] = model_settings
    settings["apiKeyHelper"] = key_helper_command()
    status_raw = settings.get("statusLine")
    status_line: dict[str, object] = (
        dict(status_raw) if isinstance(status_raw, dict) else {}
    )
    for key, value in STATUSLINE_DEFAULTS.items():
        status_line.setdefault(key, value)
    status_line["command"] = statusline_command()
    settings["statusLine"] = status_line
    return settings


def _tier_refs(
    config: Config, profile: str, overrides: Mapping[str, str]
) -> dict[str, ModelRef]:
    unknown = sorted(set(overrides) - set(TIERS))
    if unknown:
        raise ConfigError(f"临时替换的档位 {unknown} 不存在，只接受 {list(TIERS)}")
    refs = profile_refs(config, profile)
    for tier, ref_text in overrides.items():
        refs[tier] = resolve_ref(config, ref_text)
    return refs


def _fetch_definitions(
    config: Config, management_key: str
) -> dict[str, dict[str, object]]:
    return {
        channel: gateway.fetch_model_definitions(management_key, channel)
        for channel in _subscription_channels(config)
    }


def build_profile_snapshot(
    config: Config,
    profile: str,
    tier_refs: Mapping[str, ModelRef],
    catalog_data: Catalog,
    definitions: Mapping[str, Mapping[str, object]],
    registered: set[str],
) -> ProfileSnapshot:
    """由配置、目录、网关定义与网关模型列表生成 profile 快照的内容。

    `models` 收四档与配置里其余已在网关注册的模型；四档之外未注册的模型不收
    （`/model` 切过去也用不了）。

    异常
    ----------
    ConfigError
        任一收入的模型取不到 context。
    RenderError
        四档引用的模型不在 `registered` 里。
    """
    metas = model_inputs(config, catalog_data, definitions)
    missing_context = sorted(ref for ref, meta in metas.items() if meta.context is None)
    if missing_context:
        raise ConfigError(
            f"模型 {missing_context} 取不到 context：在 claudex.toml 里给它们写 "
            "context，或检查 openrouter slug 与网关模型定义"
        )
    unregistered = sorted(
        {ref.ref for ref in tier_refs.values() if ref.gateway_id not in registered}
    )
    if unregistered:
        raise RenderError(
            f"profile {profile} 引用的模型 {unregistered} 不在网关的模型列表里；"
            "运行 claudex gateway restart 让网关重新加载配置"
        )
    tier_ref_texts = {ref.ref for ref in tier_refs.values()}
    models: dict[str, ModelSnapshot] = {}
    for ref in [*tier_refs.values(), *_all_refs(config)]:
        if ref.ref in models:
            continue
        if ref.ref not in tier_ref_texts and ref.gateway_id not in registered:
            continue
        models[ref.ref] = _model_snapshot(
            ref, metas[ref.ref], catalog_data, definitions
        )
    tier_contexts = [models[ref.ref]["context"] for ref in tier_refs.values()]
    return {
        "schema": PROFILE_SNAPSHOT_SCHEMA,
        "profile": profile,
        "tiers": {tier: tier_refs[tier].ref for tier in TIERS},
        "models": models,
        "compact_window": compact_window(tier_contexts, config.compact_window_factor),
    }


def write_snapshot(
    profile_data: ProfileSnapshot, settings: Mapping[str, object]
) -> Snapshot:
    """把两份内容写成按摘要命名的快照，返回两份文件的路径。

    摘要取 profile 内容与（不含 `CLAUDEX_PROFILE_FILE` 的）settings 内容的规范 JSON
    的 sha256；写入的 settings 再补上 `CLAUDEX_PROFILE_FILE`，它由摘要决定。同名快照
    已存在时内容必然相同，照样原子重写一遍：这样修改时间随启动刷新，正在用的快照不会被
    按修改时间清理掉。
    """
    digest = hashlib.sha256(
        _canonical(profile_data) + b"\0" + _canonical(settings)
    ).hexdigest()[:SNAPSHOT_DIGEST_LENGTH]
    stem = f"{profile_data['profile']}-{digest}"
    directory = paths.sessions_dir()
    snapshot = Snapshot(
        profile_file=directory / f"{stem}{PROFILE_SUFFIX}",
        settings_file=directory / f"{stem}{SETTINGS_SUFFIX}",
    )
    final_settings = dict(settings)
    env_raw = final_settings.get("env")
    env: dict[str, object] = dict(env_raw) if isinstance(env_raw, dict) else {}
    env[PROFILE_FILE_ENV] = str(snapshot.profile_file)
    final_settings["env"] = env
    jsonio.write_json_atomic(snapshot.profile_file, profile_data)
    jsonio.write_json_atomic(snapshot.settings_file, final_settings)
    touch_snapshot(snapshot.profile_file)
    return snapshot


def render(
    config: Config,
    profile: str,
    overrides: Mapping[str, str] | None = None,
    *,
    fast: bool = False,
) -> Snapshot:
    """生成本次启动的快照。

    参数
    ----------
    profile : str
        要启动的 profile 名。
    overrides : Mapping[str, str] | None
        档位名（fable、opus、sonnet、haiku）到模型引用的临时替换，只作用于本次快照。
    fast : bool
        本次以 `--fast` 启动：派生 env 带 Fast 请求头与 `CLAUDEX_FAST=1`（见
        `build_settings`）。

    说明
    ----------
    在线取配置里全部订阅通道的网关模型定义与网关的 `/v1/models`，读 OpenRouter 目录
    缓存（没有缓存时同步抓取一次），生成快照后清理修改时间早于 7 天前的旧快照。

    异常
    ----------
    ConfigError
        profile 或临时替换无效、基底 settings 不可用、模型取不到 context。
    GatewayError
        key 文件不可用或网关查询失败。
    RenderError
        四档引用的模型不在网关里。
    CatalogError
        没有目录缓存且抓取失败。
    """
    tier_refs = _tier_refs(config, profile, overrides or {})
    base = load_settings_base()
    catalog_data = catalog.ensure_catalog()
    secrets = gateway.load_secrets(config)
    definitions = _fetch_definitions(config, secrets.management_key)
    registered = gateway.fetch_models(secrets.client_key)
    profile_data = build_profile_snapshot(
        config, profile, tier_refs, catalog_data, definitions, registered
    )
    settings = build_settings(
        base, profile_data, load_user_effort_table(user_settings_file()), fast=fast
    )
    snapshot = write_snapshot(profile_data, settings)
    prune_snapshots()
    return snapshot


def touch_snapshot(profile_file: Path) -> None:
    """把 profile 快照与同名 settings 快照的修改时间刷新为现在。

    异常
    ----------
    OSError
        任一文件不存在或无法修改时间。
    """
    os.utime(profile_file)
    os.utime(_settings_sibling(profile_file))


def prune_snapshots(
    max_age: timedelta = SNAPSHOT_MAX_AGE, *, now: datetime | None = None
) -> list[Path]:
    """删除快照目录里修改时间早于 `now - max_age` 的快照文件，返回删掉的路径。

    只处理名字形如 `<profile>-<12 位十六进制>.profile.json` 或 `.settings.json` 的文件，
    逐个按各自的修改时间判断；目录不存在时什么都不做。
    """
    directory = paths.sessions_dir()
    if not directory.is_dir():
        return []
    cutoff = (datetime.now(UTC) if now is None else now) - max_age
    removed: list[Path] = []
    for path in sorted(directory.iterdir()):
        if _SNAPSHOT_NAME.fullmatch(path.name) is None or not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if modified < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def fast_notice(profile_data: ProfileSnapshot) -> str | None:
    """`--fast` 的提示：主对话档（fable）是 Codex 来源时为 None，否则说明主对话不会走
    Fast，以及会话里仍会按 Fast 计费的去向。"""
    models = profile_data["models"]
    fable = models[profile_data["tiers"]["fable"]]
    if fable["source_type"] == FAST_SOURCE_TYPE:
        return None
    codex_tiers = [
        f"{tier}={models[profile_data['tiers'][tier]]['gateway_id']}"
        for tier in TIERS[1:]
        if models[profile_data["tiers"][tier]]["source_type"] == FAST_SOURCE_TYPE
    ]
    codex_models = sorted(
        model["gateway_id"]
        for model in models.values()
        if model["source_type"] == FAST_SOURCE_TYPE
    )
    tier_note = (
        f"按 Codex 档派出的 subagent（{'、'.join(codex_tiers)}）"
        if codex_tiers
        else "subagent 不落到 Codex"
    )
    switch_note = (
        "、".join(codex_models) if codex_models else "（本配置没有 Codex 模型）"
    )
    return (
        "claudex: --fast 只对 Codex 来源生效；本 profile 的主对话档（fable）是 "
        f"{fable['gateway_id']}（{fable['source']}，{fable['source_type']}），"
        "主对话不会走 Fast；会话里落到 Codex 的请求仍按 Fast 计费："
        f"/model 切到 {switch_note}，或{tier_note}"
    )


# -----------------------------------------------------------------------------
# preflight


def _catalog_for_preflight(report: Report, check_gateway: bool) -> Catalog | None:
    try:
        catalog_data = (
            catalog.ensure_catalog() if check_gateway else catalog.load_catalog()
        )
    except (CatalogError, ValueError) as err:
        report.errors.append(Diagnostic("catalog", str(err)))
        return None
    if catalog_data is None:
        report.errors.append(
            Diagnostic("catalog", "OpenRouter 目录缓存不存在；运行 claudex update 抓取")
        )
    return catalog_data


def _catalog_diagnostics(
    report: Report, config: Config, catalog_data: Catalog, tier_texts: set[str]
) -> None:
    for ref in _all_refs(config):
        slug = ref.openrouter
        if slug is None or slug in catalog_data.models:
            continue
        profile = report.profile if ref.ref in tier_texts else None
        if slug in catalog_data.skipped:
            report.warnings.append(
                Diagnostic(
                    "catalog_unparsed",
                    f"openrouter slug {slug} 的目录条目格式无法解析",
                    ref.ref,
                    profile,
                )
            )
        else:
            report.warnings.append(
                Diagnostic(
                    "catalog_entry",
                    f"openrouter slug {slug} 不在 OpenRouter 目录里",
                    ref.ref,
                    profile,
                )
            )


def _gateway_diagnostics(
    report: Report,
    config: Config,
    definitions: Mapping[str, Mapping[str, object]],
    registered: set[str],
    tier_texts: set[str],
) -> None:
    for ref in _all_refs(config):
        in_profile = ref.ref in tier_texts
        profile = report.profile if in_profile else None
        target = report.errors if in_profile else report.unavailable
        if ref.gateway_id not in registered:
            target.append(
                Diagnostic(
                    "gateway_model",
                    f"{ref.gateway_id} 不在网关的模型列表里",
                    ref.ref,
                    profile,
                )
            )
        if (
            ref.source_type in SUBSCRIPTION_TYPES
            and _definition_entry(ref, definitions) is None
        ):
            target.append(
                Diagnostic(
                    "subscription_definition",
                    f"{ref.model_id} 不在网关 {ref.source_type} 通道的模型定义里",
                    ref.ref,
                    profile,
                )
            )


def preflight(config: Config, *, profile: str, check_gateway: bool) -> Report:
    """检查一个 profile 能否启动，返回全部诊断。

    检查项：profile 存在；基底 settings 可用（被生成器覆盖的键报警告）；目录缓存可用；
    有 openrouter slug 的模型在目录里（缺条目、条目格式无法解析报警告）；全部模型取得到
    context（取不到报错）。`check_gateway` 为真时另查：key 文件与网关可达；模型在网关
    模型列表里、订阅模型在网关定义里（四档报错，其余模型记为不可用）。为假时这些检查与
    需要网关定义才能取值的订阅模型都跳过，`online_checks` 为 false；目录也只读缓存、
    不抓取。
    """
    report = Report(profile=profile, online_checks=check_gateway)
    try:
        tier_refs = profile_refs(config, profile)
    except ConfigError as err:
        report.errors.append(Diagnostic("config", str(err), profile=profile))
        return report
    tier_texts = {ref.ref for ref in tier_refs.values()}
    try:
        _cleaned, removed = strip_generated_keys(load_settings_base())
    except ConfigError as err:
        report.errors.append(Diagnostic("config", str(err)))
    else:
        report.warnings.extend(
            Diagnostic("config", f"settings.base.json 的 {key} 由生成器覆盖，不透传")
            for key in removed
        )
    catalog_data = _catalog_for_preflight(report, check_gateway)
    definitions: dict[str, dict[str, object]] = {}
    if check_gateway:
        try:
            secrets = gateway.load_secrets(config)
            definitions = _fetch_definitions(config, secrets.management_key)
            registered = gateway.fetch_models(secrets.client_key)
        except GatewayError as err:
            report.errors.append(Diagnostic("gateway_access", str(err)))
            report.online_checks = False
        else:
            _gateway_diagnostics(report, config, definitions, registered, tier_texts)
    if catalog_data is None:
        return report
    _catalog_diagnostics(report, config, catalog_data, tier_texts)
    try:
        metas = model_inputs(config, catalog_data, definitions)
    except ValueError as err:
        report.errors.append(Diagnostic("subscription_definition", str(err)))
        return report
    for ref_text, meta in metas.items():
        if meta.context is None:
            report.errors.append(
                Diagnostic(
                    "context_missing",
                    f"{ref_text} 取不到 context；在 claudex.toml 里写 context",
                    ref_text,
                    profile if ref_text in tier_texts else None,
                )
            )
    return report
