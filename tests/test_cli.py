"""claudex.cli 的子命令、启动器 `bin/claudex` 与 `bin/claudex-client-key`。

子命令在进程内经 `cli.main` 调用，外网换成本地假服务（`fake_service`）或替身函数。
启动器以子进程运行源码树的 `bin/claudex`：假 `claude` 放在 PATH 上的临时目录，假网关
（`fake_gateway`）装在假 HOME 的 `.local/bin/cli-proxy-api`。启动器与生成器把网关地址固定
为 127.0.0.1:8317，所以启动器用例都在 `unshare` 建的独立用户与网络命名空间里运行，
假网关监听的是命名空间自己的回环端口，碰不到本机真实网关；系统不允许建用户命名空间时
这些用例跳过。
"""

import functools
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fake_gateway import install as install_fake_gateway
from fake_service import FakeService

from claudex import catalog, cli, paths, quota, render, upgrade
from claudex.catalog import Catalog, CatalogEntry, Pricing

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "bin" / "claudex"
CLIENT_KEY_COMMAND = REPO / "bin" / "claudex-client-key"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
SOURCE_KEYS = {"or": "sk-FAKE-or-7q2w9e", "alpha": "sk-FAKE-alpha-4r8t1y"}
RUN_ID = "0123456789abcdef0123456789abcdef"
LAUNCH_TIMEOUT_SECONDS = 60
STOP_WAIT_SECONDS = 5
UNSHARE = shutil.which("unshare")
NSENTER = shutil.which("nsenter")
IP = shutil.which("ip") or next(
    (path for path in ("/usr/sbin/ip", "/sbin/ip") if Path(path).exists()), None
)
CURL = shutil.which("curl")

CONFIG_TEXT = """\
default_profile = "daily"
mcp_deny = ["mcp__github__*", "mcp__alpha__*"]

[sources.or]
type = "openrouter"
models = ["vendor/model-c", "vendor/model-d"]

[sources.alpha]
type = "openai"
base_url = "https://example.invalid/v1"
models = [{ id = "model-a", context = 128000 }]

[profiles.daily]
fable = "or/vendor/model-c"
opus = "or/vendor/model-d"
sonnet = "alpha/model-a"
haiku = "or/vendor/model-c"

[profiles.alt]
fable = "alpha/model-a"
opus = "alpha/model-a"
sonnet = "or/vendor/model-c"
haiku = "or/vendor/model-c"
"""
SUBSCRIPTION_SOURCE = """
[sources.sub]
type = "codex"
models = ["model-s"]
"""
FAKE_CLAUDE = """\
#!/usr/bin/env bash
printf '%s\\n' "$@" > "$FAKE_CLAUDE_OUT/argv"
printf '%s' "${ANTHROPIC_CUSTOM_HEADERS-<unset>}" > "$FAKE_CLAUDE_OUT/headers"
printf '%s' "${CLAUDEX_FAST-<unset>}" > "$FAKE_CLAUDE_OUT/fast"
for name in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDEX_STATUSLINE_COMMAND \\
    CLAUDE_CODE_AUTO_COMPACT_WINDOW; do
  printf '%s=%s\\n' "$name" "${!name-<unset>}"
done > "$FAKE_CLAUDE_OUT/inherited"
"""
INHERITED_SESSION_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDEX_STATUSLINE_COMMAND",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
)


def _catalog(*extra: str) -> Catalog:
    entries = {
        "vendor/model-c": CatalogEntry(
            name="Model C",
            context_length=200_000,
            supported_parameters=("reasoning",),
            pricing=Pricing(
                prompt=0.000002,
                completion=0.00001,
                input_cache_read=None,
                input_cache_write=None,
            ),
        ),
        "vendor/model-d": CatalogEntry(
            name="Model D",
            context_length=400_000,
            supported_parameters=(),
            pricing=None,
        ),
    }
    for slug in extra:
        entries[slug] = CatalogEntry(
            name=None, context_length=150_000, supported_parameters=(), pricing=None
        )
    return Catalog(fetched_at=NOW, models=entries)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _configure(text: str = CONFIG_TEXT) -> None:
    """`init` 之后写入合成配置、两个来源的 key 与目录缓存。"""
    assert cli.main(["init"]) == 0
    paths.config_file().write_text(text, encoding="utf-8")
    for name, key in SOURCE_KEYS.items():
        _write_private(paths.source_key_file(name), f"{key}\n")
    catalog.save_catalog(_catalog())


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _settings_env(settings: Mapping[str, object]) -> dict[str, str]:
    return cast("dict[str, str]", settings["env"])


