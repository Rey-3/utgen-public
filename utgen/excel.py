from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from xml.etree import ElementTree as ET

DEFAULT_TEMPLATE_PATH = Path(r"D:\TestTeamAssets\common_tools\v2\template.xlsx")
XLSX_VARIABLE_LIMIT = 14_000
XLS_VARIABLES_PER_SHEET = 98
_DIRECTIONS = {"in", "out", "inout"}
_C_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DIMENSION = re.compile(r"\[([^]]*)\]")


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


@dataclass(frozen=True)
class _Column:
    name: str
    declaration: str
    values: tuple[str, ...]


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


def _expanded_names(name: str) -> tuple[str, ...]:
    dimensions = []
    for match in _DIMENSION.finditer(name):
        text = match.group(1)
        if not text.isdecimal() or int(text) <= 0:
            raise ValueError(f"invalid array dimension in {name}")
        dimensions.append(int(text))
    remainder = _DIMENSION.sub("", name)
    if "[" in remainder or "]" in remainder:
        raise ValueError(f"invalid array dimension in {name}")
    if not dimensions:
        return (name,)
    ranges = []
    for size in dimensions:
        ranges.append(range(size))
    expanded = []
    for indexes in product(*ranges):
        current = iter(indexes)
        expanded.append(_DIMENSION.sub(lambda match: f"[{next(current)}]", name))
    return tuple(expanded)


def _pointer_root(name: str) -> str:
    return name.split(".", 1)[0].split("[", 1)[0]


def _pointer_groups(arguments: Sequence[_ValueArgument]) -> dict[str, list[_ValueArgument]]:
    groups = {}
    for argument in arguments:
        if argument.pointer:
            groups.setdefault(_pointer_root(argument.name), []).append(argument)
    return groups


def _validate_pointer_groups(groups: dict[str, list[_ValueArgument]], row_count: int) -> None:
    for root, arguments in groups.items():
        formats = set()
        for argument in arguments:
            formats.add(argument.fmt)
        if len(formats) != 1:
            raise ValueError(f"pointer {root} has incompatible formats")
        for row in range(row_count):
            null_states = set()
            for argument in arguments:
                if argument.direction != "out":
                    null_states.add(argument.values[row] == "NULL")
            if len(null_states) > 1:
                raise ValueError(f"pointer {root} has inconsistent NULL state")


def _storage_name(root: str, name: str) -> str:
    return f"tessy_{root}{name[len(root):]}"


def _pointer_values(argument: _ValueArgument, row_count: int, marker: str) -> tuple[str, ...]:
    if marker == "o":
        return ("",) * row_count
    values = []
    for value in argument.values:
        values.append("0" if value == "NULL" else value)
    return tuple(values)


def _plan_columns(spec: _ValueSpec) -> tuple[_Column, ...]:
    groups = _pointer_groups(spec.arguments)
    _validate_pointer_groups(groups, spec.row_count)
    columns = []
    address_roots = set()
    storage_roots = set()
    for marker, directions in (("i", {"in", "inout"}), ("o", {"out", "inout"})):
        handled_roots = set()
        for argument in spec.arguments:
            if argument.direction not in directions:
                continue
            if not argument.pointer:
                values = argument.values if marker == "i" else ("",) * spec.row_count
                for name in _expanded_names(argument.name):
                    columns.append(_Column(name, marker, values))
                continue
            root = _pointer_root(argument.name)
            if root in handled_roots:
                continue
            handled_roots.add(root)
            group = groups[root]
            if root not in address_roots:
                values = ("",) * spec.row_count
                for candidate in group:
                    if candidate.direction in ("in", "inout"):
                        address_values = []
                        for value in candidate.values:
                            address_values.append("NULL" if value == "NULL" else f"&tessy_{root}")
                        values = tuple(address_values)
                        break
                columns.append(_Column(root, marker, values))
                address_roots.add(root)
            for candidate in group:
                if candidate.direction not in directions:
                    continue
                values = _pointer_values(candidate, spec.row_count, marker)
                for name in _expanded_names(candidate.name):
                    declaration = candidate.fmt if root not in storage_roots else marker
                    columns.append(_Column(_storage_name(root, name), declaration, values))
                    storage_roots.add(root)
    return tuple(columns)


def _extension(total_variables: int) -> str:
    return ".xlsx" if total_variables <= XLSX_VARIABLE_LIMIT else ".xls"


def _partition_columns(columns: Sequence[_Column]) -> tuple[tuple[_Column, ...], ...]:
    parts = []
    for start in range(0, len(columns), XLS_VARIABLES_PER_SHEET):
        parts.append(tuple(columns[start:start + XLS_VARIABLES_PER_SHEET]))
    return tuple(parts)
