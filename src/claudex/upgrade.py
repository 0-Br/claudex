"""CLIProxyAPI 的受管升级与受管重启，以及发布变更摘要。

升级与重启都由父命令准备好 state 后，fork 出脱离当前会话的 worker 执行：核对旧服务
身份、调用启动器 `claudex gateway stop` 停旧、切换 `~/.local/bin/cli-proxy-api`
（升级时）、调用 `claudex gateway start` 起新（启动器负责端口与健康检查）、核对新服务
身份，任一步失败回退到旧二进制与旧服务。state 写在 `rollouts/<run_id>.json`（0600），
同一时刻只允许一次 rollout（`upgrade.lock`）。state 与日志不含任何凭据。
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import IO

from claudex import __version__, gateway, paths, quota
from claudex.config import ConfigError, load_config
from claudex.gateway import GatewayError

STATE_SCHEMA = 1
EXIT_HEALTHY = 0
EXIT_ROLLED_BACK = 10
EXIT_FAILED = 11
EXIT_TIMEOUT = 12
TERMINAL_EXIT_CODES = {
    "healthy": EXIT_HEALTHY,
    "rolled_back": EXIT_ROLLED_BACK,
    "failed": EXIT_FAILED,
}
WORKER_PID_STATUSES = {
    "armed",
    "stopping_old",
    "switched",
    "starting_new",
    "rolling_back",
    "healthy",
    "rolled_back",
    "failed",
}
SERVICE_IDENTITY_STATUSES = {"healthy", "rolled_back"}
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
DEFAULT_COMMAND_TIMEOUT = 30.0
DEFAULT_ARM_TIMEOUT = 10.0
DEFAULT_DELAY_SECONDS = 10.0
POLL_INTERVAL = 0.1
CANDIDATE_VERSION_TIMEOUT = 10
# 外网请求与 `quota.fetch_latest_release` 共用超时与 Accept
EXTERNAL_TIMEOUT_SECONDS = quota.RELEASE_TIMEOUT_SECONDS
GITHUB_ACCEPT = "application/vnd.github+json"
RELEASE_BASE_URL = "https://github.com/router-for-me/CLIProxyAPI/releases/download"
RELEASES_API_URL = "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases"
RELEASES_PAGE_SIZE = 100
MAX_CHANGE_POINTS = 10
# 配置生成、就地覆写与热加载前提按这个版本的网关源码核实；README 第 3 节同值
VERIFIED_GATEWAY_VERSION = "7.3.20"
GATEWAY_BINARY_NAME = "cli-proxy-api"
INSTALL_HINT = "网关二进制不在 {link}；首次安装方法见 README 的「安装」一节"
NOT_RUNNING_HINT = (
    "网关没有在运行或身份核对失败（{reason}）；先运行 claudex gateway start"
)
_USER_AGENT = f"claudex/{__version__}"
_CHANGE_POINT = re.compile(r"^\s*[-*]\s+(.+?)\s*$")

RunCommand = Callable[[list[str], float], int]
Sleep = Callable[[float], None]
ServiceVerifier = Callable[[dict[str, object], Path], tuple[int, str]]
GatewayCheck = Callable[[], None]


class RolloutError(RuntimeError):
    """升级或重启的准备、执行或状态协议错误。"""


# -----------------------------------------------------------------------------
# 位置


def default_launcher() -> Path:
    """缺省启动器：与本进程同一环境 scripts 目录下的 `claudex`。"""
    return Path(sysconfig.get_path("scripts")) / "claudex"


def default_install_root() -> Path:
    """网关版本目录根：`~/.local/lib/cliproxyapi`，每个版本一个 `X.Y.Z/` 子目录。"""
    return Path.home() / ".local" / "lib" / "cliproxyapi"


def default_gateway_link() -> Path:
    """网关的版本化 symlink：`~/.local/bin/cli-proxy-api`。

    位置固定，不受 `CLAUDEX_CONFIG_DIR`、`CLAUDEX_STATE_DIR` 影响。
    """
    return Path.home() / ".local" / "bin" / GATEWAY_BINARY_NAME


# -----------------------------------------------------------------------------
# 归档校验与安装


def parse_checksum_manifest(manifest: str, filename: str) -> str:
    """从官方 `checksums.txt` 读出指定资产的 SHA-256；必须恰好匹配一行。"""
    matches: list[str] = []
    for line in manifest.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1].lstrip("*") != filename:
            continue
        digest = parts[0].lower()
        if not SHA256_PATTERN.fullmatch(digest):
            raise RolloutError(f"checksum for {filename} is not SHA-256")
        matches.append(digest)
    if len(matches) != 1:
        raise RolloutError(
            f"checksums.txt matched {len(matches)} entries for {filename}, expected 1"
        )
    return matches[0]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive_checksum(path: Path, expected: str) -> None:
    """校验归档 SHA-256，不一致时拒绝继续。"""
    digest = _sha256_file(path)
    if digest != expected:
        raise RolloutError(
            f"archive checksum mismatch: expected {expected}, received {digest}"
        )


def _safe_member_path(destination: Path, name: str) -> Path:
    member_path = PurePosixPath(name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise RolloutError(f"archive member escapes destination: {name!r}")
    target = destination.joinpath(*member_path.parts)
    resolved_destination = destination.resolve()
    resolved_target = target.resolve(strict=False)
    if (
        resolved_target != resolved_destination
        and resolved_destination not in resolved_target.parents
    ):
        raise RolloutError(f"archive member escapes destination: {name!r}")
    return target


def extract_release_archive(archive: Path, destination: Path) -> Path:
    """安全解包 release 归档，返回根目录下的网关二进制。

    拒绝绝对路径、`..`、逃出目标目录的成员，以及 symlink、硬链接、设备与其他非普通
    文件成员；`destination` 必须还不存在。
    """
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary: Path | None = None
    try:
        with tarfile.open(archive, "r:gz") as stream:
            for member in stream.getmembers():
                if member.issym() or member.islnk() or member.isdev():
                    raise RolloutError(
                        f"archive member type is not allowed: {member.name!r}"
                    )
                target = _safe_member_path(destination, member.name)
                if member.isdir():
                    target.mkdir(mode=member.mode or 0o755, parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise RolloutError(
                        f"archive member type is not regular: {member.name!r}"
                    )
                source = stream.extractfile(member)
                if source is None:
                    raise RolloutError(f"archive member unreadable: {member.name!r}")
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with target.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                target.chmod(member.mode & 0o777)
                if member.name == GATEWAY_BINARY_NAME:
                    binary = target
    except (OSError, tarfile.TarError) as err:
        raise RolloutError(f"archive extraction failed ({type(err).__name__})") from err
    if binary is None or not binary.is_file():
        raise RolloutError("archive has no root cli-proxy-api binary")
    binary.chmod(binary.stat().st_mode | 0o100)
    return binary


def _fetch_bytes(url: str, accept: str | None = None) -> bytes:
    """GET 外网资源（经环境代理，超时 `EXTERNAL_TIMEOUT_SECONDS`）。"""
    headers = {"User-Agent": _USER_AGENT}
    if accept is not None:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener().open(
            request, timeout=EXTERNAL_TIMEOUT_SECONDS
        ) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as err:
        raise RolloutError(f"download failed ({type(err).__name__})") from err


def _candidate_version(binary: Path) -> str:
    try:
        completed = subprocess.run(
            [str(binary), "-h"],
            check=False,
            capture_output=True,
            text=True,
            timeout=CANDIDATE_VERSION_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise RolloutError(
            f"candidate version check failed ({type(err).__name__})"
        ) from err
    first_line = (completed.stdout or completed.stderr).splitlines()
    if completed.returncode != 0 or not first_line:
        raise RolloutError("candidate -h did not return a version line")
    match = re.search(r"Version:\s*([0-9]+\.[0-9]+\.[0-9]+)", first_line[0])
    if match is None:
        raise RolloutError("candidate version line is unrecognized")
    return match.group(1)


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def _write_install_manifest(
    directory: Path,
    *,
    version: str,
    asset: str,
    archive_sha256: str,
    binary_sha256: str,
) -> None:
    """在版本目录里记下可重验的官方归档与二进制身份。"""
    _write_private_json(
        directory / "install-manifest.json",
        {
            "schema": 1,
            "version": version,
            "asset": asset,
            "archive_sha256": archive_sha256,
            "binary_sha256": binary_sha256,
        },
    )


def prepare_release(version: str, install_root: Path) -> Path:
    """下载并按官方 `checksums.txt` 校验归档，装进版本目录，返回候选二进制。

    每次都重新下载校验；版本目录已存在时要求其中二进制的 hash 与官方二进制一致。
    候选二进制的自报版本必须等于 `version`。
    """
    if not VERSION_PATTERN.fullmatch(version):
        raise RolloutError(f"version must be X.Y.Z, got {version!r}")
    install_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    final_directory = install_root / version
    final_binary = final_directory / GATEWAY_BINARY_NAME
    asset = f"CLIProxyAPI_{version}_linux_amd64.tar.gz"
    release_url = f"{RELEASE_BASE_URL}/v{version}"
    manifest = _fetch_bytes(f"{release_url}/checksums.txt").decode("utf-8")
    archive_bytes = _fetch_bytes(f"{release_url}/{asset}")
    expected_archive_sha256 = parse_checksum_manifest(manifest, asset)
    with tempfile.TemporaryDirectory(
        dir=install_root, prefix=f".staging-{version}."
    ) as staging_raw:
        staging = Path(staging_raw)
        archive = staging / asset
        archive.write_bytes(archive_bytes)
        archive.chmod(0o600)
        verify_archive_checksum(archive, expected_archive_sha256)
        extracted = staging / "extracted"
        official_binary = extract_release_archive(archive, extracted)
        if official_binary != extracted / GATEWAY_BINARY_NAME:
            raise RolloutError("candidate binary is not at archive root")
        if _candidate_version(official_binary) != version:
            raise RolloutError("candidate self-version does not match target")
        official_binary_sha256 = _sha256_file(official_binary)
        if final_directory.exists():
            if (
                not final_binary.is_file()
                or _candidate_version(final_binary) != version
            ):
                raise RolloutError(f"existing version directory {version} is invalid")
            if _sha256_file(final_binary) != official_binary_sha256:
                raise RolloutError(
                    f"existing version {version} binary does not match official asset"
                )
            _write_install_manifest(
                final_directory,
                version=version,
                asset=asset,
                archive_sha256=expected_archive_sha256,
                binary_sha256=official_binary_sha256,
            )
            return final_binary
        _write_install_manifest(
            extracted,
            version=version,
            asset=asset,
            archive_sha256=expected_archive_sha256,
            binary_sha256=official_binary_sha256,
        )
        os.replace(extracted, final_directory)
    return final_binary


# -----------------------------------------------------------------------------
# state 与锁


def write_state(path: Path, state: dict[str, object]) -> None:
    """以 0600 原子写入 rollout state，并盖上 `schema` 与 `updated_at`。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = dict(state)
    payload["schema"] = STATE_SCHEMA
    payload["updated_at"] = int(time.time())
    _write_private_json(path, payload)


