"""来源探测：列出上游模型，并经网关检验挑中的模型的四项能力。

列上游模型只要求来源已写进 `claudex.toml`、通用来源的 key 已写入；订阅来源列出网关
模型定义里的模型。探测模型要求它已列在该来源的 `models` 里，但不要求 context（这是
probe 与启动、preflight 的不同之处），经本机网关对每个模型各发几个极小请求：能否应答、
工具调用、各档位是否被接受、图片输入。
"""

import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from claudex import catalog, gateway, paths, quota
from claudex.config import (
    EFFORT_LEVELS,
    SUBSCRIPTION_TYPES,
    Config,
    ConfigError,
    ModelRef,
    resolve_ref,
)
from claudex.gateway import GatewayError
from claudex.quota import RequestError

UPSTREAM_TIMEOUT_SECONDS = 10
PROBE_TIMEOUT_SECONDS = 60
ANTHROPIC_VERSION = "2023-06-01"
KEY_FILE_MODE = 0o600
PROBE_MAX_TOKENS = 16
EFFORT_MAX_TOKENS = 64
# 1×1 像素的 PNG，图片输入检验用
PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/"
    "iZk9HQAAAABJRU5ErkJggg=="
)
ECHO_TOOL: dict[str, object] = {
    "name": "echo",
    "description": "Echo the given text back.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


class ProbeError(RuntimeError):
    """探测无法进行：上游不提供模型列表、key 不可用、模型不在网关里等。"""


@dataclass
class ProbeResult:
    """一个模型的探测结果；没能应答时其余三项为 None（未检验）。"""

    ref: ModelRef
    answered: bool
    answer_error: str = ""
    tools: bool | None = None
    efforts: dict[str, bool] = field(default_factory=dict)
    image: bool | None = None


def _source(config: Config, name: str) -> tuple[str, str | None]:
    source = config.sources.get(name)
    if source is None:
        raise ConfigError(
            f"来源 {name!r} 不在 claudex.toml 里，已定义 {sorted(config.sources)}"
        )
    return source.type, source.base_url


def read_source_key(name: str) -> str:
    """读一个通用来源的上游 key（`keys/<来源名>.key`）。

    只读这一个，不要求其余来源的 key 在场。

    异常
    ----------
    ProbeError
        文件不存在、权限不是 0600，或内容不是一行不含空白的文本；消息只含路径。
    """
    path = paths.source_key_file(name)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProbeError(f"{path} 不存在；运行 claudex key set {name} 写入") from None
    except (OSError, UnicodeDecodeError) as err:
        raise ProbeError(f"{path} 读取失败（{type(err).__name__}）") from None
    if mode != KEY_FILE_MODE:
        raise ProbeError(f"{path} 的权限应为 0600，实际为 {mode:04o}")
    key = raw.rstrip("\r\n")
    if not key or any(character.isspace() for character in key):
        raise ProbeError(f"{path} 格式不对：应为一行不含空白的 key")
    return key


def _model_ids(payload: Mapping[str, object], source_label: str) -> list[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise ProbeError(f"{source_label} 的模型列表应答缺 data 列表")
    return sorted(
        str(entry["id"])
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    )


def list_upstream_models(config: Config, name: str) -> list[str]:
    """列出来源 `name` 的上游模型 id。

    - openrouter：`GET <gateway.OPENROUTER_BASE_URL>/models`；
    - openai：`GET <base_url>/models`；
    - anthropic：`GET <base_url>/v1/models`（带 `x-api-key`），上游不提供时报错说明；
    - codex、antigravity：本机网关该通道的模型定义（需要网关在跑）。

    异常
    ----------
    ConfigError
        来源不存在。
    ProbeError
        key 不可用、上游不提供模型列表，或订阅来源取不到网关模型定义。
    GatewayError
        订阅来源的 key 文件不可用。
    """
    source_type, base_url = _source(config, name)
    if source_type in SUBSCRIPTION_TYPES:
        secrets = gateway.load_secrets(config)
        try:
            payload = gateway.fetch_model_definitions(
                secrets.management_key, source_type
            )
        except GatewayError as err:
            raise ProbeError(
                f"取不到网关 {source_type} 通道的模型定义：{err}；"
                "网关没在跑时先运行 claudex gateway start"
            ) from err
        models = payload.get("models")
        if not isinstance(models, list):
            raise ProbeError(f"网关 {source_type} 通道的模型定义缺 models 列表")
        return sorted(
            str(entry["id"])
            for entry in models
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        )
    key = read_source_key(name)
    if source_type == "openrouter":
        url = f"{gateway.OPENROUTER_BASE_URL}/models"
        headers = {"Authorization": f"Bearer {key}"}
    elif source_type == "openai":
        url = f"{str(base_url).rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {key}"}
    else:
        url = f"{str(base_url).rstrip('/')}/v1/models"
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    try:
        payload = quota.request_json(
            "GET", url, headers, timeout=UPSTREAM_TIMEOUT_SECONDS
        )
    except RequestError as err:
        hint = "；这个上游可能不提供模型列表接口" if source_type == "anthropic" else ""
        raise ProbeError(f"取不到来源 {name} 的上游模型列表：{err}{hint}") from err
    return _model_ids(payload, f"来源 {name}")


def _messages(
    ref: ModelRef,
    client_key: str,
    body: Mapping[str, object],
    base_url: str,
) -> dict[str, object]:
    return quota.request_json(
        "POST",
        f"{base_url.rstrip('/')}/v1/messages",
        {
            "Authorization": f"Bearer {client_key}",
            "anthropic-version": ANTHROPIC_VERSION,
        },
        {"model": ref.gateway_id, **body},
        timeout=PROBE_TIMEOUT_SECONDS,
    )


def _accepted(
    ref: ModelRef, client_key: str, body: Mapping[str, object], base_url: str
) -> tuple[bool, dict[str, object] | None, str]:
    try:
        return True, _messages(ref, client_key, body, base_url), ""
    except RequestError as err:
        return False, None, str(err)


def _user(content: object) -> list[dict[str, object]]:
    return [{"role": "user", "content": content}]


def probe_model(ref: ModelRef, client_key: str, base_url: str) -> ProbeResult:
    """对一个模型发四类极小请求，返回判定结果。"""
    answered, _reply, error = _accepted(
        ref,
        client_key,
        {"max_tokens": PROBE_MAX_TOKENS, "messages": _user("Reply with OK.")},
        base_url,
    )
    result = ProbeResult(ref=ref, answered=answered, answer_error=error)
    if not answered:
        return result
    ok, reply, _error = _accepted(
        ref,
        client_key,
        {
            "max_tokens": PROBE_MAX_TOKENS * 4,
            "messages": _user("Call the echo tool with text 'hi'."),
            "tools": [ECHO_TOOL],
            "tool_choice": {"type": "tool", "name": "echo"},
        },
        base_url,
    )
    content = reply.get("content") if reply is not None else None
    result.tools = (
        ok
        and isinstance(content, list)
        and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        )
    )
    for level in EFFORT_LEVELS:
        accepted, _reply, _error = _accepted(
            ref,
            client_key,
            {
                "max_tokens": EFFORT_MAX_TOKENS,
                "messages": _user("Reply with OK."),
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": level},
            },
            base_url,
        )
        result.efforts[level] = accepted
    result.image, _reply, _error = _accepted(
        ref,
        client_key,
        {
            "max_tokens": PROBE_MAX_TOKENS,
            "messages": _user(
                [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": PIXEL_PNG,
                        },
                    },
                    {"type": "text", "text": "Describe the image in one word."},
                ]
            ),
        },
        base_url,
    )
    return result


