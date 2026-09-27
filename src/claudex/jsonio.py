"""JSON 文件的原子写入与读取；claudex 写 JSON 状态与快照只经这里。"""

import contextlib
import json
import os
import tempfile
from pathlib import Path


def write_json_atomic(path: Path, obj: object, *, mode: int = 0o600) -> bool:
    """把 `obj` 序列化后原子写到 `path`，返回是否真的写了。

    参数
    ----------
    path : Path
        目标文件；父目录不存在时以 0700 创建。
    obj : object
        可 JSON 序列化的对象；按 `ensure_ascii=False`、`indent=2`、`sort_keys=True`
        序列化，末尾带换行。
    mode : int
        新文件的权限位。

    返回
    ----------
    bool
        现有文件内容与序列化结果逐字节相同时不重写，返回 False；否则写入并返回 True。

    说明
    ----------
    先写同目录下的临时文件并设好权限，再 `os.replace` 覆盖目标，读者不会看到写了一半
    的文件。内容相同而权限不同时同样不重写。
    """
    content = (
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.read_bytes() == content:
            return False
    except FileNotFoundError:
        pass
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary: Path | None = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        assert temporary is not None
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
    return True


def read_json_object(path: Path) -> dict[str, object] | None:
    """读取顶层为对象的 JSON 文件。

    返回
    ----------
    dict[str, object] | None
        解析出的对象；文件不存在时为 None。

    异常
    ----------
    ValueError
        内容不是合法的 UTF-8 JSON，或顶层不是对象；消息含路径与原因。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"{path}: 不是有效的 JSON：{err}") from err
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: JSON 顶层必须是对象，收到 {type(parsed).__name__}")
    return parsed