def read_state(path: Path) -> dict[str, object]:
    """读取并校验 rollout state 的基本形状。

    异常
    ----------
    RolloutError
        读不出、schema 不对、缺 `run_id` 或 `status`、文件名与 `run_id` 不一致、worker
        阶段缺正整数 `worker_pid`，或终态缺服务身份。
    """
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as err:
        raise RolloutError(f"rollout state unreadable ({type(err).__name__})") from err
    if not isinstance(parsed, dict) or parsed.get("schema") != STATE_SCHEMA:
        raise RolloutError("rollout state schema is invalid")
    run_id = parsed.get("run_id")
    status = parsed.get("status")
    if not isinstance(run_id, str) or not isinstance(status, str):
        raise RolloutError("rollout state lacks run_id or status")
    if path.stem != run_id:
        raise RolloutError(
            f"rollout state filename {path.stem!r} does not match run id {run_id!r}"
        )
    worker_pid = parsed.get("worker_pid")
    if status in WORKER_PID_STATUSES and (
        isinstance(worker_pid, bool)
        or not isinstance(worker_pid, int)
        or worker_pid <= 0
    ):
        raise RolloutError(
            f"rollout state {status} requires a positive worker_pid, got {worker_pid!r}"
        )
    if status in SERVICE_IDENTITY_STATUSES:
        service_pid = parsed.get("service_pid")
        executable = parsed.get("service_executable")
        if (
            isinstance(service_pid, bool)
            or not isinstance(service_pid, int)
            or service_pid <= 0
            or not isinstance(executable, str)
            or not executable
        ):
            raise RolloutError(
                f"rollout state {status} requires service identity, "
                f"got pid={service_pid!r}, executable={executable!r}"
            )
    return dict(parsed)


