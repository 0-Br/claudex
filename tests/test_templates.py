"""起步文件：包内 `templates/` 与仓库 `examples/` 逐字相同，且模板本身能通过校验。"""

import json
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from claudex import config, gateway

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
STARTER_FILES = ("claudex.toml", "gateway.base.yaml", "settings.base.json")


def _template_text(name: str) -> str:
    return (resources.files("claudex") / "templates" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", STARTER_FILES)
def test_example_matches_packaged_template(name: str) -> None:
    assert (EXAMPLES_DIR / name).read_text(encoding="utf-8") == _template_text(name)


def test_examples_dir_holds_only_starter_files() -> None:
    assert sorted(p.name for p in EXAMPLES_DIR.iterdir()) == sorted(STARTER_FILES)


def test_claudex_toml_template_parses() -> None:
    parsed = config.parse_config(tomllib.loads(_template_text("claudex.toml")))
    assert parsed.default_profile in parsed.profiles
    # 起步配置缺省不屏蔽任何 MCP，示例写成注释
    assert parsed.mcp_deny == ()


def test_gateway_base_template_passes_forbidden_key_check(tmp_path: Path) -> None:
    base_file = tmp_path / "gateway.base.yaml"
    base_file.write_text(_template_text("gateway.base.yaml"), encoding="utf-8")
    base = gateway.load_gateway_base(base_file)
    assert not set(base) & set(gateway.FORBIDDEN_BASE_KEYS)
    assert base["disable-image-generation"] == "chat"


def test_settings_base_template_is_plain_json() -> None:
    assert isinstance(json.loads(_template_text("settings.base.json")), dict)
