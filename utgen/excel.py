from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

DEFAULT_TEMPLATE_PATH = Path(r"D:\TestTeamAssets\common_tools\v2\template.xlsx")
XLSX_VARIABLE_LIMIT = 14_000
XLS_VARIABLES_PER_SHEET = 98
_DIRECTIONS = {"in", "out", "inout"}
_C_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class _ValueArgument:
    name: str
    length: str
    fmt: str
    direction: str
    pointer: bool
    values: tuple[str, ...]


@dataclass(frozen=True)
class _ValueSpec:
    path: Path
    function: str
    arguments: tuple[_ValueArgument, ...]
    row_count: int


def _attribute(element, name, path):
    value = element.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{path}: missing attribute {name}")
    return value.strip()


def _parse_value(path: Path) -> _ValueSpec:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise ValueError(f"{path}: cannot parse value XML: {error}") from error
    if root.tag != "function":
        raise ValueError(f"{path}: root tag must be function")
    function = (root.get("name") or "").strip()
    if not function:
        raise ValueError(f"{path}: function name is required")
    if not _C_IDENTIFIER.fullmatch(function):
        raise ValueError(f"{path}: function name must be a valid C identifier")
    arguments = []
    names = set()
    row_count = None
    for element in root:
        if element.tag != "argument":
            raise ValueError(f"{path}: direct child must be argument")
        name = _attribute(element, "name", path)
        if name in names:
            raise ValueError(f"{path}: duplicate argument {name}")
        names.add(name)
        length = _attribute(element, "len", path)
        fmt = _attribute(element, "format", path)
        direction = _attribute(element, "direction", path)
        pointer_text = _attribute(element, "pointer", path)
        if direction not in _DIRECTIONS:
            raise ValueError(f"{path}: invalid direction {direction}")
        if pointer_text not in ("true", "false"):
            raise ValueError(f"{path}: invalid pointer {pointer_text}")
        values = ()
        if direction in ("in", "inout"):
            if element.get("testmethod") != "decision":
                raise ValueError(f"{path}: {name} requires testmethod=decision")
            text = element.get("range")
            if text is None or not text.strip():
                raise ValueError(f"{path}: {name} requires a non-empty range")
            parsed = []
            for token in text.split(","):
                token = token.strip()
                if not token:
                    raise ValueError(f"{path}: {name} contains an empty range token")
                parsed.append(token)
            values = tuple(parsed)
            if row_count is None:
                row_count = len(values)
            elif len(values) != row_count:
                raise ValueError(f"{path}: {name} range has {len(values)} rows; expected {row_count}")
        arguments.append(_ValueArgument(name, length, fmt, direction,
                                        pointer_text == "true", values))
    return _ValueSpec(path, function, tuple(arguments), row_count or 1)
