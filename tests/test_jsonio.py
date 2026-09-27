"""claudex.jsonio：原子写、权限、内容不变不重写与读取的错误形态。"""

import json
import stat
from pathlib import Path

import pytest

from claudex import jsonio


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_creates_file_with_mode_and_canonical_text(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    assert jsonio.write_json_atomic(target, {"b": 1, "a": "中"}) is True
    assert target.read_text(encoding="utf-8") == '{\n  "a": "中",\n  "b": 1\n}\n'
    assert _mode(target) == 0o600


def test_write_honours_custom_mode(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    jsonio.write_json_atomic(target, [1], mode=0o644)
    assert _mode(target) == 0o644


def test_write_creates_missing_parent_with_private_mode(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "out.json"
    jsonio.write_json_atomic(target, {})
    assert json.loads(target.read_text(encoding="utf-8")) == {}
    assert _mode(target.parent) == 0o700


def test_identical_content_is_not_rewritten(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    jsonio.write_json_atomic(target, {"a": 1})
    before = target.stat().st_ino, target.stat().st_mtime_ns
    assert jsonio.write_json_atomic(target, {"a": 1}) is False
    assert (target.stat().st_ino, target.stat().st_mtime_ns) == before


def test_changed_content_replaces_file_without_leftovers(tmp_path: Path) -> None:
    target = tmp_path / "data" / "out.json"
    jsonio.write_json_atomic(target, {"a": 1})
    assert jsonio.write_json_atomic(target, {"a": 2}) is True
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert sorted(p.name for p in target.parent.iterdir()) == ["out.json"]


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert jsonio.read_json_object(tmp_path / "missing.json") is None


def test_read_returns_object(tmp_path: Path) -> None:
    target = tmp_path / "in.json"
    target.write_text('{"k": [1, 2]}', encoding="utf-8")
    assert jsonio.read_json_object(target) == {"k": [1, 2]}


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"{broken", "JSON"),
        (b"\xff\xfe", "JSON"),
        (b"[1, 2]", "list"),
        (b'"text"', "str"),
    ],
)
def test_read_rejects_invalid_or_non_object(
    raw: bytes, reason: str, tmp_path: Path
) -> None:
    target = tmp_path / "bad.json"
    target.write_bytes(raw)
    with pytest.raises(ValueError, match=reason) as caught:
        jsonio.read_json_object(target)
    assert str(target) in str(caught.value)