def update_state(
    path: Path,
    *,
    status: str | None = None,
    reason: str | None = None,
    **fields: object,
) -> dict[str, object]:
    """读取、更新并原子写回 rollout state。"""
    state = read_state(path)
    if status is not None:
        state["status"] = status
    if reason is not None:
        state["reason"] = reason
    state.update(fields)
    write_state(path, state)
    return state


def acquire_lock(path: Path) -> IO[str]:
    """取得非阻塞排他 flock；已有 rollout 在跑时抛 `RolloutError`。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = path.open("a+", encoding="utf-8")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as err:
        stream.close()
        raise RolloutError("another gateway rollout is already running") from err
    return stream


def state_path_for(run_id: str) -> Path:
    """run id 对应的 state 文件 `rollouts/<run_id>.json`；run id 须为 32 位十六进制。"""
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise RolloutError(f"run id is invalid: {run_id!r}")
    return paths.rollouts_dir() / f"{run_id}.json"


# -----------------------------------------------------------------------------
# worker


def atomic_symlink(target: Path, link: Path) -> None:
    """用临时 symlink 加 `os.replace()` 原子切换活动二进制。"""
    link.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{os.getpid()}.new")
    try:
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def _default_run_command(command: list[str], timeout: float) -> int:
    return subprocess.run(command, check=False, timeout=timeout).returncode


def _target_from_state(state: dict[str, object], key: str) -> Path:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        raise RolloutError(f"rollout state lacks {key}")
    return Path(value)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def verify_service_identity(
    state: dict[str, object], expected_target: Path
) -> tuple[int, str]:
    """核对 PID 文件里的进程活着，且它的 executable、活动 symlink 都指向期望 target。

    返回
    ----------
    tuple[int, str]
        (服务 PID, executable 的解析路径)。

    异常
    ----------
    RolloutError
        PID 文件无效、进程不在、路径读不出或三者不一致。
    """
    pid_file = _target_from_state(state, "pid_file")
    link = _target_from_state(state, "symlink")
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError) as err:
        raise RolloutError(
            f"service pid file is invalid ({type(err).__name__})"
        ) from err
    if pid <= 0 or not _process_alive(pid):
        raise RolloutError(f"service pid is not alive: {pid}")
    try:
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        resolved_target = expected_target.resolve(strict=True)
        resolved_link = link.resolve(strict=True)
    except OSError as err:
        raise RolloutError(
            f"service executable identity is unreadable ({type(err).__name__})"
        ) from err
    if executable != resolved_target or resolved_link != resolved_target:
        raise RolloutError(
            f"service identity mismatch: pid executable={executable}, "
            f"link={resolved_link}, expected={resolved_target}"
        )
    return pid, str(executable)


def _warn(message: str) -> None:
    print(f"gateway worker: {message}", file=sys.stderr)


def _prune_version_directory(candidate: Path, keep: set[Path]) -> None:
    """删掉一个不在保留集合里的 `X.Y.Z` 版本目录；一切失败只告警。

    筛选与删除同在一个 try 里：`is_symlink()`、`is_dir()`、`resolve()` 都会访问文件
    系统，逃出去的 OSError 会落进回滚分支、推翻已经写下的 healthy。
    """
    try:
        if not VERSION_PATTERN.fullmatch(candidate.name) or candidate.is_symlink():
            return
        if not candidate.is_dir() or candidate.resolve() in keep:
            return
        shutil.rmtree(candidate)
    except OSError as err:
        _warn(f"stale version {candidate.name} not pruned ({type(err).__name__})")


def _prune_stale_version_directories(state: dict[str, object]) -> None:
    """升级到 healthy 后删除 install root 下不再被引用的版本目录。

    只在升级模式、且新旧 target 不同时工作；保留新旧 target 与活动 symlink 各自所在
    的版本目录。此处已是 healthy 终态，state 结构错与文件系统失败都只告警。
    """
    if state.get("mode") != "upgrade":
        return
    try:
        new_target = _target_from_state(state, "new_target").resolve()
        old_target = _target_from_state(state, "old_target").resolve()
        link_directory = (
            _target_from_state(state, "symlink").resolve(strict=False).parent.resolve()
        )
    except (OSError, RolloutError) as err:
        _warn(f"stale versions not pruned ({type(err).__name__})")
        return
    if new_target == old_target:
        return
    keep = {new_target.parent, old_target.parent, link_directory}
    try:
        candidates = sorted(new_target.parent.parent.iterdir())
    except OSError as err:
        _warn(f"version directories not listed ({type(err).__name__})")
        return
    for candidate in candidates:
        _prune_version_directory(candidate, keep)


def _gateway_command(launcher: Path, action: str) -> list[str]:
    return [str(launcher), "gateway", action]


def _roll_back_after_exception(
    state_path: Path,
    state: dict[str, object],
    *,
    reason: str,
    switched: bool,
    run_command: RunCommand,
    verify_service: ServiceVerifier,
    timeout: float,
) -> None:
    launcher = _target_from_state(state, "launcher")
    old_target = _target_from_state(state, "old_target")
    link = _target_from_state(state, "symlink")
    try:
        update_state(state_path, status="rolling_back", reason=reason)
        if switched:
            run_command(_gateway_command(launcher, "stop"), timeout)
        atomic_symlink(old_target, link)
        if run_command(_gateway_command(launcher, "start"), timeout) == 0:
            service_pid, executable = verify_service(state, old_target)
            update_state(
                state_path,
                status="rolled_back",
                reason=reason,
                service_pid=service_pid,
                service_executable=executable,
            )
            return
    except Exception as err:
        # 回退途中再出错也只记类名：异常文本可能带路径以外的内容，不进日志
        _warn(f"rollback after exception failed ({type(err).__name__})")
    update_state(state_path, status="failed", reason=reason)


def run_worker(
    state_path: Path,
    *,
    run_command: RunCommand = _default_run_command,
    sleep: Sleep = time.sleep,
    verify_service: ServiceVerifier = verify_service_identity,
) -> None:
    """执行一次 rollout：核对旧服务、停旧、切换、起新、核对新服务，失败即回退。

    停旧与起新都是调用启动器：`<launcher> gateway stop` 与 `<launcher> gateway start`。
    旧服务停下之后逃出的任何 `Exception` 都会被捕获：状态落 `rolling_back`，恢复旧
    target 与旧服务，成功为 `rolled_back`，否则为 `failed`，原因记为
    `worker_exception_<异常类名>`，日志只记类名。
    """
    state = read_state(state_path)
    if state["status"] != "armed":
        state = update_state(state_path, status="armed", reason="")
    delay = state.get("delay_seconds", 0)
    if isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay > 0:
        sleep(float(delay))
    launcher = _target_from_state(state, "launcher")
    old_target = _target_from_state(state, "old_target")
    new_target = _target_from_state(state, "new_target")
    link = _target_from_state(state, "symlink")
    timeout_raw = state.get("command_timeout", DEFAULT_COMMAND_TIMEOUT)
    timeout = (
        float(timeout_raw)
        if isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool)
        else DEFAULT_COMMAND_TIMEOUT
    )
    try:
        verify_service(state, old_target)
    except RolloutError:
        update_state(state_path, status="failed", reason="old_identity_failed")
        return
    old_stopped = False
    switched = False
    try:
        update_state(state_path, status="stopping_old", reason="")
        if run_command(_gateway_command(launcher, "stop"), timeout) != 0:
            update_state(state_path, status="failed", reason="stop_failed")
            return
        old_stopped = True
        atomic_symlink(new_target, link)
        switched = True
        update_state(state_path, status="switched", reason="")
        update_state(state_path, status="starting_new", reason="")
        rollback_reason = "new_start_failed"
        if run_command(_gateway_command(launcher, "start"), timeout) == 0:
            try:
                service_pid, executable = verify_service(state, new_target)
            except RolloutError:
                rollback_reason = "new_identity_failed"
            else:
                update_state(
                    state_path,
                    status="healthy",
                    reason="",
                    service_pid=service_pid,
                    service_executable=executable,
                )
                _prune_stale_version_directories(state)
                return
        update_state(state_path, status="rolling_back", reason=rollback_reason)
        run_command(_gateway_command(launcher, "stop"), timeout)
        atomic_symlink(old_target, link)
        switched = False
        if run_command(_gateway_command(launcher, "start"), timeout) != 0:
            update_state(state_path, status="failed", reason="rollback_start_failed")
            return
        try:
            service_pid, executable = verify_service(state, old_target)
        except RolloutError:
            update_state(state_path, status="failed", reason="rollback_identity_failed")
            return
        update_state(
            state_path,
            status="rolled_back",
            reason=rollback_reason,
            service_pid=service_pid,
            service_executable=executable,
        )
    except Exception as err:
        # 兜底覆盖一切异常：停旧之后网关不能停在中间态。异常文本可能带路径以外的
        # 内容（如下游命令的输出），日志与 state 只记类名
        reason = f"worker_exception_{type(err).__name__}"
        _warn(reason)
        if not old_stopped:
            update_state(state_path, status="failed", reason=reason)
            return
        _roll_back_after_exception(
            state_path,
            state,
            reason=reason,
            switched=switched,
            run_command=run_command,
            verify_service=verify_service,
            timeout=timeout,
        )


def wait_for_terminal(state_path: Path, timeout: float) -> int:
    """等 rollout 到终态并返回退出码；worker 提前退出时把 state 标为 failed。

    返回
    ----------
    int
        `EXIT_HEALTHY`、`EXIT_ROLLED_BACK`、`EXIT_FAILED`，超时为 `EXIT_TIMEOUT`。
    """
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        state = read_state(state_path)
        status = str(state["status"])
        if status in TERMINAL_EXIT_CODES:
            return TERMINAL_EXIT_CODES[status]
        if time.monotonic() >= deadline:
            return EXIT_TIMEOUT
        worker_pid = state.get("worker_pid")
        if (
            isinstance(worker_pid, int)
            and not isinstance(worker_pid, bool)
            and worker_pid > 0
            and not _process_alive(worker_pid)
        ):
            update_state(
                state_path, status="failed", reason="worker_exited_before_terminal"
            )
            return EXIT_FAILED
        time.sleep(POLL_INTERVAL)


# -----------------------------------------------------------------------------
# 启动 rollout


def _gateway_answers() -> None:
    """确认本机网关应答 `/v1/models`（超时 2 秒）。

    异常
    ----------
    RolloutError
        配置或 key 文件不可用，或网关没有应答。
    """
    try:
        secrets = gateway.load_secrets(load_config(paths.config_file()))
        gateway.fetch_models(secrets.client_key)
    except (OSError, ConfigError, GatewayError) as err:
        raise RolloutError(str(err)) from err


def _start_rollout(
    *,
    mode: str,
    version: str | None,
    delay_seconds: float,
    launcher: Path,
    install_root: Path,
    gateway_link: Path,
    gateway_check: GatewayCheck,
    verify_service: ServiceVerifier,
) -> dict[str, object]:
    lock = acquire_lock(paths.upgrade_lock_file())
    try:
        if not gateway_link.is_symlink() and not gateway_link.exists():
            raise RolloutError(INSTALL_HINT.format(link=gateway_link))
        old_target = gateway_link.resolve(strict=True)
        pid_file = paths.gateway_pid_file()
        precheck: dict[str, object] = {
            "pid_file": str(pid_file),
            "symlink": str(gateway_link),
        }
        try:
            verify_service(precheck, old_target)
            gateway_check()
        except RolloutError as err:
            raise RolloutError(NOT_RUNNING_HINT.format(reason=err)) from err
        new_target = (
            prepare_release(str(version), install_root)
            if mode == "upgrade"
            else old_target
        )
        run_id = uuid.uuid4().hex
        state_path = state_path_for(run_id)
        log_path = state_path.with_suffix(".log")
        write_state(
            state_path,
            {
                "run_id": run_id,
                "mode": mode,
                "version": version,
                "status": "staged",
                "old_target": str(old_target),
                "new_target": str(new_target),
                "symlink": str(gateway_link),
                "launcher": str(launcher),
                "pid_file": str(pid_file),
                "worker_pid": 0,
                "started_at": int(time.time()),
                "reason": "",
                "delay_seconds": delay_seconds,
                "command_timeout": DEFAULT_COMMAND_TIMEOUT,
                "log_path": str(log_path),
            },
        )
        command = [
            sys.executable,
            "-P",
            "-m",
            "claudex.upgrade",
            "_detach",
            "--state",
            str(state_path),
            "--lock-fd",
            str(lock.fileno()),
        ]
        with log_path.open("a", encoding="utf-8") as log_stream:
            os.chmod(log_path, 0o600)
            detached = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                pass_fds=(lock.fileno(),),
                timeout=DEFAULT_ARM_TIMEOUT,
            )
        if detached.returncode != 0:
            raise RolloutError("detacher failed before worker launch")
        deadline = time.monotonic() + DEFAULT_ARM_TIMEOUT
        while time.monotonic() < deadline:
            state = read_state(state_path)
            worker_pid = state.get("worker_pid")
            if (
                state["status"] == "armed"
                and isinstance(worker_pid, int)
                and worker_pid > 0
            ):
                return {
                    "run_id": run_id,
                    "state_path": str(state_path),
                    "worker_pid": worker_pid,
                    "status": "armed",
                }
            if state["status"] in TERMINAL_EXIT_CODES:
                raise RolloutError(
                    f"worker reached {state['status']} before parent observed armed"
                )
            if (
                isinstance(worker_pid, int)
                and worker_pid > 0
                and not _process_alive(worker_pid)
            ):
                raise RolloutError("worker exited before armed")
            time.sleep(POLL_INTERVAL)
        raise RolloutError("worker did not reach armed before timeout")
    finally:
        lock.close()


def start_upgrade(
    version: str,
    *,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    launcher: Path | None = None,
    install_root: Path | None = None,
    gateway_link: Path | None = None,
    gateway_check: GatewayCheck = _gateway_answers,
    verify_service: ServiceVerifier = verify_service_identity,
) -> dict[str, object]:
    """开始一次受管升级：下载校验 `version`，布防 worker 后返回。

    参数
    ----------
    version : str
        目标版本 `X.Y.Z`。
    delay_seconds : float
        worker 布防后等多久再停旧，给调用方留出退出当前命令的时间。
    launcher, install_root, gateway_link : Path | None
        缺省为 `default_launcher()`、`default_install_root()`、
        `default_gateway_link()`。
    gateway_check, verify_service
        布防前核对网关正在运行的两步，测试可注入。

    返回
    ----------
    dict[str, object]
        `{"run_id", "state_path", "worker_pid", "status": "armed"}`。

    异常
    ----------
    RolloutError
        已有 rollout 在跑；网关二进制不在固定位置（提示首装方法见 README）；网关没有
        在运行（提示先 `claudex gateway start`）；下载或校验失败；worker 没能布防。
    """
    return _start_rollout(
        mode="upgrade",
        version=version,
        delay_seconds=delay_seconds,
        launcher=launcher or default_launcher(),
        install_root=install_root or default_install_root(),
        gateway_link=gateway_link or default_gateway_link(),
        gateway_check=gateway_check,
        verify_service=verify_service,
    )


def start_restart(
    *,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    launcher: Path | None = None,
    gateway_link: Path | None = None,
    gateway_check: GatewayCheck = _gateway_answers,
    verify_service: ServiceVerifier = verify_service_identity,
) -> dict[str, object]:
    """开始一次受管重启：不换二进制，停旧起新走与升级相同的 worker 与回退。

    参数、返回与异常同 `start_upgrade`，没有下载与校验。
    """
    return _start_rollout(
        mode="restart",
        version=None,
        delay_seconds=delay_seconds,
        launcher=launcher or default_launcher(),
        install_root=default_install_root(),
        gateway_link=gateway_link or default_gateway_link(),
        gateway_check=gateway_check,
        verify_service=verify_service,
    )


def wait_run(run_id: str, timeout: float) -> int:
    """等 run id 对应的 rollout 到终态，返回退出码（见 `wait_for_terminal`）。

    异常
    ----------
    RolloutError
        run id 形态不对，或 state 读不出、形状不对。
    """
    return wait_for_terminal(state_path_for(run_id), timeout)


# -----------------------------------------------------------------------------
# 变更摘要


def _version_key(tag: str) -> tuple[int, int, int] | None:
    match = VERSION_PATTERN.fullmatch(tag.removeprefix("v"))
    if match is None:
        return None
    major, minor, patch = (int(part) for part in tag.removeprefix("v").split("."))
    return major, minor, patch


def parse_changes(releases: object, version: str) -> list[str]:
    """从 releases 列表里取 `version` 之后的正式发布，整理成摘要行。

    返回
    ----------
    list[str]
        按版本从新到旧，每个发布先一行 `v<X.Y.Z> <标题>`，再跟最多
        `MAX_CHANGE_POINTS` 行 `  - <要点>`（取正文里以 `-` 或 `*` 开头的行）。
        草稿、预发布与 tag 不是 `X.Y.Z` 的发布跳过。

    异常
    ----------
    ValueError
        `version` 不是 `X.Y.Z`，或 `releases` 不是列表。
    """
    current = _version_key(version)
    if current is None:
        raise ValueError(f"version must be X.Y.Z, got {version!r}")
    if not isinstance(releases, list):
        raise ValueError(f"releases 应答必须是列表，收到 {type(releases).__name__}")
    newer: list[tuple[tuple[int, int, int], dict[str, object]]] = []
    for release in releases:
        if (
            not isinstance(release, dict)
            or release.get("draft")
            or release.get("prerelease")
        ):
            continue
        tag = release.get("tag_name")
        key = _version_key(tag) if isinstance(tag, str) else None
        if key is not None and key > current:
            newer.append((key, release))
    lines: list[str] = []
    for key, release in sorted(newer, key=lambda item: item[0], reverse=True):
        name = release.get("name")
        title = name if isinstance(name, str) and name.strip() else ""
        lines.append(f"v{'.'.join(str(part) for part in key)} {title}".rstrip())
        body = release.get("body")
        points = [
            match.group(1)
            for line in (body.splitlines() if isinstance(body, str) else [])
            if (match := _CHANGE_POINT.match(line)) is not None
        ]
        lines.extend(f"  - {point}" for point in points[:MAX_CHANGE_POINTS])
    return lines


def latest_in_major(releases: object, major: int) -> str | None:
    """releases 列表里大版本号为 `major` 的最新正式发布 `X.Y.Z`；没有时为 None。

    草稿、预发布与 tag 不是 `X.Y.Z` 的发布跳过。

    异常
    ----------
    ValueError
        `releases` 不是列表。
    """
    if not isinstance(releases, list):
        raise ValueError(f"releases 应答必须是列表，收到 {type(releases).__name__}")
    keys = [
        key
        for release in releases
        if isinstance(release, dict)
        and not release.get("draft")
        and not release.get("prerelease")
        and isinstance(tag := release.get("tag_name"), str)
        and (key := _version_key(tag)) is not None
        and key[0] == major
    ]
    return ".".join(str(part) for part in max(keys)) if keys else None


def fetch_releases() -> object:
    """最近 `RELEASES_PAGE_SIZE` 个发布（GitHub releases API，经环境代理，超时 10 秒）。

    异常
    ----------
    RolloutError
        请求失败。
    ValueError
        应答不是 JSON。
    """
    raw = _fetch_bytes(
        f"{RELEASES_API_URL}?per_page={RELEASES_PAGE_SIZE}", GITHUB_ACCEPT
    )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"releases 应答不是 JSON：{type(err).__name__}") from err


def fetch_latest_in_major(major: int) -> str:
    """大版本号为 `major` 的最新正式发布，供不带版本的 `claudex upgrade` 取目标。

    异常
    ----------
    RolloutError
        请求失败，或最近的发布里没有这个大版本的正式发布。
    ValueError
        应答不是 JSON 列表。
    """
    version = latest_in_major(fetch_releases(), major)
    if version is None:
        raise RolloutError(
            f"最近 {RELEASES_PAGE_SIZE} 个发布里没有 {major}.x 的正式发布；"
            "显式写版本升级：claudex upgrade X.Y.Z"
        )
    return version


def changes_since(version: str) -> list[str]:
    """本机版本之后各发布的标题与要点，取自 `fetch_releases`。

    返回形态见 `parse_changes`。

    异常
    ----------
    RolloutError
        请求失败。
    ValueError
        应答不是 JSON 列表，或 `version` 不是 `X.Y.Z`。
    """
    return parse_changes(fetch_releases(), version)


# -----------------------------------------------------------------------------
# 脱离执行的内部入口


def _detach_main(args: argparse.Namespace) -> int:
    """fork 出脱离父命令的 worker，并把它的 PID 写进 state。"""
    if args.lock_fd < 0:
        raise RolloutError("detacher lock fd is invalid")
    worker_pid = os.fork()
    if worker_pid > 0:
        update_state(args.state, worker_pid=worker_pid)
        return EXIT_HEALTHY
    try:
        os.setsid()
        code = _worker_main(args)
    except BaseException:
        code = EXIT_FAILED
    os._exit(code)


def _worker_main(args: argparse.Namespace) -> int:
    try:
        if args.lock_fd < 0:
            raise RolloutError("worker lock fd is invalid")
        deadline = time.monotonic() + DEFAULT_ARM_TIMEOUT
        while time.monotonic() < deadline:
            worker_pid = read_state(args.state).get("worker_pid")
            if isinstance(worker_pid, int) and worker_pid > 0:
                break
            time.sleep(POLL_INTERVAL)
        else:
            raise RolloutError("detacher did not publish worker pid")
        update_state(args.state, status="armed", reason="")
        run_worker(args.state)
        return EXIT_HEALTHY
    except Exception as err:
        # 布防前或 run_worker 自身出错：state 能写就落 failed，日志只记类名
        with contextlib.suppress(RolloutError):
            update_state(args.state, status="failed", reason=type(err).__name__)
        _warn(f"worker failed ({type(err).__name__})")
        return EXIT_FAILED


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claudex.upgrade")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("_detach", "_worker"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--state", type=Path, required=True)
        sub.add_argument("--lock-fd", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """内部入口 `python -P -m claudex.upgrade _detach|_worker`。

    由 `_start_rollout` 调用；命令层不经这里，直接调用 `start_upgrade`、
    `start_restart` 等函数。
    """
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "_detach":
            return _detach_main(args)
        return _worker_main(args)
    except (OSError, RolloutError) as err:
        print(f"claudex.upgrade: {type(err).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