def probe_models(
    config: Config,
    name: str,
    model_ids: Sequence[str],
    *,
    base_url: str = gateway.GATEWAY_URL,
) -> list[ProbeResult]:
    """经网关探测来源 `name` 里列出的模型；不要求它们写了 context。

    调用前网关须已按当前配置在跑（启动器负责生成配置与拉起网关）。

    异常
    ----------
    ConfigError
        来源不存在或模型没有列在该来源的 `models` 里。
    GatewayError
        key 文件不可用或网关不可达。
    ProbeError
        来源是订阅来源（订阅来源只列出网关定义里的模型），或模型不在网关的模型列表里。
    """
    source_type, _base_url = _source(config, name)
    if source_type in SUBSCRIPTION_TYPES:
        raise ProbeError(
            f"来源 {name} 是 {source_type} 订阅来源，只列出网关定义里的模型，"
            f"不做能力探测；运行 claudex probe {name} 查看"
        )
    refs = [resolve_ref(config, f"{name}/{model_id}") for model_id in model_ids]
    secrets = gateway.load_secrets(config)
    registered = gateway.fetch_models(secrets.client_key, base_url=base_url)
    missing = sorted(ref.gateway_id for ref in refs if ref.gateway_id not in registered)
    if missing:
        raise ProbeError(
            f"{missing} 不在网关的模型列表里；运行 claudex gateway restart 后再试"
        )
    return [probe_model(ref, secrets.client_key, base_url) for ref in refs]


def _known_context(ref: ModelRef) -> int | None:
    if ref.context is not None:
        return ref.context
    cached = catalog.load_catalog()
    if cached is None or ref.openrouter is None:
        return None
    entry = cached.models.get(ref.openrouter)
    return entry.context_length if entry is not None else None


def format_result(result: ProbeResult) -> list[str]:
    """把探测结果整理成打印用的行，末行是建议写进配置的 context 与 efforts。"""
    ref = result.ref
    lines = [f"{ref.ref}（网关 id {ref.gateway_id}）"]
    if not result.answered:
        lines.append(f"  应答：失败（{result.answer_error}）")
        return lines
    lines.append("  应答：正常")
    lines.append(f"  工具调用：{'支持' if result.tools else '不支持'}")
    accepted = [level for level, ok in result.efforts.items() if ok]
    rejected = [level for level, ok in result.efforts.items() if not ok]
    lines.append(
        f"  档位：接受 {', '.join(accepted) or '无'}；"
        f"拒绝 {', '.join(rejected) or '无'}"
    )
    lines.append(f"  图片输入：{'接受' if result.image else '不接受'}")
    context = _known_context(ref)
    context_text = str(context) if context is not None else "<查上游文档后填写>"
    efforts_text = ", ".join(f'"{level}"' for level in accepted)
    lines.append(
        f'  建议：{{ id = "{ref.model_id}", context = {context_text}, '
        f"efforts = [{efforts_text}] }}"
    )
    return lines
