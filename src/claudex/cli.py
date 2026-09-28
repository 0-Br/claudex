"""claudex 的 Python 子命令：`python -P -m claudex.cli <子命令>`。

用户子命令由启动器 `bin/claudex` 转来；以下划线开头的内部子命令只给启动器用，按固定
次序生成网关配置、等待模型注册与渲染快照。退出码：0 成功，1 失败，2 用法错误。
"""

import argparse
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from claudex import (
    __version__,
    catalog,
    gateway,
    paths,
    probe,
    quota,
    render,
    statusline,
    upgrade,
)
from claudex.catalog import CatalogError
from claudex.config import (
    GENERIC_TYPES,
    SUBSCRIPTION_TYPES,
    TIERS,
    Config,
    ConfigError,
    load_config,
    resolve_ref,
)
from claudex.gateway import GatewayError
from claudex.probe import ProbeError
from claudex.quota import RequestError
from claudex.render import RenderError
from claudex.upgrade import RolloutError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
DIRECTORY_MODE = 0o700
KEY_MODE = 0o600
KEY_BYTES = 32
TEMPLATE_FILES = ("claudex.toml", "gateway.base.yaml", "settings.base.json")
LOGIN_FLAGS = {
    "codex": ("-codex-device-login",),
    "antigravity": ("-antigravity-login", "-no-browser"),
}
VERSION_TIMEOUT_SECONDS = 10
WAIT_HINT_TIMEOUT_SECONDS = 300
STATUS_PREFIX = "runtime      : "
PROBLEM_PREFIX = "problem: "
NOTE_PREFIX = "note: "
_VERSION_LINE = re.compile(r"Version:\s*([0-9]+\.[0-9]+\.[0-9]+)")
# 这些异常都带面向用户的消息，打印一行后以退出码 1 结束
_USER_ERRORS = (
    ConfigError,
    GatewayError,
    RenderError,
    CatalogError,
    RolloutError,
    ProbeError,
    RequestError,
    ValueError,
    OSError,
)
COMPLETION_TEMPLATE = "completion.bash"


def _print(line: str = "") -> None:
    sys.stdout.write(f"{line}\n")


def _error(message: str) -> None:
    sys.stderr.write(f"claudex: {message}\n")


def _config() -> Config:
    return load_config(paths.config_file())


# -----------------------------------------------------------------------------
# 网关配置、等待与快照（启动器按 spec 的次序依次调用）


def prepare_gateway_config(config: Config) -> bool:
    """按当前配置生成 `gateway.yaml`，返回是否改写了文件。

    通用来源的 context 与档位从 OpenRouter 目录求出（没有缓存时同步抓取一次），此时不
    需要网关在跑；订阅来源不写网关段。
    """
    metas = render.model_inputs(config, catalog.ensure_catalog(), {})
    contexts, efforts = render.gateway_inputs(metas)
    return gateway.prepare_gateway(config, contexts=contexts, efforts=efforts)


def generic_gateway_ids(config: Config) -> list[str]:
    """本次配置里全部通用来源模型在网关里的 id。"""
    return [
        resolve_ref(config, f"{source.name}/{model.id}").gateway_id
        for source in config.sources.values()
        if source.type in GENERIC_TYPES
        for model in source.models
    ]


def _cmd_gateway_config(_args: argparse.Namespace) -> int:
    _print("changed" if prepare_gateway_config(_config()) else "unchanged")
    return EXIT_OK


def _cmd_wait_models(_args: argparse.Namespace) -> int:
    config = _config()
    secrets_data = gateway.load_secrets(config)
    gateway.wait_for_models(generic_gateway_ids(config), secrets_data.client_key)
    return EXIT_OK


def _cmd_render(args: argparse.Namespace) -> int:
    config = _config()
    profile = args.profile or config.default_profile
    overrides = {
        tier: getattr(args, tier) for tier in TIERS if getattr(args, tier) is not None
    }
    snapshot = render.render(config, profile, overrides, fast=args.fast)
    _print(f"settings={snapshot.settings_file}")
    for pattern in config.mcp_deny:
        _print(f"mcp_deny={pattern}")
    if args.fast:
        notice = render.fast_notice(
            statusline.load_profile_snapshot(snapshot.profile_file)
        )
        if notice is not None:
            _print(f"notice={notice}")
    return EXIT_OK


