"""全部测试共用的隔离与本地假服务 fixture。

隔离：HOME、config、state 与临时目录一律指到本用例的 tmp_path 下，并清掉会话继承来的
claudex 变量与代理变量（本地假服务不能被环境代理截走）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_service import FakeService, running_service

# 从调用者环境继承、会改变被测行为的变量
_CLEARED_ENV = (
    "CLAUDEX_PROFILE",
    "CLAUDEX_PROFILE_FILE",
    "CLAUDEX_FAST",
    "CLAUDEX_STATUSLINE_COMMAND",
    "CLAUDEX_NO_REFRESH",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把可写根路径指到临时目录，并清掉会话继承来的 claudex 变量与代理变量。"""
    roots = {
        "HOME": tmp_path / "home",
        "CLAUDEX_CONFIG_DIR": tmp_path / "config",
        "CLAUDEX_STATE_DIR": tmp_path / "state",
        "XDG_STATE_HOME": tmp_path / "xdg-state",
        "TMPDIR": tmp_path / "tmp",
    }
    for name, path in roots.items():
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    for name in _CLEARED_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_service() -> Iterator[FakeService]:
    """127.0.0.1 随机端口上的假 HTTP 服务，见 `fake_service.py`。"""
    with running_service() as service:
        yield service
