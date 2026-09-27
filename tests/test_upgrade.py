"""claudex.upgrade：归档安全、状态机与回退、worker 调用的启动器命令、锁、wait 与变更摘要。

归档是 `tmp_path` 里现场构造的 tar 包；下载经 monkeypatch 替换 `claudex.upgrade._fetch_bytes`；
启动器是写在 `tmp_path` 下的 shell 脚本，记录收到的参数。
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from claudex import paths, upgrade
from claudex.upgrade import RolloutError

OLD = "7.2.140"
NEW = "7.2.142"
RUN_ID = "0123456789abcdef0123456789abcdef"


# -----------------------------------------------------------------------------
# 归档


def _archive(tmp_path: Path, member_name: str = "cli-proxy-api") -> Path:
    archive = tmp_path / "release.tar.gz"
    payload = f"#!/bin/sh\necho 'CLIProxyAPI Version: {NEW}'\n".encode()
    info = tarfile.TarInfo(member_name)
    info.mode = 0o755
    info.size = len(payload)
    with tarfile.open(archive, "w:gz") as stream:
        stream.addfile(info, io.BytesIO(payload))
    return archive


def test_checksum_manifest_requires_exact_filename() -> None:
    digest = "a" * 64
    manifest = f"{digest}  asset.tar.gz\n{'b' * 64}  other.tar.gz\n"
    assert upgrade.parse_checksum_manifest(manifest, "asset.tar.gz") == digest
    with pytest.raises(RolloutError, match="matched 0"):
        upgrade.parse_checksum_manifest(manifest, "missing.tar.gz")
    with pytest.raises(RolloutError, match="matched 2"):
        upgrade.parse_checksum_manifest(manifest * 2, "asset.tar.gz")


def test_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    with pytest.raises(RolloutError, match="mismatch"):
        upgrade.verify_archive_checksum(archive, "0" * 64)
    upgrade.verify_archive_checksum(
        archive, hashlib.sha256(archive.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "name", ["../cli-proxy-api", "/tmp/cli-proxy-api", "a/../../x"]
)
def test_archive_path_traversal_is_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(RolloutError, match="escapes"):
        upgrade.extract_release_archive(_archive(tmp_path, name), tmp_path / "out")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_archive_link_members_are_rejected(tmp_path: Path, kind: bytes) -> None:
    archive = tmp_path / "release.tar.gz"
    info = tarfile.TarInfo("cli-proxy-api")
    info.type = kind
    info.linkname = "/bin/true"
    with tarfile.open(archive, "w:gz") as stream:
        stream.addfile(info)
    with pytest.raises(RolloutError, match="not allowed"):
        upgrade.extract_release_archive(archive, tmp_path / "out")


def test_archive_without_root_binary_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RolloutError, match="no root"):
        upgrade.extract_release_archive(
            _archive(tmp_path, "sub/cli-proxy-api"), tmp_path / "out"
        )


def _release_payloads(tmp_path: Path) -> list[bytes]:
    archive_bytes = _archive(tmp_path).read_bytes()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    manifest = f"{digest}  CLIProxyAPI_{NEW}_linux_amd64.tar.gz\n".encode()
    return [manifest, archive_bytes]


def _fake_fetch(monkeypatch: pytest.MonkeyPatch, responses: list[bytes]) -> list[str]:
    urls: list[str] = []

    def fetch(url: str, accept: str | None = None) -> bytes:
        del accept
        urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(upgrade, "_fetch_bytes", fetch)
    return urls


def test_prepare_release_installs_verified_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = _fake_fetch(monkeypatch, _release_payloads(tmp_path))
    binary = upgrade.prepare_release(NEW, tmp_path / "versions")
    assert urls == [
        f"{upgrade.RELEASE_BASE_URL}/v{NEW}/checksums.txt",
        f"{upgrade.RELEASE_BASE_URL}/v{NEW}/CLIProxyAPI_{NEW}_linux_amd64.tar.gz",
    ]
    assert binary == tmp_path / "versions" / NEW / "cli-proxy-api"
    assert os.access(binary, os.X_OK)
    manifest = json.loads((binary.parent / "install-manifest.json").read_text())
    assert manifest["version"] == NEW
    assert manifest["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()


def test_tampered_existing_version_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "versions" / NEW
    existing.mkdir(parents=True)
    binary = existing / "cli-proxy-api"
    binary.write_text(
        f"#!/bin/sh\n# tampered\necho 'CLIProxyAPI Version: {NEW}'\n", encoding="utf-8"
    )
    binary.chmod(0o755)
    _fake_fetch(monkeypatch, _release_payloads(tmp_path))
    with pytest.raises(RolloutError, match="does not match"):
        upgrade.prepare_release(NEW, tmp_path / "versions")


def test_prepare_release_rejects_bad_version(tmp_path: Path) -> None:
    with pytest.raises(RolloutError, match=r"X\.Y\.Z"):
        upgrade.prepare_release("latest", tmp_path)


# -----------------------------------------------------------------------------
# 状态机


class _Rig:
    """一套新旧两个版本目录、活动 symlink 与 armed 状态的 rollout 现场。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.old_target = root / OLD / "cli-proxy-api"
        self.new_target = root / NEW / "cli-proxy-api"
        for target, text in ((self.old_target, "old"), (self.new_target, "new")):
            target.parent.mkdir()
            target.write_text(text, encoding="utf-8")
        self.link = root / "cli-proxy-api"
        self.link.symlink_to(self.old_target)
        self.state_path = paths.rollouts_dir() / f"{RUN_ID}.json"
        self.calls: list[list[str]] = []
        upgrade.write_state(
            self.state_path,
            {
                "run_id": RUN_ID,
                "mode": "upgrade",
                "status": "armed",
                "old_target": str(self.old_target),
                "new_target": str(self.new_target),
                "symlink": str(self.link),
                "launcher": "/fake/claudex",
                "pid_file": str(paths.gateway_pid_file()),
                "worker_pid": os.getpid(),
                "started_at": 1,
                "reason": "",
            },
        )

    def runner(self, codes: list[int]) -> Callable[[list[str], float], int]:
        def run(command: list[str], timeout: float) -> int:
            del timeout
            self.calls.append(command)
            return codes.pop(0)

        return run

    @staticmethod
    def verify(state: dict[str, object], expected: Path) -> tuple[int, str]:
        del state
        return 321, str(expected)

    def run(
        self,
        codes: list[int],
        verify: Callable[[dict[str, object], Path], tuple[int, str]] | None = None,
    ) -> dict[str, object]:
        upgrade.run_worker(
            self.state_path,
            run_command=self.runner(codes),
            sleep=lambda _seconds: None,
            verify_service=verify or self.verify,
        )
        return upgrade.read_state(self.state_path)

    def actions(self) -> list[str]:
        return [" ".join(call[1:]) for call in self.calls]


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    return _Rig(tmp_path)