# -----------------------------------------------------------------------------
# init、key set、completion、profiles 与内部子命令


def test_init_creates_private_roots_keys_and_starter_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["init"]) == 0
    root = paths.config_dir()
    assert _mode(root) == 0o700
    assert _mode(paths.keys_dir()) == 0o700
    for key_file in (paths.client_key_file(), paths.management_key_file()):
        assert _mode(key_file) == 0o600
        text = key_file.read_text(encoding="utf-8")
        assert len(text.strip()) == 64
        assert set(text.strip()) <= set("0123456789abcdef")
    assert (
        paths.client_key_file().read_text() != paths.management_key_file().read_text()
    )
    assert 'mcp_deny = ["mcp__github__*"]' in paths.config_file().read_text()
    assert 'disable-image-generation: "chat"' in paths.gateway_base_file().read_text()
    json.loads(paths.settings_base_file().read_text(encoding="utf-8"))
    assert "已生成" in capsys.readouterr().out


def test_init_does_not_overwrite_existing_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["init"]) == 0
    targets = [
        paths.client_key_file(),
        paths.management_key_file(),
        paths.config_file(),
        paths.gateway_base_file(),
        paths.settings_base_file(),
    ]
    for target in targets:
        target.write_text(f"custom {target.name}\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["init"]) == 0
    for target in targets:
        assert target.read_text(encoding="utf-8") == f"custom {target.name}\n"
    output = capsys.readouterr().out
    assert output.count("已存在，不覆盖") == len(targets)


def test_key_set_writes_private_key_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure()
    paths.source_key_file("or").unlink()
    paths.keys_dir().chmod(0o755)
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-FAKE-new-1a2b3c\n"))
    assert cli.main(["key", "set", "or"]) == 0
    key_file = paths.source_key_file("or")
    assert key_file.read_text(encoding="utf-8") == "sk-FAKE-new-1a2b3c\n"
    assert _mode(key_file) == 0o600
    assert _mode(paths.keys_dir()) == 0o700


@pytest.mark.parametrize(
    ("source", "message"),
    [("missing", "不在 claudex.toml 里"), ("sub", "订阅来源")],
)
def test_key_set_rejects_unknown_and_subscription_sources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    message: str,
) -> None:
    _configure(CONFIG_TEXT + SUBSCRIPTION_SOURCE)
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-FAKE-x\n"))
    assert cli.main(["key", "set", source]) == 1
    assert message in capsys.readouterr().err
    assert not paths.source_key_file(source).exists()


def test_key_set_rejects_blank_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure()
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk FAKE\n"))
    assert cli.main(["key", "set", "or"]) == 1
    assert "不含空白" in capsys.readouterr().err
    assert paths.source_key_file("or").read_text() == f"{SOURCE_KEYS['or']}\n"


def test_completion_bash_parses(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["completion", "bash"]) == 0
    script = capsys.readouterr().out
    checked = subprocess.run(
        ["bash", "-n"], input=script, capture_output=True, text=True, check=False
    )
    assert checked.returncode == 0, checked.stderr
    assert "complete -F _claudex claudex" in script


def test_complete_lists_generic_sources(capsys: pytest.CaptureFixture[str]) -> None:
    _configure(CONFIG_TEXT + SUBSCRIPTION_SOURCE)
    capsys.readouterr()
    assert cli.main(["_complete", "generic-sources"]) == 0
    assert capsys.readouterr().out.split() == ["or", "alpha"]


