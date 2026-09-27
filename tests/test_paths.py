"""claudex.paths：两个根目录的覆盖与缺省，以及派生路径。"""

from collections.abc import Callable
from pathlib import Path

import pytest

from claudex import paths


def test_config_dir_follows_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDEX_CONFIG_DIR", str(tmp_path / "cfg"))
    assert paths.config_dir() == tmp_path / "cfg"


def test_state_dir_follows_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDEX_STATE_DIR", str(tmp_path / "st"))
    assert paths.state_dir() == tmp_path / "st"


@pytest.mark.parametrize("value", [None, ""])
def test_roots_default_under_home(
    value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "somebody"
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDEX_CONFIG_DIR", "CLAUDEX_STATE_DIR"):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert paths.config_dir() == home / ".config" / "claudex"
    assert paths.state_dir() == home / ".local" / "state" / "claudex"
    assert paths.auth_dir() == home / ".local" / "share" / "claudex" / "auth"


@pytest.mark.parametrize(
    ("function", "root", "relative"),
    [
        (paths.config_file, "config", "claudex.toml"),
        (paths.gateway_base_file, "config", "gateway.base.yaml"),
        (paths.settings_base_file, "config", "settings.base.json"),
        (paths.client_key_file, "config", "client.key"),
        (paths.management_key_file, "config", "management.key"),
        (paths.keys_dir, "config", "keys"),
        (paths.gateway_config_file, "state", "gateway.yaml"),
        (paths.gateway_pid_file, "state", "gateway.pid"),
        (paths.gateway_start_lock_file, "state", "gateway-start.lock"),
        (paths.gateway_bootstrap_log_file, "state", "gateway-bootstrap.log"),
        (paths.gateway_logs_dir, "state", "logs"),
        (paths.quota_file, "state", "quota.json"),
        (paths.refresh_file, "state", "refresh.json"),
        (paths.refresh_lock_file, "state", "refresh.lock"),
        (paths.gateway_release_file, "state", "gateway-release.json"),
        (paths.rollouts_dir, "state", "rollouts"),
        (paths.upgrade_lock_file, "state", "upgrade.lock"),
        (paths.sessions_dir, "state", "sessions"),
        (paths.catalog_file, "state", "catalog.json"),
    ],
)
def test_derived_paths_follow_roots(
    function: Callable[[], Path],
    root: str,
    relative: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDEX_CONFIG_DIR", str(tmp_path / "config-root"))
    monkeypatch.setenv("CLAUDEX_STATE_DIR", str(tmp_path / "state-root"))
    assert function() == tmp_path / f"{root}-root" / relative


def test_source_key_file_is_named_after_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDEX_CONFIG_DIR", str(tmp_path))
    assert paths.source_key_file("acme") == tmp_path / "keys" / "acme.key"


def test_paths_are_evaluated_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDEX_STATE_DIR", str(tmp_path / "first"))
    first = paths.gateway_config_file()
    monkeypatch.setenv("CLAUDEX_STATE_DIR", str(tmp_path / "second"))
    assert first == tmp_path / "first" / "gateway.yaml"
    assert paths.gateway_config_file() == tmp_path / "second" / "gateway.yaml"
