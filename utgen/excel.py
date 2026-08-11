from __future__ import annotations

import re
import warnings
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
_C_BODY_FORBIDDEN = re.compile(r"[\r\n;{}]")
_STORAGE_QUALIFIERS = {"auto", "extern", "register", "static", "typedef", "_Thread_local"}


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
class _StubSpec:
    name: str
    return_type: str
    return_name: str
    argument_types: tuple[str, ...]


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


def _stub_text(element, path: Path, label: str) -> str:
    text = element.text
    if text is None or not text.strip():
        raise ValueError(f"{path}: {label} is required")
    if _C_BODY_FORBIDDEN.search(text):
        raise ValueError(f"{path}: {label} contains invalid C body characters")
    return text.strip()


def _stub_identifier(value: str, path: Path, label: str) -> str:
    if _C_BODY_FORBIDDEN.search(value) or not _C_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{path}: {label} must be a valid C identifier")
    return value


def _stub_attribute(element, name: str, path: Path, label: str) -> str:
    value = element.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{path}: {label} is required")
    return _stub_identifier(value, path, label)


def _parse_stub_file(path: Path) -> tuple[_StubSpec, ...]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise ValueError(f"{path}: cannot parse stub XML: {error}") from error
    if root.tag != "function_analysis":
        raise ValueError(f"{path}: root tag must be function_analysis")
    called = root.find("called_functions")
    if called is None:
        raise ValueError(f"{path}: called_functions is required")
    try:
        expected_count = int(_attribute(called, "count", path))
    except ValueError as error:
        raise ValueError(f"{path}: called_functions count must be an integer") from error
    functions = list(called)
    if expected_count != len(functions):
        raise ValueError(f"{path}: declares {expected_count} called functions but contains {len(functions)}")
    stubs = []
    for function in functions:
        if function.tag != "function":
            raise ValueError(f"{path}: called_functions child must be function")
        name = _stub_attribute(function, "name", path, "stub name")
        returns = []
        arguments = []
        for child in function:
            if child.tag == "return":
                returns.append(child)
            elif child.tag == "arguments":
                arguments.append(child)
            else:
                raise ValueError(f"{path}: function child must be return or arguments")
        if len(returns) > 1 or len(arguments) > 1:
            raise ValueError(f"{path}: duplicate stub return or arguments")
        return_type = ""
        return_name = ""
        if returns:
            return_element = returns[0]
            type_element = return_element.find("type")
            name_element = return_element.find("name")
            if type_element is None or name_element is None:
                raise ValueError(f"{path}: stub return definition is incomplete")
            return_type = _stub_text(type_element, path, "stub return type")
            return_name = _stub_identifier(_stub_text(name_element, path, "stub return name"), path,
                                           "stub return name")
        else:
            warnings.warn(f"{path}: stub {name} is missing return data", RuntimeWarning)
        argument_types = []
        if arguments:
            for argument in arguments[0]:
                if argument.tag != "argument":
                    raise ValueError(f"{path}: arguments child must be argument")
                type_element = argument.find("type")
                name_element = argument.find("name")
                if type_element is None or name_element is None:
                    raise ValueError(f"{path}: stub argument definition is incomplete")
                argument_types.append(_stub_text(type_element, path, "stub argument type"))
                _stub_identifier(_stub_text(name_element, path, "stub argument name"), path, "stub argument name")
        stubs.append(_StubSpec(name, return_type, return_name, tuple(argument_types)))
    return tuple(stubs)


def _stub_catalog(paths: Sequence[Path]) -> tuple[_StubSpec, ...]:
    catalog = []
    by_name = {}
    for path in paths:
        for stub in _parse_stub_file(path):
            previous = by_name.get(stub.name)
            if previous is None:
                by_name[stub.name] = stub
                catalog.append(stub)
            elif previous != stub:
                raise ValueError(f"{path}: conflicting stub definition for {stub.name}")
    return tuple(catalog)


def _is_void(return_type: str) -> bool:
    words = return_type.split()
    while words and words[0] in _STORAGE_QUALIFIERS:
        words.pop(0)
    return words == ["void"]


def _stub_body(stub: _StubSpec, value: _ValueSpec) -> str:
    if not stub.return_type or _is_void(stub.return_type):
        return ""
    for argument in value.arguments:
        if argument.name == stub.return_name or argument.name.startswith(f"{stub.return_name}.") or argument.name.startswith(f"{stub.return_name}["):
            return f"extern {stub.return_type} {stub.return_name};\rreturn {stub.return_name};\r"
    return f"{stub.return_type} temp = {{0}};\rreturn temp;\r"


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