def _cmd_complete(args: argparse.Namespace) -> int:
    try:
        config = _config()
    except (OSError, ConfigError):
        return EXIT_OK
    if args.kind == "profiles":
        names = list(config.profiles)
    elif args.kind == "sources":
        names = list(config.sources)
    elif args.kind == "generic-sources":
        names = [n for n, s in config.sources.items() if s.type in GENERIC_TYPES]
    else:
        names = [
            f"{source.name}/{model.id}"
            for source in config.sources.values()
            for model in source.models
        ]
    for name in names:
        _print(name)
    return EXIT_OK


# -----------------------------------------------------------------------------
# init 与 key


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _cmd_init(_args: argparse.Namespace) -> int:
    root = paths.config_dir()
    for directory in (root, paths.keys_dir()):
        directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(DIRECTORY_MODE)
    for key_file in (paths.client_key_file(), paths.management_key_file()):
        if key_file.exists():
            _print(f"{key_file} 已存在，不覆盖")
            continue
        _write_private(key_file, secrets.token_hex(KEY_BYTES) + "\n")
        _print(f"{key_file} 已生成（0600）")
    for name in TEMPLATE_FILES:
        target = root / name
        if target.exists():
            _print(f"{target} 已存在，不覆盖")
            continue
        text = (_templates() / name).read_text(encoding="utf-8")
        target.write_text(text, encoding="utf-8")
        _print(f"{target} 已写入起步文件")
    return EXIT_OK


def _read_key_input() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("key: ")
    return sys.stdin.readline()


def _cmd_key_set(args: argparse.Namespace) -> int:
    config = _config()
    source = config.sources.get(args.source)
    if source is None:
        raise ConfigError(
            f"来源 {args.source!r} 不在 claudex.toml 里，"
            f"已定义 {sorted(config.sources)}"
        )
    if source.type not in GENERIC_TYPES:
        raise ConfigError(
            f"来源 {args.source!r} 是 {source.type} 订阅来源，不用 key；"
            f"运行 claudex login {source.type} 登录"
        )
    key = _read_key_input().rstrip("\r\n")
    if not key or any(character.isspace() for character in key):
        raise ValueError("key 必须是一行不含空白的文本")
    keys_dir = paths.keys_dir()
    keys_dir.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    keys_dir.chmod(DIRECTORY_MODE)
    target = paths.source_key_file(args.source)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.new")
    temporary.unlink(missing_ok=True)
    _write_private(temporary, key + "\n")
    temporary.replace(target)
    _print(f"{target} 已写入（0600）")
    return EXIT_OK


# -----------------------------------------------------------------------------
# probe 与 login


def _cmd_probe(args: argparse.Namespace) -> int:
    config = _config()
    if not args.models:
        for model_id in probe.list_upstream_models(config, args.source):
            _print(model_id)
        return EXIT_OK
    for result in probe.probe_models(config, args.source, args.models):
        for line in probe.format_result(result):
            _print(line)
    return EXIT_OK


def gateway_environment() -> dict[str, str]:
    """拉起网关（或以登录模式运行网关）时的环境：`WRITABLE_PATH` 指向 state 目录，
    网关日志与失败快照因此落在 `<state>/logs`。"""
    return {**os.environ, "WRITABLE_PATH": str(paths.state_dir())}


def _cmd_login(args: argparse.Namespace) -> int:
    prepare_gateway_config(_config())
    binary = upgrade.default_gateway_link()
    if not binary.exists():
        raise RolloutError(upgrade.INSTALL_HINT.format(link=binary))
    state = paths.state_dir()
    state.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    argv = [
        str(binary),
        "-config",
        str(paths.gateway_config_file()),
        *LOGIN_FLAGS[args.channel],
    ]
    os.umask(0o077)
    os.chdir(state)
    os.execve(str(binary), argv, gateway_environment())


# -----------------------------------------------------------------------------
# profiles、status、preflight


def _cmd_profiles(_args: argparse.Namespace) -> int:
    config = _config()
    for name, profile in config.profiles.items():
        mark = "*" if name == config.default_profile else " "
        tiers = "  ".join(f"{tier}={getattr(profile, tier)}" for tier in TIERS)
        _print(f"{mark} {name}  {tiers}")
    return EXIT_OK


