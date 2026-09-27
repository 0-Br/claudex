"""OpenRouter 模型目录的缓存、模型元数据取值与按目录标价的计费。

目录只有一份缓存 `catalog.json`（`paths.catalog_file()`），存每个 slug 的显示名、
context、`supported_parameters` 与单价；单价一律是每 token 的美元数。元数据取值
顺序：`claudex.toml` 里的显式值，其次订阅来源取网关模型定义、通用来源取 slug 的
目录条目；显示名最后退到模型 id。抓取目录走外网，经环境代理。
"""

import http.client
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from claudex import __version__, jsonio, paths
from claudex.config import SUBSCRIPTION_TYPES, ModelRef

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
FETCH_TIMEOUT_SECONDS = 10
CATALOG_SCHEMA = 1
# `supported_parameters` 含其中任一项时，模型按网关缺省的三档处理
REASONING_PARAMETERS = ("reasoning", "reasoning_effort")
# 损坏的缓存文件由重新抓取覆盖，报错时一并提示
_RECOVERY_HINT = "运行 claudex update 重新抓取"
DEFAULT_EFFORTS = ("low", "medium", "high")
FETCHED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_USER_AGENT = f"claudex/{__version__}"
_ENTRY_KEYS = ("name", "context_length", "supported_parameters", "pricing")
_PRICE_KEYS = ("prompt", "completion", "input_cache_read", "input_cache_write")
# 网关给模型 id 附加的 `[1m]` 与档位后缀 `(…)`，与定义里的 id 比对前去掉
_ONE_M_SUFFIX = re.compile(r"(\[1m\])+$", re.IGNORECASE)
_THINKING_SUFFIX = re.compile(r"\([^()]*\)$")


class CatalogError(RuntimeError):
    """抓取 OpenRouter 目录失败：网络错误、超时、非 2xx 或应答形状不对。"""


@dataclass(frozen=True)
class Pricing:
    """每 token 的美元单价；缓存单价缺失为 None，计费时按输入单价计。"""

    prompt: float
    completion: float
    input_cache_read: float | None
    input_cache_write: float | None


@dataclass(frozen=True)
class CatalogEntry:
    """目录里一个 slug 的条目；`pricing` 为 None 表示目录没有固定单价。"""

    name: str | None
    context_length: int | None
    supported_parameters: tuple[str, ...]
    pricing: Pricing | None


@dataclass(frozen=True)
class Catalog:
    """整份目录：slug 到条目的映射、抓取时刻（带时区的 UTC 时间），以及抓取时因
    格式无法解析而跳过的条目。

    `skipped` 按字典序排列；条目缺字符串 id 时记为它在应答里的位置 `data[<i>]`。
    """

    fetched_at: datetime
    models: dict[str, CatalogEntry]
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelMeta:
    """一个模型引用的元数据；context 取不到时为 None，efforts 为 None 即无档位。"""

    context: int | None
    efforts: tuple[str, ...] | None
    display: str


def _now(now: datetime | None) -> datetime:
    return datetime.now(UTC) if now is None else now


def _format_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(FETCHED_AT_FORMAT)


