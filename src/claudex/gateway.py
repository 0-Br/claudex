"""本机网关（CLIProxyAPI）的配置生成、写入与查询。

`gateway.yaml` 由 `gateway.base.yaml` 底稿加上 claudex 管理的部分生成：监听地址、
OAuth 凭据目录、下游 key、管理密码，以及按通用来源生成的 `openai-compatibility` 与
`claude-api-key` 段。写入按网关热加载的方式就地覆写同一个 inode。查询函数只访问本机
回环地址上的网关，不走环境代理；错误消息与异常链不携带任何 key。
"""

import contextlib
import copy
import http.client
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from claudex import paths
from claudex.config import GENERIC_TYPES, Config, ConfigError, Source, resolve_ref

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 8317
GATEWAY_URL = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT_SECONDS = 2
WAIT_ATTEMPTS = 20
WAIT_INTERVAL_SECONDS = 0.5
GATEWAY_FILE_MODE = 0o600
KEY_FILE_MODE = 0o600
# 底稿里由 claudex 生成、不许用户写的顶层键
FORBIDDEN_BASE_KEYS = (
    "openai-compatibility",
    "claude-api-key",
    "api-keys",
    "auth-dir",
    "host",
    "port",
)
REMOTE_MANAGEMENT_KEY = "remote-management"
SECRET_KEY = "secret-key"
# 各通用 type 在网关里的段名
_SECTION_BY_TYPE = {
    "openrouter": "openai-compatibility",
    "openai": "openai-compatibility",
    "anthropic": "claude-api-key",
}
_HEX64 = re.compile(r"[0-9a-f]{64}")
# 管理接口与模型列表都在本机回环上，显式不走环境代理
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class GatewayError(RuntimeError):
    """网关相关的运行错误：key 文件缺失或格式不对、网关不可达、应答形状不对、
    等不到模型注册。消息不含任何 key。"""


@dataclass(frozen=True)
class GatewaySecrets:
    """生成 `gateway.yaml` 所需的全部 key；repr 不输出任何字段。"""

    client_key: str = field(repr=False)
    management_key: str = field(repr=False)
    source_keys: dict[str, str] = field(repr=False)


