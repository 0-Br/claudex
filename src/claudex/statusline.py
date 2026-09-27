"""Claude Code 状态栏适配器：`python -P -m claudex.statusline`。

Claude Code 每次刷新状态栏时把状态栏 JSON 写进 stdin。适配器读会话启动时的 profile
快照（`CLAUDEX_PROFILE_FILE`），把 JSON 改写为 claudex 口径：显示名、effort、按目录单价
累计的费用、当前订阅来源的额度窗口、上下文分母，删除 `prompt_cache`，加上 `claudex`
对象，再交给 `CLAUDEX_STATUSLINE_COMMAND` 指定的底层渲染器；没配置或渲染器失败时输出
一行内置简版。另按 60 秒节流在后台拉起额度刷新程序。
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from claudex import catalog, jsonio, quota, render
from claudex.catalog import Pricing
from claudex.config import SUBSCRIPTION_TYPES, TIERS
from claudex.render import ModelSnapshot, ProfileSnapshot

REFRESH_MODULE = "claudex.quota"
RENDERER_TIMEOUT_SECONDS = 3
# 底层渲染器命令里出现它就当作递归调用，不执行
RECURSION_MARK = "claudex.statusline"
NO_REFRESH_ENV = "CLAUDEX_NO_REFRESH"
FAST_ENV = "CLAUDEX_FAST"
FAST_SOURCE_TYPE = "codex"
STATE_PREFIX = "claudex-sl-"
STATE_VERSION = 1
QUOTA_FRESH_SECONDS = 120
# 余额这类按量数据超过这个年龄就不再显示；订阅窗口保留到它自己的重置时刻
QUOTA_STALE_SECONDS = 600
FIVE_HOUR_SECONDS = 18_000
SEVEN_DAY_SECONDS = 604_800
WINDOW_NAMES = (("five_hour", "5h"), ("seven_day", "7d"))
ESTIMATE_MARK = "≈"
# 费用按标准档单价计，没有乘 Fast 档的倍率
FAST_COST_MARKER = "fast ×2.5 not in ≈"
COST_PARTIAL_MARKER = "cost partial"
FAST_SUFFIX = " ↯fast"
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_ONE_M = re.compile(r"(\[1m\])+$", re.IGNORECASE)
_MODEL_KEYS = {
    "ref": str,
    "source": str,
    "source_type": str,
    "model_id": str,
    "gateway_id": str,
    "claude_id": str,
    "display": str,
    "context": int,
    "dynamic_allowed": bool,
    "estimated": bool,
}


class ClaudexObject(TypedDict):
    """加进状态栏 JSON 的 `claudex` 对象，字段冻结，是底层渲染器的输入契约。"""

    profile: str | None
    source: str | None
    source_type: str | None
    cost_estimated: bool
    fast: bool
    quota_label: str | None
    markers: list[str]


@dataclass(frozen=True)
class QuotaView:
    """一个来源的额度记录解读结果：可显示的数据、新鲜度与原因、冷却快照。"""

    data: dict[str, object] | None
    status: str
    reason: str
    cooldowns: list[object] | None = None


# -----------------------------------------------------------------------------
# 小工具


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _warn(message: str) -> None:
    print(f"claudex statusline: {message}", file=sys.stderr)


def _validate_model(ref: str, value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"models[{ref!r}] 必须是对象")
    for key, kind in _MODEL_KEYS.items():
        if not isinstance(value.get(key), kind):
            raise ValueError(f"models[{ref!r}].{key} 必须是 {kind.__name__}")
    efforts = value.get("efforts")
    if efforts is not None and not isinstance(efforts, list):
        raise ValueError(f"models[{ref!r}].efforts 必须是列表或 null")
    pricing = value.get("pricing")
    if pricing is not None and not isinstance(pricing, dict):
        raise ValueError(f"models[{ref!r}].pricing 必须是对象或 null")


def load_profile_snapshot(path: Path) -> ProfileSnapshot:
    """读 profile 快照并校验结构。

    异常
    ----------
    OSError
        文件读不到。
    ValueError
        不是 JSON 对象、schema 不对，或四档与模型表的结构不对；消息含路径。
    """
    data = jsonio.read_json_object(path)
    if data is None:
        raise FileNotFoundError(f"{path} 不存在")
    if data.get("schema") != render.PROFILE_SNAPSHOT_SCHEMA:
        raise ValueError(f"{path}: schema 必须是 {render.PROFILE_SNAPSHOT_SCHEMA}")
    models = data.get("models")
    tiers = data.get("tiers")
    if not isinstance(data.get("profile"), str) or not isinstance(models, dict):
        raise ValueError(f"{path}: 缺 profile 名或 models 表")
    if not isinstance(tiers, dict) or any(
        tiers.get(tier) not in models for tier in TIERS
    ):
        raise ValueError(f"{path}: tiers 必须给四档各指一个 models 里的引用")
    try:
        for ref, model in models.items():
            _validate_model(str(ref), model)
    except ValueError as err:
        raise ValueError(f"{path}: {err}") from err
    return cast("ProfileSnapshot", data)


def find_model(profile: ProfileSnapshot, model_id: object) -> ModelSnapshot | None:
    """按状态栏的 `model.id` 找快照里的模型。

    依次认：档位名（fable 等）、四档的规范 id（`render.MODEL_OVERRIDE_KEYS`）、
    `claude_id`，以及去掉 `[1m]` 后的 `gateway_id`。
    """
    if not isinstance(model_id, str) or not model_id:
        return None
    models = profile["models"]
    tiers = profile["tiers"]
    if model_id in tiers:
        return models[tiers[model_id]]
    for tier, keys in render.MODEL_OVERRIDE_KEYS.items():
        if model_id in keys:
            return models[tiers[tier]]
    bare = _ONE_M.sub("", model_id)
    for model in models.values():
        if model["claude_id"] == model_id or model["gateway_id"] == bare:
            return model
    return None


def _pricing(model: ModelSnapshot) -> Pricing | None:
    raw = model["pricing"]
    if raw is None:
        return None
    return Pricing(
        prompt=raw["prompt"],
        completion=raw["completion"],
        input_cache_read=raw["input_cache_read"],
        input_cache_write=raw["input_cache_write"],
    )


def _current_model_id(payload: Mapping[str, object]) -> object:
    model = payload.get("model")
    return model.get("id") if isinstance(model, dict) else None


# -----------------------------------------------------------------------------
# 会话费用：每次响应完成时按当前模型的单价结算一次


def state_path(session_id: str) -> Path:
    """会话费用状态文件：`$TMPDIR/claudex-sl-<session id 摘要前 12 位>.json`。"""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    directory = Path(os.environ.get("TMPDIR") or "/tmp")
    return directory / f"{STATE_PREFIX}{digest}.json"


def empty_state() -> dict[str, object]:
    return {
        "v": STATE_VERSION,
        "api_ms": 0,
        "native_cost": 0.0,
        "usage_fingerprint": "",
        "context_tokens": 0,
        "settled_usd": 0.0,
        "settled_events": 0,
        "unpriced_events": 0,
        "estimated": False,
    }


def load_state(path: Path) -> dict[str, object]:
    """读会话状态；缺失、损坏或版本不符时从空状态开始。"""
    try:
        parsed = jsonio.read_json_object(path)
    except ValueError:
        parsed = None
    if parsed is None or parsed.get("v") != STATE_VERSION:
        return empty_state()
    state = empty_state()
    for key, default in state.items():
        value = parsed.get(key)
        if type(value) is type(default):
            state[key] = value
    return state


def _context_tokens(payload: Mapping[str, object]) -> int | None:
    context_window = payload.get("context_window")
    if not isinstance(context_window, dict):
        return None
    total_input = _int(context_window.get("total_input_tokens"))
    total_output = _int(context_window.get("total_output_tokens"))
    if total_input is None or total_output is None:
        return None
    return total_input + total_output


def _usage_event(
    payload: Mapping[str, object],
) -> tuple[str, dict[str, object], str] | None:
    """本次输入里的最近一次响应：(模型 id, 四项 token, 指纹)。"""
    model_id = _current_model_id(payload)
    context_window = payload.get("context_window")
    usage = (
        context_window.get("current_usage")
        if isinstance(context_window, dict)
        else None
    )
    if not isinstance(model_id, str) or not model_id or not isinstance(usage, dict):
        return None
    normalized: dict[str, object] = {
        name: _int(usage.get(name)) or 0 for name in TOKEN_FIELDS
    }
    bare = _ONE_M.sub("", model_id)
    fingerprint = hashlib.sha256(
        json.dumps([bare, normalized], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return bare, normalized, fingerprint


def _settle(
    state: dict[str, object],
    profile: ProfileSnapshot,
    model_id: str,
    usage: Mapping[str, object],
) -> None:
    model = find_model(profile, model_id)
    pricing = _pricing(model) if model is not None else None
    if model is None or pricing is None:
        state["unpriced_events"] = (_int(state["unpriced_events"]) or 0) + 1
        return
    state["settled_usd"] = (_float(state["settled_usd"]) or 0.0) + (
        catalog.usage_cost_usd(usage, pricing)
    )
    state["settled_events"] = (_int(state["settled_events"]) or 0) + 1
    if model["estimated"]:
        state["estimated"] = True


def update_cost_state(
    state: dict[str, object], payload: Mapping[str, object], profile: ProfileSnapshot
) -> None:
    """用一次状态栏输入更新会话费用。

    说明
    ----------
    一次响应完成的信号：原生 `cost.total_cost_usd` 增加；没有原生费用字段时，
    `total_api_duration_ms` 变了且本次 usage 的指纹与上一次不同。同一次响应在后续
    刷新里重复出现时指纹不变，不会重复结算；API 失败只增加时长、不带新 usage，也不
    结算。`/clear` 的判定：原生费用变小，或没有原生费用时 API 时长与上下文 token 同时
    变小，此时从空状态重新开始，不补算当次。
    """
    cost_raw = payload.get("cost")
    cost = cost_raw if isinstance(cost_raw, dict) else {}
    api_ms = _int(cost.get("total_api_duration_ms")) or 0
    native_cost = _float(cost.get("total_cost_usd"))
    context_tokens = _context_tokens(payload)
    previous_native = _float(state["native_cost"]) or 0.0
    previous_api = _int(state["api_ms"]) or 0
    previous_context = _int(state["context_tokens"]) or 0
    previous_fingerprint = str(state["usage_fingerprint"])
    event = _usage_event(payload)
    fingerprint = event[2] if event is not None else ""

    native_clear = native_cost is not None and native_cost + 1e-9 < previous_native
    fallback_clear = (
        native_cost is None
        and previous_api > 0
        and api_ms < previous_api
        and previous_context > 0
        and context_tokens is not None
        and context_tokens < previous_context
    )
    if native_clear or fallback_clear:
        state.clear()
        state.update(empty_state())
        state["api_ms"] = api_ms
        if native_cost is not None:
            state["native_cost"] = native_cost
        state["context_tokens"] = context_tokens or 0
        state["usage_fingerprint"] = fingerprint
        return

    native_increased = native_cost is not None and native_cost > previous_native + 1e-9
    fallback_completed = (
        api_ms > 0
        and api_ms != previous_api
        and event is not None
        and fingerprint != previous_fingerprint
    )
    if event is not None and (native_increased or fallback_completed):
        _settle(state, profile, event[0], event[1])
    state["api_ms"] = api_ms
    if native_cost is not None:
        state["native_cost"] = native_cost
    if context_tokens is not None:
        state["context_tokens"] = context_tokens
    state["usage_fingerprint"] = fingerprint


# -----------------------------------------------------------------------------
# 额度


def load_quota() -> dict[str, object] | None:
    """读额度缓存（`quota.load_quota`）；文件损坏时写一行 stderr 并视为没有数据。"""
    try:
        return quota.load_quota()
    except ValueError as err:
        _warn(f"quota cache unreadable: {err}")
        return None


def quota_view(
    quota_data: Mapping[str, object] | None, source: str, source_type: str, now: int
) -> QuotaView:
    """解读一个来源的额度记录。

    订阅来源的窗口保留到各自的重置时刻；按量数据（OpenRouter 余额）超过
    `QUOTA_STALE_SECONDS` 即不显示。`ok` 且年龄不超过 `QUOTA_FRESH_SECONDS` 为 fresh，
    其余可显示的数据为 stale。
    """
    sources = quota_data.get("sources") if quota_data is not None else None
    record = sources.get(source) if isinstance(sources, dict) else None
    if not isinstance(record, dict):
        return QuotaView(None, "unavailable", "no quota data")
    raw_cooldowns = record.get("cooldowns")
    cooldowns = raw_cooldowns if isinstance(raw_cooldowns, list) else None
    ok = record.get("ok") is True
    error = record.get("error")
    reason = (
        "" if ok else (error if isinstance(error, str) and error else "refresh failed")
    )
    updated_at = _int(record.get("updated_at"))
    data = record.get("data")
    if updated_at is None or not isinstance(data, dict) or not data:
        return QuotaView(None, "unavailable", reason or "no last-good data", cooldowns)
    age = now - updated_at
    if age < 0:
        return QuotaView(None, "unavailable", "quota timestamp invalid", cooldowns)
    if source_type not in SUBSCRIPTION_TYPES and age > QUOTA_STALE_SECONDS:
        return QuotaView(None, "unavailable", reason or "stale", cooldowns)
    if ok and age <= QUOTA_FRESH_SECONDS:
        return QuotaView(data, "fresh", "", cooldowns)
    return QuotaView(data, "stale", reason or "stale", cooldowns)


def _window(value: object, now: int) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    used = _float(value.get("used_percentage"))
    resets_at = _int(value.get("resets_at"))
    if used is None or not 0 <= used <= 100 or resets_at is None or resets_at <= now:
        return None
    return {"used_percentage": used, "resets_at": resets_at}


def _codex_windows(data: Mapping[str, object], now: int) -> dict[str, object]:
    windows = data.get("windows")
    result: dict[str, object] = {}
    if isinstance(windows, dict):
        for name, _short in WINDOW_NAMES:
            window = _window(windows.get(name), now)
            if window is not None:
                result[name] = window
    return result


def _bucket_window(
    bucket: Mapping[str, object], now: int
) -> tuple[str, dict[str, object]] | None:
    remaining = _float(bucket.get("remaining_fraction"))
    resets_at = _int(bucket.get("reset_at"))
    if remaining is None or resets_at is None or resets_at <= now:
        return None
    seconds = _int(bucket.get("window_seconds"))
    if seconds is None:
        seconds = (
            FIVE_HOUR_SECONDS
            if resets_at - now <= FIVE_HOUR_SECONDS
            else SEVEN_DAY_SECONDS
        )
    names = {FIVE_HOUR_SECONDS: "five_hour", SEVEN_DAY_SECONDS: "seven_day"}
    if seconds not in names:
        return None
    return names[seconds], {
        "used_percentage": (1.0 - remaining) * 100.0,
        "resets_at": resets_at,
    }


def _antigravity_windows(
    data: Mapping[str, object], model: ModelSnapshot, now: int
) -> dict[str, object]:
    """按模型家族取 Antigravity 的窗口：Gemini 系为 `gemini` 桶，其余为 `3p` 桶；
    家族内没有桶时退回全部桶；同名窗口取已用比例最高的。"""
    buckets = data.get("buckets")
    if not isinstance(buckets, list):
        return {}
    usable = [bucket for bucket in buckets if isinstance(bucket, dict)]
    family = "gemini" if model["model_id"].lower().startswith("gemini") else "3p"
    matched = [b for b in usable if str(b.get("id", "")).lower().startswith(family)]
    result: dict[str, object] = {}
    for bucket in matched or usable:
        window = _bucket_window(bucket, now)
        if window is None:
            continue
        name, value = window
        previous = result.get(name)
        previous_used = (
            _float(previous.get("used_percentage"))
            if isinstance(previous, dict)
            else None
        )
        current_used = _float(value.get("used_percentage")) or 0.0
        if previous_used is None or current_used > previous_used:
            result[name] = value
    return result


def rate_limits_for(
    model: ModelSnapshot, view: QuotaView, now: int
) -> dict[str, object] | None:
    """当前订阅来源的额度窗口，形状同 Claude Code 原生的 `rate_limits`。"""
    if view.data is None:
        return None
    if model["source_type"] == "codex":
        limits = _codex_windows(view.data, now)
    elif model["source_type"] == "antigravity":
        limits = _antigravity_windows(view.data, model, now)
    else:
        limits = {}
    return limits or None


def fmt_countdown(epoch: int, now: int) -> str:
    """紧凑倒计时：`2d16h`、`3h05m`、`12:34`。"""
    remaining = epoch - now
    if remaining <= 0:
        return "reset"
    if remaining >= 86_400:
        return f"{remaining // 86_400}d{remaining % 86_400 // 3600}h"
    if remaining >= 3600:
        return f"{remaining // 3600}h{remaining % 3600 // 60:02d}m"
    return f"{remaining // 60}:{remaining % 60:02d}"


def _quota_marker(label: str, model: ModelSnapshot, view: QuotaView, now: int) -> str:
    data = view.data
    if data is None:
        return f"{label} n/a ({view.reason})"
    suffix = "?" if view.status != "fresh" else ""
    if model["source_type"] == "openrouter":
        credits = _float(data.get("total_credits"))
        usage = _float(data.get("total_usage"))
        if credits is None or usage is None:
            return f"{label} n/a (invalid balance)"
        return f"{label} ${credits - usage:.2f} left{suffix}"
    windows = rate_limits_for(model, view, now) or {}
    parts = []
    for name, short in WINDOW_NAMES:
        window = windows.get(name)
        if isinstance(window, dict):
            used = _float(window.get("used_percentage")) or 0.0
            resets = _int(window.get("resets_at")) or now
            parts.append(f"{short} {used:.0f}% {fmt_countdown(resets, now)}")
    if not parts:
        return f"{label} n/a ({view.reason or 'windows reset'})"
    return f"{label} {' '.join(parts)}{suffix}"


def _cooldown_marker(model: ModelSnapshot, view: QuotaView, now: int) -> str | None:
    """当前模型可见的未过期冷却里最晚恢复的一条；凭据级冷却对所有模型可见。"""
    active: list[tuple[int, str, str]] = []
    for item in view.cooldowns or []:
        if not isinstance(item, dict):
            continue
        retry_at = _int(item.get("retry_at"))
        scope = item.get("scope")
        if retry_at is None or retry_at <= now:
            continue
        if scope == "model" and item.get("model_key") != model["gateway_id"]:
            continue
        if scope not in ("credential", "model"):
            continue
        active.append((retry_at, str(scope), str(item.get("reason", ""))))
    if not active:
        return None
    retry_at, scope, reason = max(active)
    return f"cooldown {scope} {reason} {fmt_countdown(retry_at, now)}"


# -----------------------------------------------------------------------------
# 组装


def _apply_context_window(payload: dict[str, object], window: int | None) -> None:
    """按快照的 compact 窗口重算上下文分母与比例；窗口为 None 时不动。"""
    context_window = payload.get("context_window")
    if window is None or not isinstance(context_window, dict):
        return
    context_window["context_window_size"] = window
    total_input = _int(context_window.get("total_input_tokens"))
    if total_input is None or total_input < 0:
        return
    used = total_input / window * 100
    context_window["used_percentage"] = used
    context_window["remaining_percentage"] = max(0.0, 100.0 - used)


def _apply_model(payload: dict[str, object], model: ModelSnapshot) -> None:
    """显示名换成快照里的 display；effort 按后端档位换算，不支持档位时删掉。"""
    info = payload.get("model")
    if isinstance(info, dict):
        info["display_name"] = model["display"]
    effort = payload.get("effort")
    level = effort.get("level") if isinstance(effort, dict) else None
    if not isinstance(effort, dict) or not isinstance(level, str):
        return
    effective = render.effective_effort(
        level, model["efforts"], dynamic_allowed=model["dynamic_allowed"]
    )
    if effective is None:
        payload.pop("effort", None)
    else:
        effort["level"] = effective


def _sanitize(source: Mapping[str, object]) -> dict[str, object]:
    """复制输入并去掉不是 claudex 口径的字段：原生费用、原生额度、prompt_cache。"""
    payload: dict[str, object] = json.loads(json.dumps(source))
    cost = payload.get("cost")
    if isinstance(cost, dict):
        cost.pop("total_cost_usd", None)
    payload.pop("rate_limits", None)
    payload.pop("prompt_cache", None)
    return payload


def prepare(
    source: Mapping[str, object],
    profile: ProfileSnapshot | None,
    quota_data: Mapping[str, object] | None,
    state: dict[str, object],
    now: int,
    *,
    fast: bool,
) -> dict[str, object]:
    """把状态栏输入改写为 claudex 口径，返回带 `claudex` 对象的新 JSON。

    参数
    ----------
    profile : ProfileSnapshot | None
        会话快照；None 时只去掉原生费用、原生额度与 `prompt_cache`。
    quota_data : Mapping[str, object] | None
        `quota.load_quota` 的结果。
    state : dict[str, object]
        会话费用状态，原地更新；`profile` 为 None 时不动。
    fast : bool
        会话是否以 `--fast` 启动（`CLAUDEX_FAST=1`）。

    返回
    ----------
    dict[str, object]
        `cost.total_cost_usd` 为 claudex 结算的累计费用，没有可计价的响应时不写；
        `rate_limits` 只在当前模型是订阅来源且有额度数据时写；`claudex` 对象字段见
        `ClaudexObject`。
    """
    payload = _sanitize(source)
    claudex: ClaudexObject = {
        "profile": None,
        "source": None,
        "source_type": None,
        "cost_estimated": False,
        "fast": False,
        "quota_label": None,
        "markers": [],
    }
    if profile is None:
        claudex["markers"].append("profile unavailable")
        payload["claudex"] = claudex
        return payload
    claudex["profile"] = profile["profile"]
    _apply_context_window(payload, profile["compact_window"])
    update_cost_state(state, source, profile)
    settled = _int(state["settled_events"]) or 0
    cost = payload.get("cost")
    if settled > 0 and isinstance(cost, dict):
        cost["total_cost_usd"] = _float(state["settled_usd"]) or 0.0
        claudex["cost_estimated"] = state["estimated"] is True
    model = find_model(profile, _current_model_id(payload))
    if model is not None:
        _apply_model(payload, model)
        claudex["source"] = model["source"]
        claudex["source_type"] = model["source_type"]
        claudex["fast"] = fast and model["source_type"] == FAST_SOURCE_TYPE
        if model["source_type"] in quota.QUOTA_TYPES:
            view = quota_view(quota_data, model["source"], model["source_type"], now)
            claudex["quota_label"] = model["source"]
            limits = rate_limits_for(model, view, now)
            if limits is not None:
                payload["rate_limits"] = limits
            claudex["markers"].append(_quota_marker(model["source"], model, view, now))
            cooldown = _cooldown_marker(model, view, now)
            if cooldown is not None:
                claudex["markers"].append(cooldown)
        if claudex["fast"]:
            claudex["markers"].append(FAST_COST_MARKER)
    if settled > 0 and (_int(state["unpriced_events"]) or 0) > 0:
        claudex["markers"].append(COST_PARTIAL_MARKER)
    payload["claudex"] = claudex
    return payload


def builtin_line(payload: Mapping[str, object]) -> str:
    """内置简版：显示名、effort、费用与额度标记，读改写后的 JSON 与 `claudex` 对象。"""
    claudex = payload.get("claudex")
    info = claudex if isinstance(claudex, dict) else {}
    model = payload.get("model")
    name = model.get("display_name") if isinstance(model, dict) else None
    parts = [str(name) if name else "claudex"]
    if info.get("fast") is True:
        parts[0] += FAST_SUFFIX
    effort = payload.get("effort")
    level = effort.get("level") if isinstance(effort, dict) else None
    if isinstance(level, str):
        parts.append(level)
    cost = payload.get("cost")
    total = _float(cost.get("total_cost_usd")) if isinstance(cost, dict) else None
    if total is not None:
        mark = ESTIMATE_MARK if info.get("cost_estimated") is True else ""
        parts.append(f"{mark}${total:.2f}")
    markers = info.get("markers")
    if isinstance(markers, list):
        parts.extend(str(marker) for marker in markers)
    return " · ".join(parts)


def run_renderer(command: str | None, payload: Mapping[str, object]) -> str:
    """交给底层渲染器，返回要写到 stdout 的文本。

    `command` 为 None 或空时直接给内置简版，不写 stderr。以 `/bin/sh -c` 执行，stdin 为
    改写后的 JSON，超时 3 秒，stderr 原样转写；非零退出、超时、退出 0 而 stdout 为空、
    命令含 `claudex.statusline` 四种情形在 stderr 写原因后给内置简版。
    """
    if not command:
        return builtin_line(payload)
    if RECURSION_MARK in command:
        _warn(f"{render.STATUSLINE_COMMAND_ENV} 指向 claudex.statusline，不执行")
        return builtin_line(payload)
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=RENDERER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _warn(f"底层渲染器超过 {RENDERER_TIMEOUT_SECONDS} 秒未结束")
        return builtin_line(payload)
    except OSError as err:
        _warn(f"底层渲染器无法执行：{err}")
        return builtin_line(payload)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        _warn(f"底层渲染器退出码 {result.returncode}")
        return builtin_line(payload)
    if not result.stdout:
        _warn("底层渲染器退出码 0 但没有输出")
        return builtin_line(payload)
    return result.stdout


# -----------------------------------------------------------------------------
# 后台刷新


def refresh_command() -> tuple[str, ...]:
    """后台刷新程序的命令：同一解释器以模块方式运行 `claudex.quota`。"""
    return (sys.executable, "-P", "-m", REFRESH_MODULE)


def spawn_refresh(command: Sequence[str]) -> None:
    """脱离状态栏进程拉起刷新程序，不等它结束。"""
    subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def maybe_spawn_refresh(now: int) -> bool:
    """该拉起时拉起刷新程序，返回是否拉起。

    节流判断与 `spawned_at` 的记录都交给 `quota.claim_refresh`（持 `refresh.lock`
    读改写 `refresh.json`）；`CLAUDEX_NO_REFRESH=1` 时不拉起、不写文件。
    """
    if os.environ.get(NO_REFRESH_ENV) == "1":
        return False
    if not quota.claim_refresh(now):
        return False
    spawn_refresh(refresh_command())
    return True


# -----------------------------------------------------------------------------
# 入口


def _read_input() -> dict[str, object]:
    try:
        parsed: object = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        _warn(f"stdin parse: {err}")
        return {}
    if not isinstance(parsed, dict):
        _warn(f"stdin not an object: {type(parsed).__name__}")
        return {}
    return parsed


def _read_profile() -> ProfileSnapshot | None:
    configured = os.environ.get(render.PROFILE_FILE_ENV)
    if not configured:
        return None
    path = Path(configured)
    try:
        profile = load_profile_snapshot(path)
    except (OSError, ValueError) as err:
        _warn(f"profile unreadable: {err}")
        return None
    try:
        render.touch_snapshot(path)
    except OSError as err:
        _warn(f"snapshot touch: {err}")
    return profile


def main() -> int:
    """读一次状态栏输入，改写后渲染到 stdout；任何失败都不让状态栏空白。"""
    source = _read_input()
    now = int(time.time())
    profile = _read_profile()
    state_file = state_path(str(source.get("session_id") or "x"))
    state = load_state(state_file)
    fast = os.environ.get(FAST_ENV) == "1"
    try:
        payload = prepare(source, profile, load_quota(), state, now, fast=fast)
    except Exception as err:
        # 状态栏每秒运行一次，改写逻辑里没料到的输入不能让整行消失；原因写进 stderr
        _warn(f"prepare: {type(err).__name__}: {err}")
        payload = _sanitize(source)
    else:
        if profile is not None:
            try:
                jsonio.write_json_atomic(state_file, state)
            except OSError as err:
                _warn(f"state write: {err}")
    try:
        maybe_spawn_refresh(now)
    except OSError as err:
        _warn(f"refresh: {err}")
    sys.stdout.write(
        run_renderer(os.environ.get(render.STATUSLINE_COMMAND_ENV), payload)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