def installed_gateway_version(binary: Path) -> str | None:
    """网关二进制 `-h` 自报的版本；二进制不在或认不出时为 None。"""
    if not binary.exists():
        return None
    try:
        completed = subprocess.run(
            [str(binary), "-h"],
            check=False,
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (completed.stdout or completed.stderr).splitlines()
    match = _VERSION_LINE.search(lines[0]) if lines else None
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _gateway_process_line(binary: Path) -> str:
    pid_file = paths.gateway_pid_file()
    state: dict[str, object] = {"pid_file": str(pid_file), "symlink": str(binary)}
    try:
        pid, _executable = upgrade.verify_service_identity(
            state, binary.resolve(strict=True)
        )
    except (OSError, RolloutError):
        return "gateway process: not running"
    return f"gateway process: pid {pid}, {gateway.GATEWAY_URL}"


def _latest_snapshot(sessions: Path) -> Path | None:
    """修改时间最新的 profile 快照；目录不在或没有快照时为 None。

    另一个启动同时在清理过期快照，列目录与取修改时间之间被删掉的文件跳过。
    """
    if not sessions.is_dir():
        return None
    dated: list[tuple[float, Path]] = []
    for path in sessions.glob("*.profile.json"):
        try:
            dated.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
    return max(dated)[1] if dated else None


def _status_problems(config: Config, cached: catalog.Catalog) -> list[str]:
    """真问题里不查网关就能判断的一类：`openrouter` slug 不在目录缓存里。"""
    problems: list[str] = []
    for source in config.sources.values():
        for model in source.models:
            slug = model.openrouter
            if slug is None or slug in cached.models:
                continue
            reason = (
                "目录条目格式无法解析"
                if slug in cached.skipped
                else "不在 OpenRouter 目录里"
            )
            problems.append(
                f"{source.name}/{model.id} 的 openrouter slug {slug} {reason}"
            )
    return problems


def _cmd_status(_args: argparse.Namespace) -> int:
    lines = [f"claudex {__version__}"]
    problems: list[str] = []
    notes: list[str] = []
    try:
        config: Config | None = _config()
    except (OSError, ConfigError) as err:
        config = None
        problems.append(f"配置不可用：{err}")
    if config is not None:
        profiles = ", ".join(
            f"{name}{'*' if name == config.default_profile else ''}"
            for name in config.profiles
        )
        lines.append(f"config: {paths.config_file()} (profiles: {profiles})")
    binary = upgrade.default_gateway_link()
    installed = installed_gateway_version(binary)
    if binary.exists():
        lines.append(f"gateway binary: {binary} -> {binary.resolve()}")
    else:
        lines.append(f"gateway binary: {binary} missing (see README)")
    lines.append(f"gateway version: {installed or 'unknown'}")
    lines.append(_gateway_process_line(binary))
    latest = _latest_snapshot(paths.sessions_dir())
    lines.append(f"snapshot: {latest or 'none'}")
    try:
        quota_data = quota.load_quota()
    except ValueError as err:
        quota_data = None
        lines.append(f"quota cache: unreadable ({err})")
    sources = quota_data.get("sources") if quota_data is not None else None
    if isinstance(sources, dict):
        for name, record in sources.items():
            if not isinstance(record, dict):
                continue
            state = (
                "ok" if record.get("ok") is True else f"failed ({record.get('error')})"
            )
            lines.append(
                f"quota {name}: {state}, updated_at {record.get('updated_at')}"
            )
    try:
        cached = catalog.load_catalog()
    except ValueError as err:
        cached = None
        lines.append(f"catalog: unreadable ({err}); run claudex update")
    else:
        lines.append(
            "catalog: none (run claudex update)"
            if cached is None
            else f"catalog: fetched {cached.fetched_at.isoformat()} "
            f"({len(cached.models)} models)"
        )
    if config is not None and cached is not None:
        problems.extend(_status_problems(config, cached))
    release = quota.load_release_record()
    latest = release.get("version") if release is not None else None
    # 只提示同一大版本内的新版；新大版本未经核实，由 claudex update 说明
    if (
        isinstance(latest, str)
        and installed is not None
        and _version_tuple(latest) > _version_tuple(installed)
        and _version_tuple(latest)[0] == _version_tuple(installed)[0]
    ):
        notes.append(
            f"gateway {latest} is available (installed {installed}); "
            "run claudex upgrade"
        )
    for line in lines:
        _print(f"{STATUS_PREFIX}{line}")
    for problem in problems:
        _print(f"{PROBLEM_PREFIX}{problem}")
    for note in notes:
        _print(f"{NOTE_PREFIX}{note}")
    return EXIT_OK


def _format_human(report: render.Report) -> list[str]:
    lines: list[str] = []
    for label, items in (
        ("error", report.errors),
        ("warning", report.warnings),
        ("unavailable", report.unavailable),
    ):
        for item in items:
            target = f" {item.model}" if item.model else ""
            lines.append(f"{label} [{item.category}]{target}: {item.message}")
    lines.append(
        f"profile {report.profile}: {len(report.errors)} errors, "
        f"{len(report.warnings)} warnings, {len(report.unavailable)} unavailable"
        f"{'' if report.online_checks else ' (gateway not checked)'}"
    )
    return lines


def _cmd_preflight(args: argparse.Namespace) -> int:
    # profile 只取 --profile 与 default_profile，不读 CLAUDEX_PROFILE
    try:
        config = _config()
    except (OSError, ConfigError) as err:
        # 配置读不出时照样给出一份报告，JSON 消费方不必另外解析错误输出
        report = render.Report(profile=args.profile or "", online_checks=False)
        report.errors.append(render.Diagnostic("config", str(err)))
    else:
        report = render.preflight(
            config,
            profile=args.profile or config.default_profile,
            check_gateway=not args.no_proxy_check,
        )
    if args.format == "json":
        _print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        for line in _format_human(report):
            _print(line)
    return report.exit_code()


# -----------------------------------------------------------------------------
# update、upgrade、gateway restart


def _cmd_update(_args: argparse.Namespace) -> int:
    failures = 0
    try:
        fetched = catalog.fetch_catalog()
        catalog.save_catalog(fetched)
        _print(f"catalog: {len(fetched.models)} models, {len(fetched.skipped)} skipped")
    except CatalogError as err:
        failures += 1
        _error(f"目录刷新失败：{err}")
    installed = installed_gateway_version(upgrade.default_gateway_link())
    try:
        latest = quota.fetch_latest_release()
    except (RequestError, ValueError) as err:
        _error(f"查询网关新版失败：{err}")
        return EXIT_FAILED
    quota.write_release_record(latest, int(time.time()))
    _print(f"gateway: latest {latest}, installed {installed or 'unknown'}")
    if installed is not None and _version_tuple(latest) > _version_tuple(installed):
        major = _version_tuple(installed)[0]
        try:
            releases = upgrade.fetch_releases()
            changes = upgrade.parse_changes(releases, installed)
            same_major = upgrade.latest_in_major(releases, major)
        except (RolloutError, ValueError) as err:
            failures += 1
            _error(f"取变更摘要失败：{err}")
        else:
            for line in changes:
                _print(line)
            if _version_tuple(latest)[0] > major:
                _print(
                    f"gateway: {latest} is a new major version; claudex is verified "
                    f"against {upgrade.VERIFIED_GATEWAY_VERSION}, so upgrade across "
                    "major versions only with an explicit version after checking "
                    "compatibility"
                )
                _print(f"gateway: latest {major}.x is {same_major or 'not found'}")
    return EXIT_FAILED if failures else EXIT_OK


def _print_armed(result: dict[str, object], action: str) -> None:
    run_id = result["run_id"]
    _print(f"{action} armed: run_id={run_id}")
    _print(
        f"wait: claudex upgrade --wait {run_id} --timeout {WAIT_HINT_TIMEOUT_SECONDS}"
    )


def _wait(run_id: str, timeout: float) -> int:
    code = upgrade.wait_run(run_id, timeout)
    names = {
        upgrade.EXIT_HEALTHY: "healthy",
        upgrade.EXIT_ROLLED_BACK: "rolled_back",
        upgrade.EXIT_FAILED: "failed",
        upgrade.EXIT_TIMEOUT: "timeout",
    }
    _print(f"run {run_id}: {names.get(code, str(code))}")
    return EXIT_OK if code == upgrade.EXIT_HEALTHY else EXIT_FAILED


def _cmd_upgrade(args: argparse.Namespace) -> int:
    if args.wait is not None:
        if args.version is not None:
            raise ValueError("--wait 与 VERSION 不能同时给")
        return _wait(args.wait, args.timeout)
    version = args.version
    if version is None:
        # 不带版本只在已装网关的大版本内取最新；跨大版本必须显式写版本
        installed = installed_gateway_version(upgrade.default_gateway_link())
        if installed is None:
            raise RolloutError(
                "取不到已安装网关的版本；显式写版本升级：claudex upgrade X.Y.Z"
            )
        major = _version_tuple(installed)[0]
        version = upgrade.fetch_latest_in_major(major)
        if version == installed:
            _print(f"gateway {installed} is already the latest {major}.x release")
            return EXIT_OK
    _print_armed(
        upgrade.start_upgrade(version, delay_seconds=args.delay),
        f"upgrade to {version}",
    )
    return EXIT_OK


def _cmd_gateway(args: argparse.Namespace) -> int:
    if args.action != "restart":
        # start 与 stop 在启动器里实现，Python 侧只处理 restart
        _error(f"gateway {args.action} 由 claudex 启动器执行")
        return EXIT_USAGE
    _print_armed(upgrade.start_restart(delay_seconds=args.delay), "restart")
    return EXIT_OK


def _templates() -> Traversable:
    return resources.files("claudex") / "templates"


def _cmd_completion(args: argparse.Namespace) -> int:
    del args
    sys.stdout.write((_templates() / COMPLETION_TEMPLATE).read_text(encoding="utf-8"))
    return EXIT_OK


# -----------------------------------------------------------------------------
# 解析与入口


def build_parser() -> argparse.ArgumentParser:
    """全部子命令的解析器。"""
    parser = argparse.ArgumentParser(prog="claudex")
    # metavar 只列用户子命令；以下划线开头的内部子命令照常可调用，但不出现在帮助里
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{init,key,probe,login,profiles,status,preflight,update,upgrade,"
        "gateway,completion}",
    )

    sub.add_parser("init").set_defaults(handler=_cmd_init)
    key = sub.add_parser("key").add_subparsers(dest="key_command", required=True)
    key_set = key.add_parser("set")
    key_set.add_argument("source")
    key_set.set_defaults(handler=_cmd_key_set)
    probe_parser = sub.add_parser("probe")
    probe_parser.add_argument("source")
    probe_parser.add_argument("models", nargs="*")
    probe_parser.set_defaults(handler=_cmd_probe)
    login = sub.add_parser("login")
    login.add_argument("channel", choices=SUBSCRIPTION_TYPES)
    login.set_defaults(handler=_cmd_login)
    sub.add_parser("profiles").set_defaults(handler=_cmd_profiles)
    sub.add_parser("status").set_defaults(handler=_cmd_status)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--profile")
    preflight.add_argument("--format", choices=("json", "human"), default="human")
    preflight.add_argument("--no-proxy-check", action="store_true")
    preflight.set_defaults(handler=_cmd_preflight)
    sub.add_parser("update").set_defaults(handler=_cmd_update)
    upgrade_parser = sub.add_parser("upgrade")
    upgrade_parser.add_argument("version", nargs="?")
    upgrade_parser.add_argument("--wait", metavar="RUN_ID")
    upgrade_parser.add_argument(
        "--timeout", type=float, default=WAIT_HINT_TIMEOUT_SECONDS
    )
    upgrade_parser.add_argument(
        "--delay", type=float, default=upgrade.DEFAULT_DELAY_SECONDS
    )
    upgrade_parser.set_defaults(handler=_cmd_upgrade)
    gateway_parser = sub.add_parser("gateway")
    gateway_parser.add_argument("action", choices=("start", "stop", "restart"))
    gateway_parser.add_argument(
        "--delay", type=float, default=upgrade.DEFAULT_DELAY_SECONDS
    )
    gateway_parser.set_defaults(handler=_cmd_gateway)
    completion = sub.add_parser("completion")
    completion.add_argument("shell", choices=("bash",))
    completion.set_defaults(handler=_cmd_completion)

    sub.add_parser("_gateway-config").set_defaults(handler=_cmd_gateway_config)
    sub.add_parser("_wait-models").set_defaults(handler=_cmd_wait_models)
    render_parser = sub.add_parser("_render")
    render_parser.add_argument("--profile")
    for tier in TIERS:
        render_parser.add_argument(f"--{tier}", metavar="REF")
    render_parser.add_argument("--fast", action="store_true")
    render_parser.set_defaults(handler=_cmd_render)
    complete = sub.add_parser("_complete")
    complete.add_argument(
        "kind", choices=("profiles", "sources", "generic-sources", "refs")
    )
    complete.set_defaults(handler=_cmd_complete)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析并执行一个子命令，返回退出码。"""
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except _USER_ERRORS as err:
        _error(str(err))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