def _read_key_file(path: Path, what: str, hint: str, *, hex64: bool) -> str:
    """读一个 key 文件：先查权限为 0600，再去掉结尾换行后校验格式。

    消息只含路径、期望与实际权限，不含文件内容。
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        raise GatewayError(f"{what} {path} 不存在；{hint}") from None
    except OSError as err:
        raise GatewayError(f"{what} {path} 读取失败：{err.strerror}") from None
    if mode != KEY_FILE_MODE:
        raise GatewayError(
            f"{what} {path} 的权限应为 {KEY_FILE_MODE:04o}，实际为 {mode:04o}；"
            f"运行 chmod 600 {path}"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise GatewayError(f"{what} {path} 不存在；{hint}") from None
    except UnicodeDecodeError:
        # 解码异常的文本会带出文件字节，不接进异常链
        raise GatewayError(f"{what} {path} 不是 UTF-8 文本；{hint}") from None
    except OSError as err:
        raise GatewayError(f"{what} {path} 读取失败：{err.strerror}") from None
    key = raw.rstrip("\r\n")
    if hex64:
        if _HEX64.fullmatch(key) is None:
            raise GatewayError(
                f"{what} {path} 格式不对：应为一行 64 位小写十六进制；{hint}"
            )
    elif not key or any(character.isspace() for character in key):
        raise GatewayError(f"{what} {path} 格式不对：应为一行不含空白的 key；{hint}")
    return key


def _generic_sources(config: Config) -> list[Source]:
    return [
        source for source in config.sources.values() if source.type in GENERIC_TYPES
    ]


def _check_base(base: Mapping[str, object]) -> None:
    for key in FORBIDDEN_BASE_KEYS:
        if key in base:
            raise ConfigError(
                f"gateway.base.yaml 不得包含顶层键 {key!r}：它由 claudex 按 "
                "claudex.toml 与 key 文件生成"
            )
    management = base.get(REMOTE_MANAGEMENT_KEY)
    if management is None:
        return
    if not isinstance(management, dict):
        raise ConfigError(
            f"gateway.base.yaml 的 {REMOTE_MANAGEMENT_KEY!r} 必须是映射，"
            f"收到 {type(management).__name__}"
        )
    if SECRET_KEY in management:
        raise ConfigError(
            f"gateway.base.yaml 不得包含 {REMOTE_MANAGEMENT_KEY}.{SECRET_KEY}："
            "管理密码取自 management.key"
        )


def _model_entries(
    config: Config,
    source: Source,
    contexts: Mapping[str, int],
    efforts: Mapping[str, tuple[str, ...]],
) -> list[dict[str, object]]:
    """一个通用来源的 `models` 列表：name 为上游 id，alias 为网关别名。

    `max-context-length` 取配置里的显式 context，否则取 `contexts[ref]`，都没有就不写；
    档位同样取显式 efforts，否则取 `efforts[ref]`。有档位的模型写 `thinking.levels`；
    无档位的模型不写 `thinking`：v7.3.20 下 `claude-api-key` 不写即剥掉 effort，
    `openai-compatibility` 不写按 low、medium、high 夹紧，空列表反而原样转发。
    """
    entries: list[dict[str, object]] = []
    for model in source.models:
        ref = resolve_ref(config, f"{source.name}/{model.id}")
        _, _, alias = ref.gateway_id.partition("/")
        entry: dict[str, object] = {"name": ref.model_id, "alias": alias}
        context = ref.context if ref.context is not None else contexts.get(ref.ref)
        if context is not None:
            entry["max-context-length"] = context
        levels = ref.efforts if ref.efforts is not None else efforts.get(ref.ref)
        if levels:
            entry["thinking"] = {"levels": list(levels)}
        entries.append(entry)
    return entries


def _source_section(
    config: Config,
    source: Source,
    api_key: str,
    contexts: Mapping[str, int],
    efforts: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    models = _model_entries(config, source, contexts, efforts)
    if source.type == "anthropic":
        return {
            "api-key": api_key,
            "prefix": source.name,
            "base-url": source.base_url,
            "models": models,
        }
    base_url = OPENROUTER_BASE_URL if source.type == "openrouter" else source.base_url
    return {
        "name": source.name,
        "prefix": source.name,
        "base-url": base_url,
        "api-key-entries": [{"api-key": api_key}],
        "models": models,
    }


def _dump_yaml(content: Mapping[str, object]) -> str:
    return yaml.safe_dump(
        dict(content), allow_unicode=True, sort_keys=False, width=10**9
    )


def _verify_yaml(path: Path, content: Mapping[str, object]) -> None:
    """按 YAML 回读临时文件，结果须与生成内容相等。"""
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        # YAML 异常的文本会引用出错行，而这个文件含全部 key，不接进异常链
        raise GatewayError(f"{path} 回读 YAML 失败（{type(err).__name__}）") from None
    if loaded != dict(content):
        raise GatewayError(f"{path} 回读结果与生成内容不一致")


def _overwrite_in_place(path: Path, data: bytes) -> None:
    """截断并重写已有文件，inode 不变；网关对配置文件的监视挂在这个 inode 上。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), GATEWAY_FILE_MODE)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _get_json(url: str, bearer: str) -> object:
    """带 Bearer 认证 GET 本机网关并解析 JSON；任何失败都抛 `GatewayError`。"""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    try:
        with _LOOPBACK_OPENER.open(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw: bytes = response.read()
    except urllib.error.HTTPError as err:
        raise GatewayError(f"网关 {url} 返回 HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise GatewayError(f"网关 {url} 请求失败：{err.reason}") from err
    except (TimeoutError, OSError, http.client.HTTPException) as err:
        raise GatewayError(f"网关 {url} 请求失败：{type(err).__name__}") from err
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise GatewayError(f"网关 {url} 的应答不是 JSON：{type(err).__name__}") from err


def load_secrets(config: Config) -> GatewaySecrets:
    """读取 client key、管理密码与各通用来源的上游 key。

    返回
    ----------
    GatewaySecrets
        `source_keys` 以来源名为键，只含通用来源；订阅来源没有 key 文件。

    异常
    ----------
    GatewayError
        任一文件不存在、读不了，或格式不对（client key 与管理密码须为一行 64 位小写
        十六进制，上游 key 须为一行不含空白的文本；结尾换行可有可无）。消息只含路径。
    """
    client_key = _read_key_file(
        paths.client_key_file(), "client key", "运行 claudex init 生成", hex64=True
    )
    management_key = _read_key_file(
        paths.management_key_file(), "管理密码", "运行 claudex init 生成", hex64=True
    )
    source_keys = {
        source.name: _read_key_file(
            paths.source_key_file(source.name),
            f"来源 {source.name} 的 key",
            f"运行 claudex key set {source.name} 写入",
            hex64=False,
        )
        for source in _generic_sources(config)
    }
    return GatewaySecrets(
        client_key=client_key, management_key=management_key, source_keys=source_keys
    )


def build_gateway_config(
    config: Config,
    base: Mapping[str, object],
    secrets: GatewaySecrets,
    *,
    contexts: Mapping[str, int] | None = None,
    efforts: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, object]:
    """由底稿、配置与 key 生成 `gateway.yaml` 的内容。

    参数
    ----------
    config : Config
        校验过的 `claudex.toml`。
    base : Mapping[str, object]
        `gateway.base.yaml` 的解析结果，不会被修改。
    secrets : GatewaySecrets
        `source_keys` 须覆盖全部通用来源。
    contexts : Mapping[str, int] | None
        模型引用（`<来源名>/<模型 id>`）到 context 的映射，用于配置里没有显式 context
        的通用来源模型；不属于通用来源的键不使用。
    efforts : Mapping[str, tuple[str, ...]] | None
        模型引用到档位的映射，键与用法同 `contexts`：配置里显式的 efforts 优先，
        映射里的非空档位写成 `thinking.levels`，空元组与缺失都不写 `thinking`。

    返回
    ----------
    dict[str, object]
        底稿的全部键按原顺序在前（`remote-management` 留在原位并并入 `secret-key`），
        其后是 `host`、`port`、`auth-dir`、`api-keys`、底稿没有时的
        `remote-management`，最后是有通用来源时才出现的 `openai-compatibility` 与
        `claude-api-key`。来源与模型按配置顺序排列。

    异常
    ----------
    ConfigError
        底稿含 `FORBIDDEN_BASE_KEYS` 之一或 `remote-management.secret-key`，或
        `remote-management` 不是映射；消息写明键名。
    GatewayError
        `secrets` 缺某个通用来源的 key。
    """
    _check_base(base)
    contexts = {} if contexts is None else contexts
    efforts = {} if efforts is None else efforts
    content: dict[str, object] = copy.deepcopy(dict(base))
    content["host"] = GATEWAY_HOST
    content["port"] = GATEWAY_PORT
    content["auth-dir"] = str(paths.auth_dir().absolute())
    content["api-keys"] = [secrets.client_key]
    management = base.get(REMOTE_MANAGEMENT_KEY)
    merged: dict[str, object] = (
        copy.deepcopy(management) if isinstance(management, dict) else {}
    )
    merged[SECRET_KEY] = secrets.management_key
    content[REMOTE_MANAGEMENT_KEY] = merged
    sections: dict[str, list[dict[str, object]]] = {}
    for source in _generic_sources(config):
        api_key = secrets.source_keys.get(source.name)
        if api_key is None:
            raise GatewayError(f"缺来源 {source.name} 的上游 key")
        section = _SECTION_BY_TYPE[source.type]
        sections.setdefault(section, []).append(
            _source_section(config, source, api_key, contexts, efforts)
        )
    for section in ("openai-compatibility", "claude-api-key"):
        if section in sections:
            content[section] = sections[section]
    return content


def write_gateway_config(content: Mapping[str, object], path: Path) -> bool:
    """把 `content` 以 YAML 写到 `path`，返回是否改写了文件。

    参数
    ----------
    content : Mapping[str, object]
        `build_gateway_config` 的结果；按 `yaml.safe_dump(allow_unicode=True,
        sort_keys=False)` 序列化，不折行。
    path : Path
        目标文件；父目录不存在时以 0700 创建。

    返回
    ----------
    bool
        现有文件与序列化结果逐字节相同时不重写、返回 False；否则写入并返回 True。

    说明
    ----------
    先在同目录临时文件里写全并按 YAML 回读核对，再截断并重写原文件（inode 不变）并
    fsync；原文件不存在时把临时文件改名为目标。网关按 inode 监视配置文件，换掉文件
    会撤掉监视，所以已有文件只做就地覆写。目标文件权限一律为 0600，内容不变时也会
    纠正权限。

    异常
    ----------
    GatewayError
        回读结果解析失败或与 `content` 不相等。
    """
    data = _dump_yaml(content).encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        current: bytes | None = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current == data:
        if stat.S_IMODE(path.stat().st_mode) != GATEWAY_FILE_MODE:
            path.chmod(GATEWAY_FILE_MODE)
        return False
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary: Path | None = Path(name)
    try:
        os.fchmod(descriptor, GATEWAY_FILE_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        assert temporary is not None
        _verify_yaml(temporary, content)
        if current is None:
            os.replace(temporary, path)
            temporary = None
        else:
            _overwrite_in_place(path, data)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    return True


def load_gateway_base(path: Path) -> dict[str, object]:
    """读取 `gateway.base.yaml`；空文件视为空映射。

    异常
    ----------
    ConfigError
        文件不存在、YAML 语法错误或顶层不是映射；消息含路径。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path} 不存在；运行 claudex init 生成起步文件") from None
    try:
        loaded: object = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"{path}: YAML 语法错误：{err}") from err
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: 顶层必须是映射，收到 {type(loaded).__name__}")
    return {str(key): value for key, value in loaded.items()}


def prepare_gateway(
    config: Config,
    *,
    contexts: Mapping[str, int] | None = None,
    efforts: Mapping[str, tuple[str, ...]] | None = None,
) -> bool:
    """读底稿与 key，生成并写入 `gateway.yaml`，返回是否改写了文件。

    `contexts` 与 `efforts` 的含义同 `build_gateway_config`。异常见
    `load_gateway_base`、`load_secrets`、`build_gateway_config` 与
    `write_gateway_config`。
    """
    base = load_gateway_base(paths.gateway_base_file())
    secrets = load_secrets(config)
    content = build_gateway_config(
        config, base, secrets, contexts=contexts, efforts=efforts
    )
    return write_gateway_config(content, paths.gateway_config_file())


def fetch_models(client_key: str, *, base_url: str = GATEWAY_URL) -> set[str]:
    """`GET /v1/models`，返回网关当前注册的模型 id 集合。

    异常
    ----------
    GatewayError
        网关不可达、超时（`REQUEST_TIMEOUT_SECONDS`）、非 2xx，或应答不是
        `{"data": [{"id": <字符串>}, …]}` 形状。
    """
    url = f"{base_url.rstrip('/')}/v1/models"
    payload = _get_json(url, client_key)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise GatewayError(f"网关 {url} 的应答缺 data 列表")
    models: set[str] = set()
    for index, entry in enumerate(data):
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(model_id, str):
            raise GatewayError(f"网关 {url} 的 data[{index}] 缺字符串 id")
        models.add(model_id)
    return models


def fetch_model_definitions(
    management_key: str, channel: str, *, base_url: str = GATEWAY_URL
) -> dict[str, object]:
    """`GET /v0/management/model-definitions/<channel>`，返回应答的 JSON 对象。

    参数
    ----------
    channel : str
        OAuth 通道名，即订阅来源的 type（`codex`、`antigravity`）。

    返回
    ----------
    dict[str, object]
        应答原样；各模型在 `models` 列表里，逐项含 `id`、`context_length` 与
        `thinking.levels` 等字段，由调用方解读。

    异常
    ----------
    GatewayError
        网关不可达、超时、非 2xx，或应答顶层不是 JSON 对象。
    """
    quoted = urllib.parse.quote(channel, safe="")
    url = f"{base_url.rstrip('/')}/v0/management/model-definitions/{quoted}"
    payload = _get_json(url, management_key)
    if not isinstance(payload, dict):
        raise GatewayError(f"网关 {url} 的应答顶层不是对象")
    return {str(key): value for key, value in payload.items()}


def wait_for_models(
    expected: Iterable[str],
    client_key: str,
    *,
    attempts: int = WAIT_ATTEMPTS,
    interval: float = WAIT_INTERVAL_SECONDS,
    base_url: str = GATEWAY_URL,
) -> None:
    """轮询 `fetch_models`，直到网关注册了 `expected` 里的全部模型 id。

    参数
    ----------
    expected : Iterable[str]
        要等的网关模型 id；为空时立即返回，不发请求。
    attempts : int
        最多查询次数，至少 1；两次查询之间等 `interval` 秒。

    说明
    ----------
    单次查询失败（网关正在重载、短暂不可达）按「还没注册」计入次数，最后一次失败
    的原因写进超时消息。

    异常
    ----------
    ValueError
        `attempts` 小于 1。
    GatewayError
        次数用完仍有模型缺席；消息列出缺的模型并提示 `claudex gateway restart`。
    """
    if attempts < 1:
        raise ValueError(f"attempts 至少为 1，收到 {attempts}")
    wanted = set(expected)
    if not wanted:
        return
    missing = wanted
    last_error: GatewayError | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(interval)
        try:
            missing = wanted - fetch_models(client_key, base_url=base_url)
        except GatewayError as err:
            last_error = err
            continue
        last_error = None
        if not missing:
            return
    detail = f"；最后一次查询失败：{last_error}" if last_error is not None else ""
    raise GatewayError(
        f"网关查询 {attempts} 次（间隔 {interval} 秒）后仍未注册 {sorted(missing)}"
        f"{detail}；运行 claudex gateway restart 让网关重新加载配置"
    )