def test_profiles_lists_default_and_tiers(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    capsys.readouterr()
    assert cli.main(["profiles"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "* daily  fable=or/vendor/model-c  opus=or/vendor/model-d  "
        "sonnet=alpha/model-a  haiku=or/vendor/model-c",
        "  alt  fable=alpha/model-a  opus=alpha/model-a  "
        "sonnet=or/vendor/model-c  haiku=or/vendor/model-c",
    ]


def test_gateway_config_reports_whether_it_rewrote(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    capsys.readouterr()
    assert cli.main(["_gateway-config"]) == 0
    assert cli.main(["_gateway-config"]) == 0
    assert capsys.readouterr().out.split() == ["changed", "unchanged"]
    assert _mode(paths.gateway_config_file()) == 0o600


# -----------------------------------------------------------------------------
# preflight


def _recording_preflight(
    monkeypatch: pytest.MonkeyPatch, *, error: bool
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake(config: object, *, profile: str, check_gateway: bool) -> render.Report:
        del config
        calls.append({"profile": profile, "check_gateway": check_gateway})
        report = render.Report(profile=profile, online_checks=check_gateway)
        if error:
            report.errors.append(render.Diagnostic("config", "broken", profile=profile))
        return report

    monkeypatch.setattr(render, "preflight", fake)
    return calls


def test_preflight_defaults_ignore_claudex_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure()
    monkeypatch.setenv("CLAUDEX_PROFILE", "alt")
    calls = _recording_preflight(monkeypatch, error=False)
    assert cli.main(["preflight"]) == 0
    assert calls == [{"profile": "daily", "check_gateway": True}]
    assert "profile daily" in capsys.readouterr().out


def test_preflight_passes_options_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure()
    capsys.readouterr()
    calls = _recording_preflight(monkeypatch, error=True)
    code = cli.main(
        ["preflight", "--profile", "alt", "--format", "json", "--no-proxy-check"]
    )
    assert code == 1
    assert calls == [{"profile": "alt", "check_gateway": False}]
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1
    assert payload["profile"] == "alt"
    assert payload["errors"][0]["category"] == "config"


def test_preflight_reports_unreadable_config_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["preflight", "--format", "json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["online_checks"] is False
    assert [item["category"] for item in payload["errors"]] == ["config"]


def test_preflight_usage_error_exits_2() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["preflight", "--format", "xml"])
    assert raised.value.code == 2


# -----------------------------------------------------------------------------
# status


def _status_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    return capsys.readouterr().out.splitlines()


def test_status_without_gateway_exits_zero_with_runtime_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    lines = _status_lines(capsys)
    assert lines
    assert all(line.startswith(cli.STATUS_PREFIX) for line in lines)
    assert f"{cli.STATUS_PREFIX}gateway process: not running" in lines


def test_status_snapshot_line_skips_files_removed_during_listing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 悬空链接在 glob 里列得出、stat 时 FileNotFoundError，与清理程序删掉文件同一路径
    _configure()
    sessions = paths.sessions_dir()
    sessions.mkdir(parents=True)
    older = sessions / "alt-000000000000.profile.json"
    newer = sessions / "daily-111111111111.profile.json"
    older.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    (sessions / "gone-222222222222.profile.json").symlink_to(sessions / "missing")
    lines = _status_lines(capsys)
    assert f"{cli.STATUS_PREFIX}snapshot: {newer}" in lines


def test_status_notes_newer_release_without_problem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure()
    monkeypatch.setattr(cli, "installed_gateway_version", lambda _binary: "7.3.20")
    quota.write_release_record("7.3.21", int(NOW.timestamp()))
    lines = _status_lines(capsys)
    notes = [line for line in lines if line.startswith(cli.NOTE_PREFIX)]
    assert len(notes) == 1
    assert "7.3.21" in notes[0]
    assert not [line for line in lines if line.startswith(cli.PROBLEM_PREFIX)]


def test_status_reports_slug_missing_from_catalog(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    catalog.save_catalog(
        Catalog(
            fetched_at=NOW,
            models={"vendor/model-c": _catalog().models["vendor/model-c"]},
        )
    )
    problems = [
        line for line in _status_lines(capsys) if line.startswith(cli.PROBLEM_PREFIX)
    ]
    assert len(problems) == 1
    assert "vendor/model-d" in problems[0]


# -----------------------------------------------------------------------------
# update


def test_update_refreshes_catalog_release_and_changes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_service: FakeService,
) -> None:
    fake_service.reply(
        "GET",
        "/api/v1/models",
        {
            "data": [
                {
                    "id": "vendor/model-c",
                    "name": "Model C",
                    "context_length": 200000,
                    "pricing": {"prompt": "0.000002", "completion": "0.00001"},
                }
            ]
        },
    )
    fake_service.reply("GET", "/latest", {"tag_name": "v7.3.21"})
    fake_service.reply(
        "GET",
        "/releases",
        [
            {"tag_name": "v7.3.21", "name": "Fix", "body": "- fixed a\n- fixed b"},
            {"tag_name": "v7.3.20", "name": "Old", "body": "- old"},
        ],
    )
    monkeypatch.setattr(
        catalog,
        "fetch_catalog",
        functools.partial(
            catalog.fetch_catalog, url=f"{fake_service.url}/api/v1/models"
        ),
    )
    monkeypatch.setattr(quota, "LATEST_RELEASE_URL", f"{fake_service.url}/latest")
    monkeypatch.setattr(upgrade, "RELEASES_API_URL", f"{fake_service.url}/releases")
    monkeypatch.setattr(cli, "installed_gateway_version", lambda _binary: "7.3.20")
    paths.catalog_file().write_text("{broken", encoding="utf-8")

    assert cli.main(["update"]) == 0
    refreshed = catalog.load_catalog()
    assert refreshed is not None
    assert list(refreshed.models) == ["vendor/model-c"]
    record = quota.load_release_record()
    assert record is not None
    assert record["version"] == "7.3.21"
    output = capsys.readouterr().out
    assert "v7.3.21 Fix" in output
    assert "  - fixed a" in output
    assert "Old" not in output


# -----------------------------------------------------------------------------
# upgrade 与 gateway restart


def _armed(**recorded: object) -> dict[str, object]:
    return {"run_id": RUN_ID, **recorded}


def test_upgrade_passes_version_and_delay(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, float]] = []

    def fake(version: str, *, delay_seconds: float) -> dict[str, object]:
        calls.append((version, delay_seconds))
        return _armed()

    monkeypatch.setattr(upgrade, "start_upgrade", fake)
    assert cli.main(["upgrade", "7.3.21", "--delay", "3"]) == 0
    assert calls == [("7.3.21", 3.0)]
    assert f"--wait {RUN_ID}" in capsys.readouterr().out


def test_upgrade_defaults_to_latest_release(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(version: str, *, delay_seconds: float) -> dict[str, object]:
        del delay_seconds
        calls.append(version)
        return _armed()

    monkeypatch.setattr(upgrade, "start_upgrade", fake)
    monkeypatch.setattr(quota, "fetch_latest_release", lambda: "7.3.22")
    assert cli.main(["upgrade"]) == 0
    assert calls == ["7.3.22"]


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [(upgrade.EXIT_HEALTHY, 0), (upgrade.EXIT_ROLLED_BACK, 1)],
)
def test_upgrade_wait_passes_run_id_and_timeout(
    monkeypatch: pytest.MonkeyPatch, terminal: int, expected: int
) -> None:
    calls: list[tuple[str, float]] = []

    def fake(run_id: str, timeout: float) -> int:
        calls.append((run_id, timeout))
        return terminal

    monkeypatch.setattr(upgrade, "wait_run", fake)
    assert cli.main(["upgrade", "--wait", RUN_ID, "--timeout", "5"]) == expected
    assert calls == [(RUN_ID, 5.0)]


def test_gateway_restart_goes_to_managed_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    def fake(*, delay_seconds: float) -> dict[str, object]:
        calls.append(delay_seconds)
        return _armed()

    monkeypatch.setattr(upgrade, "start_restart", fake)
    assert cli.main(["gateway", "restart", "--delay", "2"]) == 0
    assert calls == [2.0]
    assert cli.main(["gateway", "start"]) == 2


# -----------------------------------------------------------------------------
# login


class _Exec(Exception):
    """替身 execve 抛出它，携带参数。"""

    def __init__(self, path: str, argv: list[str], env: dict[str, str]) -> None:
        super().__init__(path)
        self.path = path
        self.argv = argv
        self.env = env


@pytest.mark.parametrize(
    ("channel", "flags"),
    [
        ("codex", ["-codex-device-login"]),
        ("antigravity", ["-antigravity-login", "-no-browser"]),
    ],
)
def test_login_runs_gateway_with_generated_config(
    monkeypatch: pytest.MonkeyPatch, channel: str, flags: list[str]
) -> None:
    _configure()
    binary = upgrade.default_gateway_link()
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    visited: list[str] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        raise _Exec(path, argv, env)

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(os, "chdir", lambda target: visited.append(str(target)))
    monkeypatch.setattr(os, "umask", lambda _mask: 0o022)
    with pytest.raises(_Exec) as raised:
        cli.main(["login", channel])
    config_file = paths.gateway_config_file()
    assert raised.value.argv == [str(binary), "-config", str(config_file), *flags]
    assert raised.value.env["WRITABLE_PATH"] == str(paths.state_dir())
    assert visited == [str(paths.state_dir())]
    assert _mode(config_file) == 0o600


# -----------------------------------------------------------------------------
# claudex-client-key


def _client_key(
    text: str | None, mode: int = 0o600
) -> subprocess.CompletedProcess[str]:
    key_file = paths.client_key_file()
    if text is not None:
        _write_private(key_file, text)
        key_file.chmod(mode)
    return subprocess.run(
        [str(CLIENT_KEY_COMMAND)], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("ending", ["\n", ""])
def test_client_key_prints_key_with_or_without_newline(ending: str) -> None:
    key = "ab" * 32
    result = _client_key(f"{key}{ending}")
    assert result.returncode == 0
    assert result.stdout == f"{key}\n"


@pytest.mark.parametrize(
    ("text", "mode", "reason"),
    [
        (None, 0o600, "missing client key"),
        ("ab" * 32 + "\n", 0o644, "mode 600"),
        ("not-a-key\n", 0o600, "64 lowercase hex"),
    ],
)
def test_client_key_failures_leave_stdout_empty(
    text: str | None, mode: int, reason: str
) -> None:
    result = _client_key(text, mode)
    assert result.returncode != 0
    assert result.stdout == ""
    assert reason in result.stderr


# -----------------------------------------------------------------------------
# 启动器：不需要网关的转发


def test_launcher_upgrade_wait_goes_straight_to_cli() -> None:
    # 没有 claudex.toml：若启动器先生成网关配置，报的会是配置错误
    result = subprocess.run(
        [str(LAUNCHER), "upgrade", "--wait", RUN_ID, "--timeout", "0.1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "claudex.toml" not in result.stderr
    assert result.stderr.startswith("claudex: ")


# -----------------------------------------------------------------------------
# 启动器：命名空间里的假网关


@dataclass
class Namespace:
    """一个独立用户与网络命名空间，启动器经 nsenter 在其中运行。"""

    pid: int
    env: dict[str, str]
    claude_out: Path
    gateway_log: Path
    curl_argv: Path

    def run(
        self, *args: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert NSENTER is not None
        return subprocess.run(
            [
                NSENTER,
                "--preserve-credentials",
                "--user",
                "--net",
                f"--target={self.pid}",
                str(LAUNCHER),
                *args,
            ],
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=LAUNCH_TIMEOUT_SECONDS,
            check=False,
        )


@dataclass(frozen=True)
class Launch:
    """假 claude 收到的参数、派生 settings 与进程环境里的两个 Fast 变量。"""

    argv: list[str]
    settings: dict[str, object]
    headers: str
    fast: str


def _stop_process(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def _gateway_pid() -> int:
    return int(paths.gateway_pid_file().read_text(encoding="utf-8").strip())


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def netns(tmp_path: Path) -> Iterator[Namespace]:
    """合成配置、假网关、假 claude 与记录参数的 curl 包装，外加一个命名空间。"""
    if UNSHARE is None or NSENTER is None or IP is None or CURL is None:
        pytest.skip("需要 unshare、nsenter、ip 与 curl")
    holder = subprocess.Popen(
        [
            UNSHARE,
            "--user",
            "--map-root-user",
            "--net",
            "sh",
            "-c",
            '"$0" link set lo up && echo ready && exec sleep 3600',
            IP,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stderr is not None
    if holder.stdout.readline().strip() != "ready":
        holder.kill()
        holder.wait()
        pytest.skip(f"建不了用户与网络命名空间：{holder.stderr.read().strip()}")
    _configure()
    install_fake_gateway(Path.home())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "claude", FAKE_CLAUDE)
    curl_argv = tmp_path / "curl-argv"
    _write_executable(
        bin_dir / "curl",
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> {curl_argv}\nexec {CURL} "$@"\n',
    )
    claude_out = tmp_path / "claude-out"
    claude_out.mkdir()
    gateway_log = tmp_path / "gateway.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_CLAUDE_OUT": str(claude_out),
        "FAKE_GATEWAY_LOG": str(gateway_log),
    }
    try:
        yield Namespace(holder.pid, env, claude_out, gateway_log, curl_argv)
    finally:
        if paths.gateway_pid_file().exists():
            _stop_process(_gateway_pid())
        holder.kill()
        holder.wait()


def _launch(ns: Namespace, *args: str, env: Mapping[str, str] | None = None) -> Launch:
    result = ns.run(*args, env=env)
    assert result.returncode == 0, result.stderr
    argv = (ns.claude_out / "argv").read_text(encoding="utf-8").splitlines()
    assert argv[0] == "--settings"
    settings = cast(
        "dict[str, object]", json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    )
    return Launch(
        argv=argv,
        settings=settings,
        headers=(ns.claude_out / "headers").read_text(encoding="utf-8"),
        fast=(ns.claude_out / "fast").read_text(encoding="utf-8"),
    )


def test_launcher_uses_default_profile_and_ignores_claudex_profile(
    netns: Namespace,
) -> None:
    launch = _launch(netns, "-p", "hello", env={"CLAUDEX_PROFILE": "alt"})
    assert _settings_env(launch.settings)["CLAUDEX_PROFILE"] == "daily"


@pytest.mark.parametrize(
    "selector", [["@alt"], ["--profile", "alt"], ["--profile=alt"]]
)
def test_launcher_selects_profile(netns: Namespace, selector: list[str]) -> None:
    launch = _launch(netns, *selector)
    assert _settings_env(launch.settings)["CLAUDEX_PROFILE"] == "alt"


def test_launcher_tier_override_only_affects_this_snapshot(netns: Namespace) -> None:
    before = paths.config_file().read_text(encoding="utf-8")
    overridden = _settings_env(_launch(netns, "--opus", "alpha/model-a").settings)
    assert overridden["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "alpha/model-a"
    plain = _settings_env(_launch(netns).settings)
    assert plain["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "or/vendor-model-d[1m]"
    assert paths.config_file().read_text(encoding="utf-8") == before


def test_launcher_mcp_deny_and_passthrough(netns: Namespace) -> None:
    denied = _launch(netns, "@daily", "-p", "hello")
    assert denied.argv[2:] == [
        "--disallowed-tools",
        "mcp__github__* mcp__alpha__*",
        "-p",
        "hello",
    ]
    allowed = _launch(netns, "--with-mcp", "--", "@daily")
    assert allowed.argv[2:] == ["@daily"]


def test_launcher_fast_is_generated_and_not_inherited(netns: Namespace) -> None:
    inherited = {
        "ANTHROPIC_CUSTOM_HEADERS": "X-Other: 1\n x-claudex-tier: fast",
        "CLAUDEX_FAST": "1",
    }
    fast = _launch(netns, "--fast", env=inherited)
    env = _settings_env(fast.settings)
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Claudex-Tier: fast"
    assert env["CLAUDEX_FAST"] == "1"
    assert fast.headers == "X-Other: 1"
    assert fast.fast == "<unset>"
    plain = _launch(netns, env=inherited)
    env = _settings_env(plain.settings)
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env
    assert "CLAUDEX_FAST" not in env
    assert plain.headers == "X-Other: 1"
    assert plain.fast == "<unset>"


def test_launcher_clears_inherited_session_env(netns: Namespace) -> None:
    # 从 claudex 会话或带认证变量的 shell 里启动：继承值不得进入新会话，派生 settings
    # 的 env 需要它们时自己给出
    inherited: dict[str, str] = dict.fromkeys(INHERITED_SESSION_ENV, "inherited-value")
    _launch(netns, env=inherited)
    seen = (netns.claude_out / "inherited").read_text(encoding="utf-8").splitlines()
    assert seen == [f"{name}=<unset>" for name in INHERITED_SESSION_ENV]


def test_gateway_start_is_idempotent_and_stop_stops(netns: Namespace) -> None:
    started = netns.run("gateway", "start")
    assert started.returncode == 0
    assert "curl:" not in started.stderr
    pid = _gateway_pid()
    assert _mode(paths.gateway_pid_file()) == 0o600
    assert netns.run("gateway", "start").returncode == 0
    assert _gateway_pid() == pid
    starts = [
        line
        for line in netns.gateway_log.read_text(encoding="utf-8").splitlines()
        if line.startswith("start ")
    ]
    state = paths.state_dir()
    assert starts == [f"start cwd={state} writable={state}"]
    status = netns.run("status")
    assert status.returncode == 0
    assert f"gateway process: pid {pid}," in status.stdout

    stopped = netns.run("gateway", "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert not Path(f"/proc/{pid}").exists()
    assert not paths.gateway_pid_file().exists()
    assert netns.run("gateway", "stop").returncode == 0


def test_gateway_start_fails_when_gateway_never_healthy(netns: Namespace) -> None:
    # 受管升级的 worker 以 `gateway start` 的退出码判新网关是否健康。启动器的健康检查
    # 预算是 40 次、间隔 0.25 秒，本用例要等满这段时间（10 秒以上）才能看到失败
    result = netns.run("gateway", "start", env={"FAKE_GATEWAY_UNHEALTHY": "1"})
    assert result.returncode != 0
    assert "failed to become healthy" in result.stderr
    # 轮询期间的 curl 报错不写终端，只在最终失败时随原因报一次
    assert result.stderr.count("curl:") == 1
    assert "503" in result.stderr
    pid = _gateway_pid()
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{pid}").exists()
    requests = netns.gateway_log.read_text(encoding="utf-8").splitlines()
    assert "GET /v1/models auth=yes" in requests


def test_gateway_start_refuses_when_port_check_fails(
    netns: Namespace, tmp_path: Path
) -> None:
    broken = tmp_path / "broken-ss"
    broken.mkdir()
    _write_executable(broken / "ss", "#!/usr/bin/env bash\nexit 1\n")
    result = netns.run(
        "gateway", "start", env={"PATH": f"{broken}:{netns.env['PATH']}"}
    )
    assert result.returncode != 0
    assert "cannot tell whether port 8317 is free" in result.stderr
    assert not paths.gateway_pid_file().exists()


def test_healthcheck_keeps_client_key_out_of_argv(netns: Namespace) -> None:
    assert netns.run("gateway", "start").returncode == 0
    argv = netns.curl_argv.read_text(encoding="utf-8").splitlines()
    client_key = paths.client_key_file().read_text(encoding="utf-8").strip()
    assert argv
    assert not [line for line in argv if client_key in line]
    assert "--config" in argv
    assert "-" in argv


def _add_model_e() -> None:
    text = paths.config_file().read_text(encoding="utf-8")
    paths.config_file().write_text(
        text.replace(
            '"vendor/model-c", "vendor/model-d"]',
            '"vendor/model-c", "vendor/model-d", "vendor/model-e"]',
        ),
        encoding="utf-8",
    )
    catalog.save_catalog(_catalog("vendor/model-e"))


def test_changed_config_waits_for_delayed_registration(netns: Namespace) -> None:
    started = netns.run("gateway", "start", env={"FAKE_GATEWAY_RELOAD_DELAY": "1.5"})
    assert started.returncode == 0, started.stderr
    _add_model_e()
    launch = _launch(netns, "--opus", "or/vendor/model-e")
    assert _settings_env(launch.settings)["ANTHROPIC_DEFAULT_OPUS_MODEL"] == (
        "or/vendor-model-e"
    )


def test_changed_config_fails_when_models_never_register(netns: Namespace) -> None:
    started = netns.run("gateway", "start", env={"FAKE_GATEWAY_RELOAD_DELAY": "never"})
    assert started.returncode == 0, started.stderr
    _add_model_e()
    result = netns.run("--opus", "or/vendor/model-e")
    assert result.returncode == 1
    # 报错来自等待注册（而不是随后的渲染），并提示受管重启
    assert "仍未注册 ['or/vendor-model-e']" in result.stderr
    assert "claudex gateway restart" in result.stderr
    assert not (netns.claude_out / "argv").exists()
