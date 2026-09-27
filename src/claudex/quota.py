"""后台刷新程序：订阅额度、OpenRouter 余额、OpenRouter 目录与网关新版记录。

`python -P -m claudex.quota` 由状态栏按 60 秒节流拉起，也可以手动运行，刷新一次后退出。
Codex 与 Antigravity 的额度经本机网关管理接口（`auth-files` 选凭据、`api-call` 代调用
上游）取得；`openrouter` 类来源的账户余额直连 `GET /api/v1/credits`。每个来源独立请求、
独立失败，失败时保留上一次成功的数据并记下错误分类。

写出的三个文件都经 `jsonio.write_json_atomic`（0600）：`quota.json`（状态栏读的额度
缓存）、`refresh.json`（节流与尝试记录，读改写一律持 `refresh.lock`）与
`gateway-release.json`（网关最新版本与查询时刻）。key 只从 key 文件读，不进任何消息。
"""

import contextlib
import fcntl
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from claudex import __version__, catalog, gateway, jsonio, paths
from claudex.catalog import CatalogError
from claudex.config import Config, ConfigError, load_config
from claudex.gateway import GatewayError, GatewaySecrets

QUOTA_SCHEMA = 1
RELEASE_SCHEMA = 1
REFRESH_INTERVAL_SECONDS = 60
QUOTA_TIMEOUT_SECONDS = 15
RELEASE_TIMEOUT_SECONDS = 10
CATALOG_MAX_AGE = timedelta(hours=24)
RELEASE_MAX_AGE = timedelta(hours=24)
ERROR_DETAIL_LIMIT = 160
QUOTA_TYPES = ("codex", "antigravity", "openrouter")
SUBSCRIPTION_QUOTA_TYPES = ("codex", "antigravity")
AUTH_FILES_PATH = "/v0/management/auth-files"
API_CALL_PATH = "/v0/management/api-call"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# 同一个接口的三个域名，超时或网关代调用失败时按顺序换下一个
ANTIGRAVITY_QUOTA_URLS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
)
ANTIGRAVITY_USER_AGENT = (
    "antigravity/cli/1.0.13 (aidev_client; os_type=linux; arch=x86_64)"
)
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
LATEST_RELEASE_URL = (
    "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest"
)
COOLDOWN_SCOPES = ("credential", "model")
FIVE_HOUR_WINDOW_SECONDS = 18_000
SEVEN_DAY_WINDOW_SECONDS = 604_800
WINDOW_NAME_BY_SECONDS = {
    FIVE_HOUR_WINDOW_SECONDS: "five_hour",
    SEVEN_DAY_WINDOW_SECONDS: "seven_day",
}
# 错误分类到状态栏显示原因的映射；分类本身写进 `error_kind`
ERROR_REASONS = {
    "credential_missing": "no credential",
    "credential_disabled": "credential disabled",
    "credential_multiple": "multiple credentials",
    "credential_incomplete": "credential incomplete",
    "credential_cooldown": "credential cooldown",
    "management_error": "management unavailable",
    "config_error": "config error",
    "response_invalid": "invalid response",
    "upstream_error": "refresh failed",
}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_USER_AGENT = f"claudex/{__version__}"
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")


class CooldownView(TypedDict):
    """网关对一条凭据或一个模型的本地冷却，投影后落盘的形态。

    `scope` 为 `credential` 时 `model_key` 为空串；`retry_at` 为 epoch 秒。
    """

    scope: str
    model_key: str
    reason: str
    retry_at: int
    http_status: int | None