def _parse_time(value: object, where: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{where} 必须是 ISO 8601 UTC 时间字符串，收到 {value!r}")
    try:
        moment = datetime.strptime(value, FETCHED_AT_FORMAT)
    except ValueError as err:
        raise ValueError(
            f"{where} 必须形如 2026-01-01T00:00:00Z，收到 {value!r}"
        ) from err
    return moment.replace(tzinfo=UTC)


def _positive_int_or_none(value: object, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{where} 必须是正整数或 null，收到 {value!r}")
    return value


def _string_or_none(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where} 必须是字符串或 null，收到 {value!r}")
    return value


def _string_tuple(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} 必须是字符串列表，收到 {value!r}")
    return tuple(value)


def _price(value: object, where: str) -> float:
    """把 OpenRouter 的单价（数字字符串或数字）转成 float。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{where} 必须是数字或数字字符串，收到 {value!r}")
    try:
        price = float(value)
    except ValueError as err:
        raise ValueError(f"{where} 不是数字：{value!r}") from err
    if not math.isfinite(price):
        raise ValueError(f"{where} 必须是有限数，收到 {value!r}")
    return price


def _payload_pricing(value: object, where: str) -> Pricing | None:
    """OpenRouter 应答里的 `pricing`；没有该字段，或输入、输出单价为负（OpenRouter
    用负数表示路由类模型没有固定单价）时返回 None。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{where} 必须是对象，收到 {value!r}")
    for key in ("prompt", "completion"):
        if key not in value:
            raise ValueError(f"{where}.{key} 缺失")
    prompt = _price(value["prompt"], f"{where}.prompt")
    completion = _price(value["completion"], f"{where}.completion")
    if prompt < 0 or completion < 0:
        return None
    cache: dict[str, float | None] = {}
    for key in ("input_cache_read", "input_cache_write"):
        raw = value.get(key)
        price = None if raw is None else _price(raw, f"{where}.{key}")
        cache[key] = price if price is not None and price >= 0 else None
    return Pricing(
        prompt=prompt,
        completion=completion,
        input_cache_read=cache["input_cache_read"],
        input_cache_write=cache["input_cache_write"],
    )


def _payload_entry(raw: dict[str, object], where: str) -> CatalogEntry:
    parameters = raw.get("supported_parameters")
    return CatalogEntry(
        name=_string_or_none(raw.get("name"), f"{where}.name"),
        context_length=_positive_int_or_none(
            raw.get("context_length"), f"{where}.context_length"
        ),
        supported_parameters=(
            ()
            if parameters is None
            else _string_tuple(parameters, f"{where}.supported_parameters")
        ),
        pricing=_payload_pricing(raw.get("pricing"), f"{where}.pricing"),
    )


def _try_payload_entry(raw: object, index: int) -> tuple[str, CatalogEntry | None]:
    """解析应答里的一个条目，返回 (slug, 条目)；格式无法解析时条目为 None。

    缺字符串 id 的条目以 `data[<index>]` 代替 slug。
    """
    slug = raw.get("id") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or not isinstance(slug, str) or not slug:
        return f"data[{index}]", None
    try:
        return slug, _payload_entry(raw, f"data[{index}]（{slug}）")
    except ValueError:
        return slug, None


def _parse_payload(payload: object, fetched_at: datetime) -> Catalog:
    """解析 `GET /api/v1/models` 的应答。

    顶层缺 data 列表时整份拒收；单个条目格式无法解析时跳过它，slug 记进
    `Catalog.skipped`，其余条目照常收下。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("OpenRouter 目录应答缺顶层 data 列表")
    models: dict[str, CatalogEntry] = {}
    skipped: list[str] = []
    for index, raw in enumerate(payload["data"]):
        slug, entry = _try_payload_entry(raw, index)
        if entry is None:
            skipped.append(slug)
        else:
            models[slug] = entry
    return Catalog(fetched_at=fetched_at, models=models, skipped=tuple(sorted(skipped)))


def _cached_pricing(value: object, where: str) -> Pricing | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(_PRICE_KEYS):
        raise ValueError(f"{where} 必须是含 {list(_PRICE_KEYS)} 的对象或 null")
    cache_read = value["input_cache_read"]
    cache_write = value["input_cache_write"]
    return Pricing(
        prompt=_price(value["prompt"], f"{where}.prompt"),
        completion=_price(value["completion"], f"{where}.completion"),
        input_cache_read=(
            None
            if cache_read is None
            else _price(cache_read, f"{where}.input_cache_read")
        ),
        input_cache_write=(
            None
            if cache_write is None
            else _price(cache_write, f"{where}.input_cache_write")
        ),
    )


def _cached_entry(value: object, where: str) -> CatalogEntry:
    if not isinstance(value, dict) or set(value) != set(_ENTRY_KEYS):
        raise ValueError(f"{where} 必须是含 {list(_ENTRY_KEYS)} 的对象")
    return CatalogEntry(
        name=_string_or_none(value["name"], f"{where}.name"),
        context_length=_positive_int_or_none(
            value["context_length"], f"{where}.context_length"
        ),
        supported_parameters=_string_tuple(
            value["supported_parameters"], f"{where}.supported_parameters"
        ),
        pricing=_cached_pricing(value["pricing"], f"{where}.pricing"),
    )


def _catalog_to_json(catalog: Catalog) -> dict[str, object]:
    models: dict[str, object] = {}
    for slug, entry in catalog.models.items():
        pricing = entry.pricing
        models[slug] = {
            "name": entry.name,
            "context_length": entry.context_length,
            "supported_parameters": list(entry.supported_parameters),
            "pricing": None
            if pricing is None
            else {
                "prompt": pricing.prompt,
                "completion": pricing.completion,
                "input_cache_read": pricing.input_cache_read,
                "input_cache_write": pricing.input_cache_write,
            },
        }
    return {
        "schema": CATALOG_SCHEMA,
        "fetched_at": _format_time(catalog.fetched_at),
        "models": models,
        "skipped": sorted(catalog.skipped),
    }


def _load_catalog_file(path: Path) -> Catalog | None:
    data = jsonio.read_json_object(path)
    if data is None:
        return None
    if data.get("schema") != CATALOG_SCHEMA:
        raise ValueError(
            f"{path}: schema 必须是 {CATALOG_SCHEMA}，收到 {data.get('schema')!r}"
        )
    fetched_at = _parse_time(data.get("fetched_at"), f"{path}: fetched_at")
    raw_models = data.get("models")
    if not isinstance(raw_models, dict):
        raise ValueError(f"{path}: models 必须是对象")
    models = {
        str(slug): _cached_entry(entry, f"{path}: models[{slug!r}]")
        for slug, entry in raw_models.items()
    }
    skipped = _string_tuple(data.get("skipped"), f"{path}: skipped")
    return Catalog(fetched_at=fetched_at, models=models, skipped=skipped)


def _definition_for(
    ref: ModelRef, definitions: Mapping[str, Mapping[str, object]]
) -> Mapping[str, object] | None:
    """在该订阅通道的网关模型定义里找 `ref.model_id`；先比原 id，再比去掉
    `[1m]` 与 `(…)` 后缀后的 id。"""
    payload = definitions.get(ref.source_type)
    if payload is None:
        raise ValueError(
            f"缺 {ref.source_type} 通道的网关模型定义，无法取 {ref.ref} 的元数据"
        )
    models = payload.get("models")
    if not isinstance(models, list):
        raise ValueError(f"{ref.source_type} 通道的网关模型定义缺 models 列表")
    candidates: list[Mapping[str, object]] = []
    for index, entry in enumerate(models):
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(model_id, str):
            raise ValueError(
                f"{ref.source_type} 通道的网关模型定义 models[{index}] 缺字符串 id"
            )
        candidates.append(entry)
    return match_definition(ref.model_id, candidates)


def _definition_context(definition: Mapping[str, object] | None) -> int | None:
    if definition is None:
        return None
    context = definition.get("context_length")
    if isinstance(context, bool) or not isinstance(context, int) or context <= 0:
        return None
    return context


def _definition_efforts(
    definition: Mapping[str, object] | None,
) -> tuple[str, ...] | None:
    if definition is None:
        return None
    thinking = definition.get("thinking")
    levels = thinking.get("levels") if isinstance(thinking, dict) else None
    if not isinstance(levels, list) or not levels:
        return None
    return tuple(str(level) for level in levels)


def match_definition(
    model_id: str, entries: Iterable[Mapping[str, object]]
) -> Mapping[str, object] | None:
    """在一个订阅通道的网关模型定义条目里找 `model_id`。

    先找 `id` 与 `model_id` 相等的条目，再找 `id` 去掉尾部 `[1m]`（可重复，不分大小写）
    与档位后缀 `(…)` 后相等的条目；`id` 不是字符串的条目跳过。找不到返回 None。
    """
    candidates = [entry for entry in entries if isinstance(entry.get("id"), str)]
    for entry in candidates:
        if entry.get("id") == model_id:
            return entry
    for entry in candidates:
        raw = str(entry.get("id"))
        if _THINKING_SUFFIX.sub("", _ONE_M_SUFFIX.sub("", raw.strip())) == model_id:
            return entry
    return None


def fetch_catalog(
    *, now: datetime | None = None, url: str = OPENROUTER_MODELS_URL
) -> Catalog:
    """抓取 OpenRouter 公开模型目录（免鉴权，经环境代理，超时 10 秒）。

    参数
    ----------
    now : datetime | None
        记为抓取时刻，须带时区；None 时取当前 UTC 时间。缓存只精确到秒。
    url : str
        目录地址，测试用。

    异常
    ----------
    CatalogError
        网络错误、超时、非 2xx、不是 JSON，或应答形状不对。
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    # 每次新建 opener，按调用当时的环境变量取代理；`urlopen` 会缓存首次调用时的 opener
    opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            raw: bytes = response.read()
    except urllib.error.HTTPError as err:
        raise CatalogError(f"OpenRouter 目录 {url} 返回 HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise CatalogError(f"OpenRouter 目录 {url} 请求失败：{err.reason}") from err
    except (OSError, http.client.HTTPException) as err:
        raise CatalogError(
            f"OpenRouter 目录 {url} 请求失败：{type(err).__name__}"
        ) from err
    try:
        payload: object = json.loads(raw.decode("utf-8"))
        return _parse_payload(payload, _now(now).astimezone(UTC).replace(microsecond=0))
    except (UnicodeDecodeError, ValueError) as err:
        raise CatalogError(f"OpenRouter 目录 {url} 的应答无法解析：{err}") from err


def load_catalog() -> Catalog | None:
    """读取 `catalog.json`；文件不存在时返回 None。

    异常
    ----------
    ValueError
        不是 JSON 对象、schema 不是 1，或任一字段形状不对；消息含路径与位置，末尾
        提示运行 `claudex update` 重新抓取。
    """
    path = paths.catalog_file()
    try:
        return _load_catalog_file(path)
    except ValueError as err:
        raise ValueError(f"{err}；{_RECOVERY_HINT}") from err


def save_catalog(catalog: Catalog) -> bool:
    """把目录写进 `catalog.json`（原子写，0600），返回是否真的改写了文件。"""
    return jsonio.write_json_atomic(paths.catalog_file(), _catalog_to_json(catalog))


def ensure_catalog(*, now: datetime | None = None) -> Catalog:
    """返回缓存的目录；没有缓存时同步抓取一次并写入缓存。

    异常
    ----------
    CatalogError
        没有缓存且抓取失败。
    ValueError
        缓存文件损坏（见 `load_catalog`）。
    """
    cached = load_catalog()
    if cached is not None:
        return cached
    catalog = fetch_catalog(now=now)
    save_catalog(catalog)
    return catalog


def refresh_catalog_if_stale(
    max_age: timedelta, *, now: datetime | None = None
) -> bool:
    """缓存缺失或抓取时刻早于 `now - max_age` 时重新抓取并写入，返回是否抓取了。

    抓取失败时旧缓存原样保留，`CatalogError` 抛给调用方；缓存文件损坏时
    `load_catalog` 的 `ValueError` 同样抛出，不自动覆盖。
    """
    moment = _now(now)
    cached = load_catalog()
    if cached is not None and moment - cached.fetched_at < max_age:
        return False
    save_catalog(fetch_catalog(now=moment))
    return True


def model_metadata(
    ref: ModelRef,
    catalog: Catalog,
    definitions: Mapping[str, Mapping[str, object]],
) -> ModelMeta:
    """按 spec 的取值顺序得出一个模型引用的 context、档位与显示名。

    参数
    ----------
    ref : ModelRef
        `config.resolve_ref` 的结果。
    catalog : Catalog
        OpenRouter 目录。
    definitions : Mapping[str, Mapping[str, object]]
        订阅通道名（`codex`、`antigravity`）到 `gateway.fetch_model_definitions`
        返回值的映射。只在订阅来源的模型缺显式 context 或 efforts 时查用。

    返回
    ----------
    ModelMeta
        context：显式值，其次订阅来源取定义里的 `context_length`、通用来源取目录条目
        的 `context_length`，都没有为 None。efforts：显式值，其次订阅来源取定义里的
        `thinking.levels`、通用来源在 `supported_parameters` 含 `reasoning` 或
        `reasoning_effort` 时取 `DEFAULT_EFFORTS`，否则 None。display：显式值，
        其次 slug 在目录里的 `name`，最后 `ref.model_id`。

    异常
    ----------
    ValueError
        需要订阅定义而 `definitions` 缺该通道，或该通道的定义形状不对。
    """
    entry = catalog.models.get(ref.openrouter) if ref.openrouter is not None else None
    context = ref.context
    efforts = ref.efforts
    if ref.source_type in SUBSCRIPTION_TYPES:
        if context is None or efforts is None:
            definition = _definition_for(ref, definitions)
            if context is None:
                context = _definition_context(definition)
            if efforts is None:
                efforts = _definition_efforts(definition)
    elif entry is not None:
        if context is None:
            context = entry.context_length
        if efforts is None and any(
            parameter in entry.supported_parameters
            for parameter in REASONING_PARAMETERS
        ):
            efforts = DEFAULT_EFFORTS
    display = ref.display
    if display is None and entry is not None:
        display = entry.name
    return ModelMeta(
        context=context,
        efforts=efforts,
        display=display if display is not None else ref.model_id,
    )


def pricing_for(ref: ModelRef, catalog: Catalog) -> Pricing | None:
    """模型引用按 slug 在目录里的单价；没有 slug、目录缺条目或条目无价时为 None。"""
    if ref.openrouter is None:
        return None
    entry = catalog.models.get(ref.openrouter)
    return None if entry is None else entry.pricing


def usage_cost_usd(usage: Mapping[str, object], pricing: Pricing) -> float:
    """按 Anthropic 形状的 usage 块算一次响应的美元费用。

    参数
    ----------
    usage : Mapping[str, object]
        读 `input_tokens`、`output_tokens`、`cache_read_input_tokens` 与
        `cache_creation_input_tokens`；缺失或不是数字的分量按 0 计。
    pricing : Pricing
        每 token 单价；缓存读写单价缺失时按输入单价计。
    """

    def tokens(name: str) -> float:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    cache_read = (
        pricing.input_cache_read
        if pricing.input_cache_read is not None
        else pricing.prompt
    )
    cache_write = (
        pricing.input_cache_write
        if pricing.input_cache_write is not None
        else pricing.prompt
    )
    return (
        tokens("input_tokens") * pricing.prompt
        + tokens("output_tokens") * pricing.completion
        + tokens("cache_read_input_tokens") * cache_read
        + tokens("cache_creation_input_tokens") * cache_write
    )


def is_estimated(ref: ModelRef) -> bool:
    """费用是否为等价花费（状态栏标 ≈）：来源类型不是 `openrouter` 即是。"""
    return ref.source_type != "openrouter"