def test_healthy_switch(rig: _Rig) -> None:
    state = rig.run([0, 0])
    assert state["status"] == "healthy"
    assert state["service_pid"] == 321
    assert state["service_executable"] == str(rig.new_target)
    assert rig.link.resolve() == rig.new_target
    assert rig.actions() == ["gateway stop", "gateway start"]
    assert all(call[0] == "/fake/claudex" for call in rig.calls)


def test_worker_runs_launcher_gateway_stop_and_start(tmp_path: Path, rig: _Rig) -> None:
    log = tmp_path / "launcher.log"
    launcher = tmp_path / "claudex"
    launcher.write_text(f'#!/bin/sh\necho "$@" >> {log}\n', encoding="utf-8")
    launcher.chmod(0o755)
    upgrade.update_state(rig.state_path, launcher=str(launcher))
    upgrade.run_worker(rig.state_path, sleep=lambda _s: None, verify_service=rig.verify)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "gateway stop",
        "gateway start",
    ]
    assert upgrade.read_state(rig.state_path)["status"] == "healthy"


def test_old_identity_failure_never_stops(rig: _Rig) -> None:
    def refuse(state: dict[str, object], expected: Path) -> tuple[int, str]:
        del state, expected
        raise RolloutError("service pid is not alive: 1")

    state = rig.run([], verify=refuse)
    assert (state["status"], state["reason"]) == ("failed", "old_identity_failed")
    assert rig.calls == []


def test_stop_failure_never_switches(rig: _Rig) -> None:
    state = rig.run([1])
    assert (state["status"], state["reason"]) == ("failed", "stop_failed")
    assert rig.link.resolve() == rig.old_target


def test_new_start_failure_rolls_back(rig: _Rig) -> None:
    state = rig.run([0, 1, 0, 0])
    assert (state["status"], state["reason"]) == ("rolled_back", "new_start_failed")
    assert state["service_executable"] == str(rig.old_target)
    assert rig.link.resolve() == rig.old_target
    assert rig.actions() == [
        "gateway stop",
        "gateway start",
        "gateway stop",
        "gateway start",
    ]