class RequestError(RuntimeError):
    """一次 HTTP 请求失败。`kind` 取 timeout、connection、http、invalid 之一；
    `kind` 为 http 时 `status` 为状态码。消息只含 URL 与原因，不含请求头。"""

    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class RefreshError(RuntimeError):
    """带稳定错误分类（`ERROR_REASONS` 的键）的来源刷新错误。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# -----------------------------------------------------------------------------
# HTTP


def _is_loopback(url: str) -> bool:
    return (urllib.parse.urlsplit(url).hostname or "") in _LOOPBACK_HOSTS


def _is_timeout(err: object) -> bool:
    return isinstance(err, TimeoutError)


def request_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, object] | None = None,
    *,
    timeout: float,
) -> dict[str, object]:
    """发一次请求并把应答解析成 JSON 对象。

    回环地址（本机网关）显式不走环境代理；其余地址每次按调用时的环境变量取代理。

    异常
    ----------
    RequestError
        超时（timeout）、连不上或连接中断（connection）、非 2xx（http）、应答不是 JSON
        对象（invalid）。
    """
    data: bytes | None = None
    request_headers = {"User-Agent": _USER_AGENT, **headers}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    opener = _NO_PROXY_OPENER if _is_loopback(url) else urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw: bytes = response.read()
    except urllib.error.HTTPError as err:
        raise RequestError("http", f"{url} HTTP {err.code}", err.code) from err
    except urllib.error.URLError as err:
        kind = "timeout" if _is_timeout(err.reason) else "connection"
        raise RequestError(kind, f"{url} {kind}: {err.reason}") from err
    except (OSError, http.client.HTTPException) as err:
        kind = "timeout" if _is_timeout(err) else "connection"
        raise RequestError(kind, f"{url} {kind}: {type(err).__name__}") from err
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RequestError("invalid", f"{url} 的应答不是 JSON") from err
    if not isinstance(parsed, dict):
        raise RequestError("invalid", f"{url} 的应答不是 JSON 对象")
    return parsed


def _management_json(
    method: str, path: str, management_key: str, body: Mapping[str, object] | None
) -> dict[str, object]:
    return request_json(
        method,
        f"{gateway.GATEWAY_URL}{path}",
        {"Authorization": f"Bearer {management_key}"},
        body,
        timeout=QUOTA_TIMEOUT_SECONDS,
    )


def api_call(
    management_key: str,
    auth_index: str,
    method: str,
    url: str,
    header: Mapping[str, str],
    data: str | None = None,
) -> tuple[int, str]:
    """经网关 `api-call` 以指定凭据代调用上游，返回 (上游状态码, 上游 body)。

    异常
    ----------
    RequestError
        到网关的请求失败（见 `request_json`）。
    RefreshError
        网关应答缺 `status_code` 或 `body`（response_invalid）。
    """
    body: dict[str, object] = {
        "auth_index": auth_index,
        "method": method,
        "url": url,
        "header": dict(header),
    }
    if data is not None:
        body["data"] = data
    payload = _management_json("POST", API_CALL_PATH, management_key, body)
    status = payload.get("status_code")
    upstream = payload.get("body")
    if isinstance(status, bool) or not isinstance(status, int):
        raise RefreshError(
            "response_invalid", f"api-call 对 {url} 的应答缺 status_code"
        )
    if not isinstance(upstream, str):
        raise RefreshError("response_invalid", f"api-call 对 {url} 的应答缺字符串 body")
    return status, upstream


def _parse_body(value: str, source: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as err:
        raise RefreshError("response_invalid", f"{source} 不是 JSON：{err}") from err
    if not isinstance(parsed, dict):
        raise RefreshError("response_invalid", f"{source} 不是 JSON 对象")
    return parsed


# -----------------------------------------------------------------------------
# 凭据选择与冷却投影


def _credential_time(entry: Mapping[str, object]) -> datetime:
    raw_time = entry.get("created_at")
    if not isinstance(raw_time, str) or raw_time == "":
        raw_time = entry.get("modtime")
    if not isinstance(raw_time, str) or raw_time == "":
        raise ValueError("expected comparable creation times for credentials")
    parsed = datetime.fromisoformat(raw_time)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expected comparable creation times for credentials")
    return parsed


def parse_cooldown_views(raw: object, provider: str) -> list[CooldownView] | None:
    """把 `auth-files` 条目的 `cooldowns` 投影成落盘形态。

    参数
    ----------
    raw : object
        条目的 `cooldowns`。null 表示该凭据的冷却不可观测（Home 模式与磁盘回落
        路径），空列表表示当前没有未过期的冷却，两者不合并。

    返回
    ----------
    list[CooldownView] | None
        null 原样返回 None；列表只保留 `scope`、`model_key`、`reason`、`retry_at`、
        `http_status`，`retry_at` 由 RFC 3339 换成 epoch 秒。`model_key` 与
        `http_status` 在上游是 omitempty，缺席时记为 `""` 与 None。

    异常
    ----------
    RefreshError
        response_invalid：不是列表、某项不是对象、缺必需键、类型不对、`scope`
        越界，或 `retry_at` 不可解析、不带时区。
    """
    if raw is None:
        return None

    def invalid(message: str) -> RefreshError:
        return RefreshError("response_invalid", f"{provider} cooldowns: {message}")

    if not isinstance(raw, list):
        raise invalid(f"expected a list, got {type(raw).__name__}")
    views: list[CooldownView] = []
    for item in raw:
        if not isinstance(item, dict):
            raise invalid(f"entry must be an object, got {type(item).__name__}")
        scope = item.get("scope")
        if not isinstance(scope, str) or scope not in COOLDOWN_SCOPES:
            raise invalid(
                f"scope must be one of {list(COOLDOWN_SCOPES)}, got {scope!r}"
            )
        model_key = item.get("model_key", "")
        if not isinstance(model_key, str):
            raise invalid(f"model_key must be a string, got {model_key!r}")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason:
            raise invalid(f"reason must be a non-empty string, got {reason!r}")
        retry_raw = item.get("retry_at")
        if not isinstance(retry_raw, str):
            raise invalid(f"retry_at must be an RFC 3339 string, got {retry_raw!r}")
        try:
            retry_at = datetime.fromisoformat(retry_raw)
        except ValueError as err:
            raise invalid(f"retry_at is unparsable, got {retry_raw!r}") from err
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            raise invalid(f"retry_at must carry a time zone, got {retry_raw!r}")
        status_raw = item.get("http_status")
        if status_raw is not None and (
            isinstance(status_raw, bool) or not isinstance(status_raw, int)
        ):
            raise invalid(f"http_status must be an integer, got {status_raw!r}")
        views.append(
            {
                "scope": scope,
                "model_key": model_key,
                "reason": reason,
                "retry_at": int(retry_at.timestamp()),
                "http_status": status_raw,
            }
        )
    return views


def select_credential(
    entries: object, provider: str
) -> tuple[dict[str, str], list[CooldownView] | None]:
    """从 `auth-files` 列表里选出某个订阅通道唯一可用的凭据及其冷却快照。

    只有 `disabled` 为 false 的凭据可用；模型的冷却或错误状态不影响额度查询。
    Codex 同一账号有多条记录时取创建时间唯一最新的一条；Antigravity 必须恰好一条。

    异常
    ----------
    RefreshError
        management_error（列表形状不对）、credential_missing、credential_disabled、
        credential_incomplete、credential_multiple；冷却投影失败为 response_invalid。
    """
    if not isinstance(entries, list):
        raise RefreshError(
            "management_error",
            f"expected auth files list, got {type(entries).__name__}",
        )
    provider_entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("provider") == provider
    ]
    if not provider_entries:
        raise RefreshError(
            "credential_missing", f"no {provider} credential is registered"
        )
    enabled = [entry for entry in provider_entries if entry.get("disabled") is False]
    if not enabled:
        raise RefreshError(
            "credential_disabled", f"all {provider} credentials are disabled"
        )
    selected: list[tuple[dict[str, str], dict[str, object]]] = []
    for entry in enabled:
        auth_index = entry.get("auth_index")
        account = entry.get("account")
        if not isinstance(account, str) or not account:
            account = entry.get("account_id")
        if not isinstance(account, str) or not account:
            account = entry.get("email")
        project_id = entry.get("project_id")
        if (
            not isinstance(auth_index, str)
            or not auth_index
            or not isinstance(account, str)
            or not account
            or (
                provider == "antigravity"
                and (not isinstance(project_id, str) or not project_id)
            )
        ):
            raise RefreshError(
                "credential_incomplete",
                f"enabled {provider} credential lacks required metadata",
            )
        credential = {"auth_index": auth_index, "account": account}
        if isinstance(project_id, str) and project_id:
            credential["project_id"] = project_id
        selected.append((credential, entry))
    accounts = {credential["account"] for credential, _entry in selected}
    if len(accounts) != 1:
        raise RefreshError(
            "credential_multiple",
            f"expected exactly one enabled {provider} account, got {len(accounts)}",
        )
    if len(selected) > 1 and provider == "antigravity":
        raise RefreshError(
            "credential_multiple",
            f"expected exactly one enabled antigravity credential, got {len(selected)}",
        )
    if len(selected) > 1:
        try:
            timestamped = [
                (credential, entry, _credential_time(entry))
                for credential, entry in selected
            ]
        except ValueError as err:
            raise RefreshError("credential_multiple", str(err)) from err
        latest = max(created for _credential, _entry, created in timestamped)
        newest = [(c, e) for c, e, created in timestamped if created == latest]
        if len(newest) != 1:
            raise RefreshError(
                "credential_multiple",
                f"expected one newest {provider} credential, got {len(newest)}",
            )
        selected = newest
    credential, entry = selected[0]
    return credential, parse_cooldown_views(entry.get("cooldowns"), provider)


# -----------------------------------------------------------------------------
# 三种额度


def _parse_window(value: object, name: str, now: int) -> tuple[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object, got {value!r}")
    window_seconds = value.get("limit_window_seconds")
    if (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or window_seconds not in WINDOW_NAME_BY_SECONDS
    ):
        raise ValueError(
            f"{name}.limit_window_seconds must be one of "
            f"{list(WINDOW_NAME_BY_SECONDS)}, got {window_seconds!r}"
        )
    used = value.get("used_percent")
    if (
        isinstance(used, bool)
        or not isinstance(used, (int, float))
        or not 0 <= used <= 100
    ):
        raise ValueError(
            f"{name}.used_percent must be numeric in [0, 100], got {used!r}"
        )
    reset_at = value.get("reset_at")
    if isinstance(reset_at, bool) or not isinstance(reset_at, int) or reset_at <= now:
        raise ValueError(
            f"{name}.reset_at must be an integer later than now={now}, got {reset_at!r}"
        )
    return WINDOW_NAME_BY_SECONDS[window_seconds], {
        "used_percentage": float(used),
        "resets_at": reset_at,
    }


def parse_codex_usage(body: Mapping[str, object], now: int) -> dict[str, object]:
    """解析 `wham/usage` 的应答。

    返回
    ----------
    dict[str, object]
        `{"windows": {"five_hour"|"seven_day": {"used_percentage", "resets_at"}},
        "plan_type": str|None}`；未返回的窗口不出现。

    异常
    ----------
    ValueError
        没有可用窗口、字段类型不对、百分比越界、窗口重复或重置时间不在未来。
    """
    rate_limit = body.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise ValueError(f"expected rate_limit object, got {rate_limit!r}")
    windows: dict[str, object] = {}
    for source_name in ("primary_window", "secondary_window"):
        value = rate_limit.get(source_name)
        if value is None:
            continue
        name, window = _parse_window(value, source_name, now)
        if name in windows:
            raise ValueError(f"duplicate rate-limit window {name!r}")
        windows[name] = window
    if not windows:
        raise ValueError("expected at least one non-null rate-limit window")
    plan_type = body.get("plan_type")
    return {
        "windows": windows,
        "plan_type": plan_type if isinstance(plan_type, str) else None,
    }


def _first(mapping: Mapping[str, object], *names: str) -> object:
    return next((mapping[name] for name in names if name in mapping), None)


def _parse_window_seconds(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if raw > 0 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    named = {"hourly": 3600, "daily": 86_400, "weekly": 604_800}
    if text in named:
        return named[text]
    units = {"s": 1, "m": 60, "h": 3600, "d": 86_400}
    if text and text[-1] in units and text[:-1].isdigit():
        return int(text[:-1]) * units[text[-1]]
    return int(text) if text.isdigit() else None


def _parse_reset_at(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw:
        with contextlib.suppress(ValueError):
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    return None


def parse_antigravity_quota(body: Mapping[str, object]) -> dict[str, object]:
    """解析 `retrieveUserQuotaSummary` 的 `groups[].buckets[]`（camel 与 snake 都认）。

    返回
    ----------
    dict[str, object]
        `{"buckets": [{"id", "display", "window_raw", "window_seconds", "reset_at",
        "remaining_fraction"}]}`，`remaining_fraction` 夹在 [0, 1]。

    异常
    ----------
    ValueError
        没有 groups 或没有任何可用的桶。
    """
    groups = body.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("expected non-empty groups in quota summary")
    buckets: list[dict[str, object]] = []
    for group in groups:
        raw_buckets = group.get("buckets") if isinstance(group, dict) else None
        if not isinstance(group, dict) or not isinstance(raw_buckets, list):
            continue
        group_name = _first(group, "displayName", "display_name")
        for bucket in raw_buckets:
            if not isinstance(bucket, dict):
                continue
            fraction = _first(bucket, "remainingFraction", "remaining_fraction")
            if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
                continue
            window_raw = bucket.get("window")
            display = _first(bucket, "displayName", "display_name")
            buckets.append(
                {
                    "id": str(_first(bucket, "bucketId", "bucket_id") or ""),
                    "display": str(display or group_name or ""),
                    "window_raw": window_raw if isinstance(window_raw, str) else None,
                    "window_seconds": _parse_window_seconds(window_raw),
                    "reset_at": _parse_reset_at(
                        _first(bucket, "resetTime", "reset_time")
                    ),
                    "remaining_fraction": max(0.0, min(1.0, float(fraction))),
                }
            )
    if not buckets:
        raise ValueError("quota summary has no usable buckets")
    return {"buckets": buckets}


def parse_openrouter_credits(body: Mapping[str, object]) -> dict[str, object]:
    """解析 `GET /api/v1/credits`：`total_credits` 与 `total_usage`（美元）。

    两个字段在 `data` 对象里；应答没有 `data` 对象时在顶层找。

    异常
    ----------
    ValueError
        任一字段缺失或不是数字。
    """
    data = body.get("data")
    source = data if isinstance(data, dict) else body
    result: dict[str, object] = {}
    for key in ("total_credits", "total_usage"):
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"credits.{key} must be numeric, got {value!r}")
        result[key] = float(value)
    return result


def refresh_codex(
    management_key: str, credential: Mapping[str, str], now: int
) -> dict[str, object]:
    """经网关用已选凭据代取 Codex 用量。"""
    try:
        status, body = api_call(
            management_key,
            credential["auth_index"],
            "GET",
            CODEX_USAGE_URL,
            {
                "Authorization": "Bearer $TOKEN$",
                "Accept": "application/json",
                "ChatGPT-Account-Id": credential["account"],
                "User-Agent": "CodexCLI",
            },
        )
    except RequestError as err:
        raise RefreshError("management_error", str(err)) from err
    if status == 429:
        raise RefreshError("credential_cooldown", "codex usage upstream status 429")
    if status != 200:
        raise RefreshError(
            "upstream_error", f"codex usage expected status 200, got {status}"
        )
    try:
        return parse_codex_usage(_parse_body(body, "codex usage body"), now)
    except ValueError as err:
        raise RefreshError("response_invalid", str(err)) from err


def refresh_antigravity(
    management_key: str, credential: Mapping[str, str]
) -> dict[str, object]:
    """经网关用已选凭据代取 Antigravity 额度摘要，三个域名依次尝试。

    换下一个域名的情形：上游非 2xx、应答解析失败、到网关的请求超时，以及网关
    代调用本身返回非 2xx。连不上网关时直接失败（management_error），换域名无用。
    """
    last_kind = "upstream_error"
    last_error = "no quota URL succeeded"
    for url in ANTIGRAVITY_QUOTA_URLS:
        try:
            status, body = api_call(
                management_key,
                credential["auth_index"],
                "POST",
                url,
                {
                    "Authorization": "Bearer $TOKEN$",
                    "Content-Type": "application/json",
                    "User-Agent": ANTIGRAVITY_USER_AGENT,
                },
                json.dumps({"project": credential["project_id"]}),
            )
        except RequestError as err:
            if err.kind == "connection":
                raise RefreshError("management_error", str(err)) from err
            last_kind, last_error = "upstream_error", str(err)
            continue
        if not 200 <= status < 300:
            last_kind = "credential_cooldown" if status == 429 else "upstream_error"
            last_error = f"{url} upstream status {status}"
            continue
        try:
            return parse_antigravity_quota(_parse_body(body, "quota summary"))
        except (RefreshError, ValueError) as err:
            last_kind, last_error = "response_invalid", str(err)
    raise RefreshError(last_kind, last_error)


def refresh_openrouter(api_key: str) -> dict[str, object]:
    """直连 OpenRouter 取账户余额（经环境代理）。"""
    try:
        payload = request_json(
            "GET",
            OPENROUTER_CREDITS_URL,
            {"Authorization": f"Bearer {api_key}"},
            timeout=QUOTA_TIMEOUT_SECONDS,
        )
    except RequestError as err:
        if err.status == 429:
            raise RefreshError("credential_cooldown", str(err)) from err
        kind = "response_invalid" if err.kind == "invalid" else "upstream_error"
        raise RefreshError(kind, str(err)) from err
    try:
        return parse_openrouter_credits(payload)
    except ValueError as err:
        raise RefreshError("response_invalid", str(err)) from err


# -----------------------------------------------------------------------------
# quota.json


def load_quota() -> dict[str, object] | None:
    """状态栏读的额度缓存视图：`quota.json` 的内容。

    返回
    ----------
    dict[str, object] | None
        文件不存在、schema 不是 `QUOTA_SCHEMA` 时为 None。

    异常
    ----------
    ValueError
        文件不是合法的 JSON 对象（由调用方决定怎么报）。
    """
    data = jsonio.read_json_object(paths.quota_file())
    if data is None or data.get("schema") != QUOTA_SCHEMA:
        return None
    return data


def _previous_records(previous: Mapping[str, object] | None) -> dict[str, object]:
    sources = previous.get("sources") if previous is not None else None
    return dict(sources) if isinstance(sources, dict) else {}


def _failure_record(
    old: object,
    kind: str,
    message: str,
    now: int,
    cooldowns: list[CooldownView] | None,
    *,
    inherit_cooldowns: bool,
) -> dict[str, object]:
    base: dict[str, object] = dict(old) if isinstance(old, dict) else {}
    base.update(
        {
            "ok": False,
            "error_kind": kind,
            "error": ERROR_REASONS.get(kind, ERROR_REASONS["upstream_error"]),
            "detail": message[:ERROR_DETAIL_LIMIT],
            "failed_at": now,
        }
    )
    base.setdefault("updated_at", None)
    base.setdefault("data", None)
    if inherit_cooldowns:
        base.setdefault("cooldowns", None)
    else:
        base["cooldowns"] = cooldowns
    return base


def refresh_sources(
    config: Config,
    secrets: GatewaySecrets | None,
    secrets_error: str | None,
    previous: Mapping[str, object] | None,
    now: int,
) -> dict[str, object]:
    """逐个来源刷新额度，返回新的 `quota.json` 内容。

    只处理 type 为 codex、antigravity、openrouter 的来源；每个来源独立失败。成功时
    记录为 `{"type", "updated_at", "ok": true, "error_kind": "", "error": null,
    "detail": "", "failed_at": null, "data", "cooldowns"}`；失败时沿用上一条记录的
    `updated_at`、`data`（last-good），把 `ok` 置 false 并写 `error_kind`、`error`
    （显示原因）、`detail`、`failed_at`。订阅来源在选出凭据之前就失败时沿用上一条
    记录的冷却快照；openrouter 来源的冷却不可观测，恒为 null。
    """
    old_records = _previous_records(previous)
    records: dict[str, object] = {}
    auth_files: object = None
    auth_error: RefreshError | None = None
    for name, source in config.sources.items():
        if source.type not in QUOTA_TYPES:
            continue
        cooldowns: list[CooldownView] | None = None
        inherit = source.type in SUBSCRIPTION_QUOTA_TYPES
        try:
            if secrets is None:
                raise RefreshError("config_error", secrets_error or "key 文件不可用")
            if source.type in SUBSCRIPTION_QUOTA_TYPES:
                if auth_files is None and auth_error is None:
                    try:
                        auth_files = _management_json(
                            "GET", AUTH_FILES_PATH, secrets.management_key, None
                        ).get("files")
                    except RequestError as err:
                        auth_error = RefreshError("management_error", str(err))
                if auth_error is not None:
                    raise auth_error
                credential, cooldowns = select_credential(auth_files, source.type)
                inherit = False
                data = (
                    refresh_codex(secrets.management_key, credential, now)
                    if source.type == "codex"
                    else refresh_antigravity(secrets.management_key, credential)
                )
            else:
                data = refresh_openrouter(secrets.source_keys[name])
        except RefreshError as err:
            records[name] = {
                "type": source.type,
                **_failure_record(
                    old_records.get(name),
                    err.kind,
                    str(err),
                    now,
                    cooldowns,
                    inherit_cooldowns=inherit,
                ),
            }
            continue
        records[name] = {
            "type": source.type,
            "updated_at": now,
            "ok": True,
            "error_kind": "",
            "error": None,
            "detail": "",
            "failed_at": None,
            "data": data,
            "cooldowns": cooldowns,
        }
    return {"schema": QUOTA_SCHEMA, "sources": records}


# -----------------------------------------------------------------------------
# refresh.json 与 refresh.lock


@contextlib.contextmanager
def refresh_lock(*, blocking: bool) -> Iterator[bool]:
    """持有 `refresh.lock` 的排他锁；产出是否拿到了锁。

    `blocking` 为假时锁被占用即产出 False，不等待。`refresh.json` 的一切读改写都
    在这把锁里做（`merge_refresh_record`），所以状态栏与刷新程序不会互相丢键。
    """
    path = paths.refresh_lock_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        os.fchmod(stream.fileno(), 0o600)
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(stream, flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_refresh_record() -> dict[str, object]:
    """读 `refresh.json`；不存在时为空对象。

    异常
    ----------
    ValueError
        文件不是合法的 JSON 对象。
    """
    return jsonio.read_json_object(paths.refresh_file()) or {}


def merge_refresh_record(fields: Mapping[str, object]) -> dict[str, object]:
    """把 `fields` 合并进 `refresh.json` 并原子写回，返回合并后的内容。

    只能在持有 `refresh_lock` 时调用；文件损坏时从空对象开始，不中断刷新。
    """
    try:
        record = read_refresh_record()
    except ValueError:
        record = {}
    merged = {**record, **fields}
    jsonio.write_json_atomic(paths.refresh_file(), merged)
    return merged


def _stamp(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def claim_refresh(now: int) -> bool:
    """状态栏用：判断现在该不该拉起刷新程序，该拉起时先记下 `spawned_at`。

    返回 False 的情形：`refresh.lock` 被占用（刷新程序正在运行），或
    `spawned_at`、`attempted_at` 中较晚的一个距今在 [0, 60) 秒内。
    """
    with refresh_lock(blocking=False) as held:
        if not held:
            return False
        try:
            record = read_refresh_record()
        except ValueError:
            record = {}
        stamps = [
            stamp
            for stamp in (_stamp(record, "spawned_at"), _stamp(record, "attempted_at"))
            if stamp is not None
        ]
        if stamps and 0 <= now - max(stamps) < REFRESH_INTERVAL_SECONDS:
            return False
        merge_refresh_record({"spawned_at": now})
        return True


# -----------------------------------------------------------------------------
# 网关新版记录


def fetch_latest_release() -> str:
    """查 CLIProxyAPI 的最新发布版本，返回去掉前缀 `v` 的版本号（如 `7.3.20`）。

    经环境代理，超时 10 秒。

    异常
    ----------
    RequestError
        请求失败。
    ValueError
        应答缺 `tag_name` 或它不是 `x.y.z` 形态。
    """
    payload = request_json(
        "GET",
        LATEST_RELEASE_URL,
        {"Accept": "application/vnd.github+json"},
        timeout=RELEASE_TIMEOUT_SECONDS,
    )
    tag = payload.get("tag_name")
    match = _VERSION.fullmatch(tag) if isinstance(tag, str) else None
    if match is None:
        raise ValueError(f"release 应答的 tag_name 不是版本号：{tag!r}")
    return match.group(1)


def load_release_record() -> dict[str, object] | None:
    """读 `gateway-release.json`；不存在、损坏或 schema 不符时为 None。"""
    try:
        data = jsonio.read_json_object(paths.gateway_release_file())
    except ValueError:
        return None
    if data is None or data.get("schema") != RELEASE_SCHEMA:
        return None
    return data


def write_release_record(version: str, now: int) -> None:
    """写 `gateway-release.json`：`{"schema": 1, "version", "checked_at"}`。"""
    jsonio.write_json_atomic(
        paths.gateway_release_file(),
        {"schema": RELEASE_SCHEMA, "version": version, "checked_at": now},
    )


def refresh_release_if_stale(now: int, max_age: timedelta = RELEASE_MAX_AGE) -> bool:
    """记录缺失或 `checked_at` 距今满 `max_age` 时查一次并写入，返回是否查了。

    异常
    ----------
    RequestError, ValueError
        查询失败；旧记录保留。
    """
    record = load_release_record()
    checked_at = _stamp(record, "checked_at") if record is not None else None
    if checked_at is not None and 0 <= now - checked_at < max_age.total_seconds():
        return False
    write_release_record(fetch_latest_release(), now)
    return True


# -----------------------------------------------------------------------------
# 入口


def _load_quota_for_refresh() -> dict[str, object] | None:
    try:
        return load_quota()
    except ValueError:
        return None


def refresh_once(now: int | None = None) -> int:
    """在 `refresh.lock` 里刷新一次，返回退出码。

    锁被占用、或 `attempted_at` 距今不满 60 秒时什么都不做。否则依次刷新额度、目录
    （24 小时）与网关新版记录（24 小时），各部分独立失败，错误写进 `refresh.json` 的
    `errors`（键为 `config`、`quota:<来源名>`、`catalog`、`release`）。单个部分失败
    不改变退出码，一律返回 0。
    """
    moment = int(time.time()) if now is None else now
    with refresh_lock(blocking=False) as held:
        if not held:
            return 0
        try:
            attempted_at = _stamp(read_refresh_record(), "attempted_at")
        except ValueError:
            attempted_at = None
        if (
            attempted_at is not None
            and 0 <= moment - attempted_at < REFRESH_INTERVAL_SECONDS
        ):
            return 0
        merge_refresh_record({"attempted_at": moment})
        errors: dict[str, str] = {}
        try:
            config = load_config(paths.config_file())
        except (OSError, ConfigError) as err:
            errors["config"] = str(err)[:ERROR_DETAIL_LIMIT]
        else:
            secrets: GatewaySecrets | None = None
            secrets_error: str | None = None
            try:
                secrets = gateway.load_secrets(config)
            except GatewayError as err:
                secrets_error = str(err)
            quota = refresh_sources(
                config, secrets, secrets_error, _load_quota_for_refresh(), moment
            )
            jsonio.write_json_atomic(paths.quota_file(), quota)
            sources = quota["sources"]
            assert isinstance(sources, dict)
            for name, record in sources.items():
                if isinstance(record, dict) and record.get("ok") is not True:
                    errors[f"quota:{name}"] = str(record.get("detail", ""))
        try:
            catalog.refresh_catalog_if_stale(
                CATALOG_MAX_AGE, now=datetime.fromtimestamp(moment, UTC)
            )
        except (CatalogError, ValueError) as err:
            errors["catalog"] = str(err)[:ERROR_DETAIL_LIMIT]
        try:
            refresh_release_if_stale(moment)
        except (RequestError, ValueError) as err:
            errors["release"] = str(err)[:ERROR_DETAIL_LIMIT]
        finished_at = int(time.time()) if now is None else moment
        merge_refresh_record(
            {"finished_at": finished_at, "ok": not errors, "errors": errors}
        )
    return 0


def main() -> int:
    """刷新一次后退出。"""
    return refresh_once()


if __name__ == "__main__":
    sys.exit(main())