def test_new_identity_failure_rolls_back(rig: _Rig) -> None:
    def new_is_wrong(state: dict[str, object], expected: Path) -> tuple[int, str]:
        if expected == rig.new_target:
            raise RolloutError("service identity mismatch")
        return rig.verify(state, expected)

    state = rig.run([0, 0, 0, 0], verify=new_is_wrong)
    assert (state["status"], state["reason"]) == ("rolled_back", "new_identity_failed")
    assert rig.link.resolve() == rig.old_target


@pytest.mark.parametrize(
    ("codes", "reason"),
    [([0, 1, 0, 1], "rollback_start_failed")],
)
def test_rollback_start_failure_is_failed(
    rig: _Rig, codes: list[int], reason: str
) -> None:
    state = rig.run(codes)
    assert (state["status"], state["reason"]) == ("failed", reason)
    assert rig.link.resolve() == rig.old_target


def test_symlink_exception_rolls_back(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = upgrade.atomic_symlink
    calls = {"n": 0}

    def fail_once(target: Path, link: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("switch failed")
        original(target, link)

    monkeypatch.setattr(upgrade, "atomic_symlink", fail_once)
    state = rig.run([0, 0])
    assert (state["status"], state["reason"]) == (
        "rolled_back",
        "worker_exception_OSError",
    )
    assert rig.link.resolve() == rig.old_target
    assert rig.actions() == ["gateway stop", "gateway start"]


def test_injected_runtime_error_after_stop_rolls_back(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_text = "token=FAKE-SECRET-should-not-appear"

    def runner(command: list[str], timeout: float) -> int:
        del timeout
        rig.calls.append(command)
        if len(rig.calls) == 2:
            raise RuntimeError(secret_text)
        return 0

    upgrade.run_worker(
        rig.state_path,
        run_command=runner,
        sleep=lambda _s: None,
        verify_service=rig.verify,
    )
    state = upgrade.read_state(rig.state_path)
    assert (state["status"], state["reason"]) == (
        "rolled_back",
        "worker_exception_RuntimeError",
    )
    assert rig.link.resolve() == rig.old_target
    assert rig.actions() == [
        "gateway stop",
        "gateway start",
        "gateway stop",
        "gateway start",
    ]
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert secret_text not in err
    assert secret_text not in rig.state_path.read_text(encoding="utf-8")


def test_exception_during_rollback_ends_failed(rig: _Rig) -> None:
    def runner(command: list[str], timeout: float) -> int:
        del timeout
        rig.calls.append(command)
        if len(rig.calls) >= 2:
            raise RuntimeError("boom")
        return 0

    upgrade.run_worker(
        rig.state_path,
        run_command=runner,
        sleep=lambda _s: None,
        verify_service=rig.verify,
    )
    state = upgrade.read_state(rig.state_path)
    assert (state["status"], state["reason"]) == (
        "failed",
        "worker_exception_RuntimeError",
    )


def _stale_directory(rig: _Rig, version: str = "7.2.100") -> Path:
    directory = rig.root / version
    directory.mkdir()
    (directory / "cli-proxy-api").write_text("stale", encoding="utf-8")
    return directory


def test_healthy_upgrade_prunes_unreferenced_versions(rig: _Rig) -> None:
    stale = _stale_directory(rig)
    rig.run([0, 0])
    assert not stale.exists()
    assert rig.old_target.is_file()
    assert rig.new_target.is_file()


def test_restart_keeps_versions(rig: _Rig) -> None:
    stale = _stale_directory(rig)
    upgrade.update_state(rig.state_path, mode="restart")
    rig.run([0, 0])
    assert stale.is_dir()


def test_prune_failure_only_warns(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = _stale_directory(rig)

    def busy(path: Path) -> None:
        del path
        raise OSError("busy")

    monkeypatch.setattr(upgrade.shutil, "rmtree", busy)
    state = rig.run([0, 0])
    assert state["status"] == "healthy"
    assert stale.is_dir()
    assert "7.2.100 not pruned" in capsys.readouterr().err


def test_state_never_contains_credentials(rig: _Rig) -> None:
    rig.run([0, 0])
    text = rig.state_path.read_text(encoding="utf-8")
    for needle in (
        "api-key",
        "Authorization",
        "Bearer",
        "client.key",
        "management.key",
    ):
        assert needle not in text
    assert stat_mode(rig.state_path) == 0o600


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


# -----------------------------------------------------------------------------
# state、锁与 wait


def _write(status: str, **fields: object) -> Path:
    path = paths.rollouts_dir() / f"{RUN_ID}.json"
    upgrade.write_state(path, {"run_id": RUN_ID, "status": status, **fields})
    return path


def test_concurrent_lock_is_rejected() -> None:
    first = upgrade.acquire_lock(paths.upgrade_lock_file())
    try:
        with pytest.raises(RolloutError, match="already running"):
            upgrade.acquire_lock(paths.upgrade_lock_file())
    finally:
        first.close()
    upgrade.acquire_lock(paths.upgrade_lock_file()).close()


@pytest.mark.parametrize(
    ("status", "fields", "message"),
    [
        (
            "healthy",
            {"worker_pid": 0, "service_pid": 1, "service_executable": "x"},
            "worker_pid",
        ),
        ("healthy", {"worker_pid": 1}, "service identity"),
        ("armed", {"worker_pid": True}, "worker_pid"),
    ],
)
def test_state_shape_is_enforced(
    status: str, fields: dict[str, object], message: str
) -> None:
    path = _write(status, **fields)
    with pytest.raises(RolloutError, match=message):
        upgrade.read_state(path)


def test_state_rejects_filename_mismatch() -> None:
    path = paths.rollouts_dir() / f"{RUN_ID}.json"
    upgrade.write_state(path, {"run_id": "other", "status": "staged", "worker_pid": 0})
    with pytest.raises(RolloutError, match="does not match"):
        upgrade.read_state(path)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("healthy", upgrade.EXIT_HEALTHY),
        ("rolled_back", upgrade.EXIT_ROLLED_BACK),
        ("failed", upgrade.EXIT_FAILED),
    ],
)
def test_wait_returns_terminal_codes(status: str, code: int) -> None:
    _write(
        status,
        worker_pid=os.getpid(),
        service_pid=os.getpid(),
        service_executable="/bin/true",
    )
    assert upgrade.wait_run(RUN_ID, timeout=0.1) == code


def test_wait_times_out_distinctly() -> None:
    _write("armed", worker_pid=os.getpid())
    assert upgrade.wait_run(RUN_ID, timeout=0) == upgrade.EXIT_TIMEOUT


def test_wait_marks_dead_worker_failed() -> None:
    path = _write("armed", worker_pid=999_999_999)
    assert upgrade.wait_run(RUN_ID, timeout=0.2) == upgrade.EXIT_FAILED
    assert upgrade.read_state(path)["reason"] == "worker_exited_before_terminal"


def test_wait_rejects_bad_run_id() -> None:
    with pytest.raises(RolloutError, match="run id"):
        upgrade.wait_run("../x", timeout=0)


# -----------------------------------------------------------------------------
# 启动 rollout


def _no_check() -> None:
    return None


@pytest.fixture(autouse=True)
def _no_real_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认把下载换成直接失败：任何用例都不会真的访问 GitHub；需要下载内容的用例
    再用 `_fake_fetch` 覆盖。"""

    def refuse(url: str, accept: str | None = None) -> bytes:
        del accept
        raise RolloutError(f"test attempted a real download: {url}")

    monkeypatch.setattr(upgrade, "_fetch_bytes", refuse)


def test_upgrade_requires_installed_binary(tmp_path: Path) -> None:
    with pytest.raises(RolloutError, match="README"):
        upgrade.start_upgrade(
            NEW,
            launcher=tmp_path / "claudex",
            install_root=tmp_path / "versions",
            gateway_link=tmp_path / "missing" / "cli-proxy-api",
            gateway_check=_no_check,
        )


def test_upgrade_requires_running_gateway(tmp_path: Path) -> None:
    target = tmp_path / OLD / "cli-proxy-api"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    link = tmp_path / "cli-proxy-api"
    link.symlink_to(target)
    with pytest.raises(RolloutError, match="claudex gateway start"):
        upgrade.start_upgrade(
            NEW,
            launcher=tmp_path / "claudex",
            install_root=tmp_path / "versions",
            gateway_link=link,
            gateway_check=_no_check,
        )
    assert not paths.rollouts_dir().exists()


def test_unanswering_gateway_blocks_restart(tmp_path: Path) -> None:
    target = tmp_path / OLD / "cli-proxy-api"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    link = tmp_path / "cli-proxy-api"
    link.symlink_to(target)

    def unanswering() -> None:
        raise RolloutError("网关 x 请求失败：refused")

    with pytest.raises(RolloutError, match="claudex gateway start"):
        upgrade.start_restart(
            launcher=tmp_path / "claudex",
            gateway_link=link,
            gateway_check=unanswering,
            verify_service=_Rig.verify,
        )


def test_rollout_refused_while_another_runs(tmp_path: Path) -> None:
    held = upgrade.acquire_lock(paths.upgrade_lock_file())
    try:
        with pytest.raises(RolloutError, match="already running"):
            upgrade.start_restart(
                launcher=tmp_path / "claudex",
                gateway_link=tmp_path / "cli-proxy-api",
                gateway_check=_no_check,
            )
    finally:
        held.close()


def test_detached_restart_reaches_healthy(tmp_path: Path) -> None:
    target = tmp_path / OLD / "cli-proxy-api"
    target.parent.mkdir()
    shutil.copy2("/bin/sleep", target)
    link = tmp_path / "cli-proxy-api"
    link.symlink_to(target)
    pid_file = paths.gateway_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "launcher.log"
    launcher = tmp_path / "claudex"
    launcher.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'case "$2" in\n'
        f"  stop) if [ -f '{pid_file}' ]; then kill \"$(cat '{pid_file}')\" 2>/dev/null || true; rm -f '{pid_file}'; fi ;;\n"
        f"  start) '{link}' 30 & echo $! > '{pid_file}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    subprocess.run([str(launcher), "gateway", "start"], check=True)
    try:
        result = upgrade.start_restart(
            delay_seconds=0.2,
            launcher=launcher,
            gateway_link=link,
            gateway_check=_no_check,
        )
        assert result["status"] == "armed"
        run_id = str(result["run_id"])
        assert upgrade.wait_run(run_id, 10) == upgrade.EXIT_HEALTHY
        terminal = upgrade.read_state(upgrade.state_path_for(run_id))
        assert terminal["service_executable"] == str(target)
        assert log.read_text(encoding="utf-8").splitlines() == [
            "gateway start",
            "gateway stop",
            "gateway start",
        ]
        assert stat_mode(upgrade.state_path_for(run_id)) == 0o600
    finally:
        subprocess.run([str(launcher), "gateway", "stop"], check=True)


# -----------------------------------------------------------------------------
# 变更摘要


def _releases() -> list[dict[str, object]]:
    return [
        {
            "tag_name": "v7.3.22",
            "name": "",
            "body": "- fix c\n* feat d",
            "draft": False,
        },
        {"tag_name": "v7.3.23", "name": "RC", "body": "- x", "prerelease": True},
        {"tag_name": "v7.3.24", "name": "draft", "body": "- y", "draft": True},
        {
            "tag_name": "v7.3.21",
            "name": "Big one",
            "body": "intro\n- fix a\n  - nested b\n",
        },
        {"tag_name": "v7.3.20", "name": "current", "body": "- old"},
        {"tag_name": "nightly", "name": "n", "body": "- z"},
    ]


def test_parse_changes_lists_newer_releases() -> None:
    assert upgrade.parse_changes(_releases(), "7.3.20") == [
        "v7.3.22",
        "  - fix c",
        "  - feat d",
        "v7.3.21 Big one",
        "  - fix a",
        "  - nested b",
    ]


def test_parse_changes_caps_points() -> None:
    body = "\n".join(f"- point {index}" for index in range(15))
    lines = upgrade.parse_changes([{"tag_name": "v1.0.1", "body": body}], "1.0.0")
    assert len(lines) == 1 + upgrade.MAX_CHANGE_POINTS


def test_parse_changes_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match=r"X\.Y\.Z"):
        upgrade.parse_changes([], "latest")
    with pytest.raises(ValueError, match="列表"):
        upgrade.parse_changes({"message": "rate limited"}, "1.0.0")


def test_changes_since_uses_github_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str | None]] = []

    def fetch(url: str, accept: str | None = None) -> bytes:
        seen.append((url, accept))
        return json.dumps(_releases()).encode()

    monkeypatch.setattr(upgrade, "_fetch_bytes", fetch)
    assert upgrade.changes_since("7.3.21") == ["v7.3.22", "  - fix c", "  - feat d"]
    assert seen == [
        (f"{upgrade.RELEASES_API_URL}?per_page=100", "application/vnd.github+json")
    ]


def test_external_timeout_is_shared_with_quota() -> None:
    assert upgrade.EXTERNAL_TIMEOUT_SECONDS == 10
