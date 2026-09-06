"""Nested value, metadata, and generated-row projection helpers."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .validation import (
    StateValidationError,
    _CHANNELS,
    _direction,
    _fail,
    _mapping,
    _selected_variables,
    _validate_variable,
)


_PATH_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_leaf_path(path: str) -> tuple[str | int, ...]:
    """Parse one TESSY leaf path into identifier and array-index tokens."""

    if not isinstance(path, str) or not path:
        _fail("leaf path must be a non-empty string")
    tokens: list[str | int] = []
    position = 0
    match = _PATH_IDENTIFIER.match(path, position)
    if match is None:
        _fail(f"invalid leaf path: {path!r}")
    tokens.append(match.group())
    position = match.end()
    while position < len(path):
        marker = path[position]
        if marker == ".":
            position += 1
            match = _PATH_IDENTIFIER.match(path, position)
            if match is None:
                _fail(f"invalid leaf path: {path!r}")
            tokens.append(match.group())
            position = match.end()
            continue
        if marker == "[":
            end = path.find("]", position + 1)
            if end < 0 or end == position + 1:
                _fail(f"invalid leaf path: {path!r}")
            index = path[position + 1:end]
            if re.fullmatch(r"[0-9]+", index) is None:
                _fail(f"invalid leaf path: {path!r}")
            tokens.append(int(index))
            position = end + 1
            continue
        _fail(f"invalid leaf path: {path!r}")
    return tuple(tokens)


def flatten_values(value: dict, metadata: Mapping[str, Any], channel: str | None = None) -> dict[str, str]:
    """Flatten nested JSON values to TESSY-style leaf paths."""

    variables = metadata_variables(metadata)
    selected = _selected_variables(variables, channel)
    _schema_leaves(selected)
    value = _mapping(value, "value")
    result: dict[str, str] = {}
    for name, spec in selected.items():
        if name not in value:
            _fail(f"value is missing variable {name!r}")
        _flatten_node(value[name], spec, name, result)
    if set(value) - set(selected):
        _fail("value contains a variable outside metadata")
    return result


def nest_values(flat_values: Mapping[str, str], metadata: Mapping[str, Any], channel: str | None = None) -> dict:
    """Rebuild nested JSON values from TESSY-style leaf paths."""

    flat = _mapping(flat_values, "flat_values")
    if any(not isinstance(key, str) for key in flat):
        _fail("flat value paths must be strings")
    variables = metadata_variables(metadata)
    selected = _selected_variables(variables, channel)
    _schema_leaves(selected)
    result: dict[str, Any] = {}
    consumed: set[str] = set()
    for name, spec in selected.items():
        result[name] = _nest_node(flat, spec, name, consumed)
    extras = set(flat) - consumed
    if extras:
        _fail(f"flat values contain unknown paths: {sorted(extras)!r}")
    return result


def sync_row_schema(row: dict, old_variables: Mapping[str, Any], new_variables: Mapping[str, Any]) -> None:
    """Project one row onto a changed variable schema without changing old lexemes."""

    row = _mapping(row, "row")
    old = metadata_variables(old_variables)
    new = metadata_variables(new_variables)
    for channel in _CHANNELS:
        old_selected = _selected_variables(old, channel)
        new_selected = _selected_variables(new, channel)
        old_leaves = _schema_leaves(old_selected)
        new_leaves = _schema_leaves(new_selected)
        old_values = _read_schema_values(row.get(channel, {}), old_leaves)
        projected = {
            path: old_values[path]
            for path, spec in new_leaves.items()
            if path in old_leaves
            and old_leaves[path][1] == spec[1]
            and path in old_values
        }
        projected.update({path: "0" for path in new_leaves if path not in projected})
        row[channel] = _nest_leaves(projected, new_selected)


def project_generated_values(values, metadata, channel: str) -> dict:
    """Project generated flat values into the validated nested state shape."""

    variables = metadata_variables(metadata)
    selected = _selected_variables(variables, channel)
    if not selected:
        return {}
    flat = _generated_flat_values(values, metadata, selected, channel)
    return nest_values(flat, variables, channel)


def metadata_variables(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    """Convert FunctionIR/Inventory leaves or plain metadata into state specs."""

    return _metadata_variables(metadata)


def _flatten_node(value: object, spec: Mapping[str, Any], path: str, result: dict[str, str], index: int = 0) -> None:
    dimensions = spec.get("dimensions", [])
    if index < len(dimensions):
        if not isinstance(value, list) or len(value) != dimensions[index]:
            _fail(f"{path} has an invalid array length/shape")
        for item_index, item in enumerate(value):
            _flatten_node(item, spec, f"{path}[{item_index}]", result, index + 1)
        return
    fields = spec.get("fields")
    if fields is not None:
        if not isinstance(value, dict) or set(value) != set(fields):
            _fail(f"{path} has an invalid struct shape")
        for name, child in fields.items():
            _flatten_node(value[name], child, f"{path}.{name}", result)
        return
    if not isinstance(value, str):
        _fail(f"{path} must preserve a string C lexeme")
    result[path] = value


def _nest_node(flat: Mapping[str, Any], spec: Mapping[str, Any], path: str, consumed: set[str], index: int = 0) -> Any:
    dimensions = spec.get("dimensions", [])
    if index < len(dimensions):
        return [
            _nest_node(flat, spec, f"{path}[{item_index}]", consumed, index + 1)
            for item_index in range(dimensions[index])
        ]
    fields = spec.get("fields")
    if fields is not None:
        return {
            name: _nest_node(flat, child, f"{path}.{name}", consumed)
            for name, child in fields.items()
        }
    if path not in flat or not isinstance(flat[path], str):
        _fail(f"flat values are missing leaf {path!r}")
    consumed.add(path)
    return flat[path]


def _metadata_variables(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    if hasattr(metadata, "leaves"):
        return _variables_from_leaves(
            getattr(metadata, "leaves"), getattr(metadata, "stubs", ())
        )
    if not isinstance(metadata, Mapping) and hasattr(metadata, "variables"):
        return _variables_from_leaves(
            getattr(metadata, "variables"), getattr(metadata, "stubs", ())
        )
    if isinstance(metadata, (list, tuple)):
        return _variables_from_leaves(metadata)
    return _mapping(metadata, "metadata")


def _schema_leaves(variables: Mapping[str, Any]) -> dict[str, tuple[Mapping[str, Any], tuple]]:
    leaves: dict[str, tuple[Mapping[str, Any], tuple]] = {}
    for name, spec in variables.items():
        if not isinstance(name, str) or not name:
            _fail("variable names must be non-empty strings")
        _validate_variable(spec, f"metadata.{name}", root=True)
        _metadata_path_tokens(name)
        _collect_schema_leaves(spec, name, leaves, context=())
    return leaves


def _collect_schema_leaves(
    spec: Mapping[str, Any],
    path: str,
    leaves: dict[str, tuple[Mapping[str, Any], tuple]],
    index: int = 0,
    context: tuple = (),
) -> None:
    dimensions = spec.get("dimensions", [])
    if index < len(dimensions):
        for item_index in range(dimensions[index]):
            _collect_schema_leaves(
                spec, f"{path}[{item_index}]", leaves, index + 1, context
            )
        return
    fields = spec.get("fields")
    if fields is not None:
        context = context + (_node_signature(spec),)
        for name, child in fields.items():
            _collect_schema_leaves(child, f"{path}.{name}", leaves, context=context)
        return
    if path in leaves:
        _fail(f"metadata contains a conflicting leaf path: {path!r}")
    _metadata_path_tokens(path)
    leaves[path] = (spec, context + (_node_signature(spec),))


def _read_schema_values(
    value: object,
    leaves: Mapping[str, tuple[Mapping[str, Any], tuple]],
) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for path in leaves:
        try:
            tokens = _metadata_path_tokens(path)
        except StateValidationError:
            continue
        current: object = value
        for token in tokens:
            if isinstance(token, int):
                if not isinstance(current, list) or token >= len(current):
                    current = None
                    break
                current = current[token]
            else:
                if not isinstance(current, dict) or token not in current:
                    current = None
                    break
                current = current[token]
        if isinstance(current, str):
            result[path] = current
    return result


def _nest_leaves(flat_values: Mapping[str, str], variables: Mapping[str, Any]) -> dict:
    result: dict[str, Any] = {}
    consumed: set[str] = set()
    for name, spec in variables.items():
        result[name] = _nest_node(flat_values, spec, name, consumed)
    extras = set(flat_values) - consumed
    if extras:
        _fail(f"flat values contain unknown paths: {sorted(extras)!r}")
    return result


def _node_signature(spec: Mapping[str, Any]) -> tuple:
    type_name = spec.get("type")
    return (
        None if type_name in {None, "", "unknown"} else type_name,
        _direction(spec.get("direction")),
        bool(spec.get("pointer", spec.get("is_pointer", False))),
        _base_format(spec.get("format", spec.get("fmt"))),
        spec.get("bit_length", spec.get("bits", spec.get("xml_len"))),
        spec.get("bits"),
        spec.get("xml_len"),
        spec.get("signed"),
        _json_native(spec.get("enum_values", ())),
        bool(spec.get("stub_return", False)),
    )


def _metadata_path_tokens(path: str) -> tuple[str | int, ...]:
    """Parse normal paths and opaque roots used by existing IR leaf names."""

    try:
        return parse_leaf_path(path)
    except StateValidationError:
        root_end = len(path)
        for marker in ".[":
            index = path.find(marker)
            if index >= 0:
                root_end = min(root_end, index)
        root = path[:root_end]
        if not root or not (
            root.startswith("@") or any(mark in root for mark in ("()", "::", "#"))
        ):
            raise
        suffix = path[root_end:]
        if not suffix:
            return (root,)
        tokens = parse_leaf_path(f"_root{suffix}")
        return (root, *tokens[1:])


def _variables_from_leaves(leaves: object, stubs: object = ()) -> dict[str, dict[str, Any]]:
    if not isinstance(leaves, (list, tuple)):
        _fail("metadata leaves must be a sequence")
    stub_roots = _stub_return_roots(stubs)
    parsed: list[tuple[object, str, tuple[str | int, ...], str]] = []
    names: set[str] = set()
    paths: set[tuple[str | int, ...]] = set()
    for leaf in leaves:
        if not _is_variable(leaf):
            _fail("metadata leaves must contain Variable metadata")
        name = getattr(leaf, "name")
        if not isinstance(name, str) or not name:
            _fail("metadata leaf names must be non-empty strings")
        if name in names:
            _fail(f"metadata contains a duplicate leaf path: {name!r}")
        names.add(name)
        direction = _direction(getattr(leaf, "direction"))
        if direction is None:
            _fail(f"metadata leaf has invalid direction: {name!r}")
        try:
            tokens = _metadata_path_tokens(name)
        except StateValidationError:
            _fail(f"metadata leaf has an invalid path: {name!r}")
        if tokens in paths:
            _fail(f"metadata contains a duplicate leaf path: {name!r}")
        paths.add(tokens)
        parsed.append((leaf, name, tokens, direction))
    for index, (_, left_name, left, _) in enumerate(parsed):
        for _, right_name, right, _ in parsed[index + 1:]:
            _validate_leaf_path_pair(left_name, left, right_name, right)

    result: dict[str, dict[str, Any]] = {}
    for leaf, name, tokens, direction in parsed:
        root = tokens[0]
        spec = result.setdefault(root, {"type": "", "direction": direction})
        if spec.get("direction") != direction:
            _fail(f"metadata leaf has conflicting directions: {name!r}")
        _merge_leaf_metadata(spec, tokens[1:], leaf, root in stub_roots)
    return result


def _stub_return_roots(stubs: object) -> set[str]:
    if stubs is None:
        return set()
    if not isinstance(stubs, (list, tuple)):
        _fail("metadata stubs must be a sequence")
    roots = set()
    for stub in stubs:
        name = getattr(stub, "name", None)
        if not isinstance(name, str) or not name:
            _fail("metadata stubs must contain named stub metadata")
        roots.add(f"tessy_{name}")
    return roots


def _validate_leaf_path_pair(
    left_name: str,
    left: tuple[str | int, ...],
    right_name: str,
    right: tuple[str | int, ...],
) -> None:
    common = min(len(left), len(right))
    for index in range(common):
        left_token, right_token = left[index], right[index]
        if left_token == right_token:
            continue
        if isinstance(left_token, int) and isinstance(right_token, int):
            _fail(f"metadata contains conflicting array extents: {left_name!r}, {right_name!r}")
        if isinstance(left_token, int) != isinstance(right_token, int):
            _fail(f"metadata contains conflicting leaf paths: {left_name!r}, {right_name!r}")
        return
    if len(left) == len(right):
        _fail(f"metadata contains duplicate leaf paths: {left_name!r}, {right_name!r}")
    _fail(f"metadata contains conflicting leaf paths: {left_name!r}, {right_name!r}")


def _merge_leaf_metadata(
    spec: dict[str, Any],
    suffix: tuple[str | int, ...],
    leaf: object,
    stub_return: bool,
) -> None:
    ctype = getattr(leaf, "ctype")
    leaf_format = getattr(leaf, "fmt", "")
    ctype_bits = getattr(ctype, "bits", None)
    leaf_spec = {
        "type": getattr(ctype, "name", "") or "unknown",
        "format": leaf_format,
        "xml_len": getattr(leaf, "xml_len", ""),
        "bits": ctype_bits,
        "bit_length": ctype_bits if ctype_bits is not None else getattr(leaf, "xml_len", ""),
        "signed": getattr(ctype, "signed", None),
        "enum_values": _json_native(getattr(ctype, "enum_values", ())),
        "pointer": bool(getattr(leaf, "is_pointer", False)),
        "stub_return": stub_return,
    }
    if not spec.get("type"):
        base_format = _base_format(leaf_format)
        spec.update({
            "type": base_format if any(isinstance(token, str) for token in suffix)
            and base_format else leaf_spec["type"],
            "stub_return": stub_return,
        })
    if not suffix:
        if spec.get("fields") or spec.get("dimensions"):
            _fail(f"metadata contains conflicting leaf shape: {getattr(leaf, 'name', '')!r}")
        if any(
            key in spec and spec[key] != value
            for key, value in leaf_spec.items()
            if key != "type"
        ):
            _fail(f"metadata contains conflicting leaf type: {getattr(leaf, 'name', '')!r}")
        spec.update(leaf_spec)
        return
    node = spec
    position = 0
    array_depth = 0
    while position < len(suffix):
        token = suffix[position]
        if isinstance(token, int):
            dimensions = node.setdefault("dimensions", [])
            if len(dimensions) <= array_depth:
                dimensions.append(token)
            elif dimensions[array_depth] != token:
                _fail(f"metadata contains conflicting array shape: {getattr(leaf, 'name', '')!r}")
            array_depth += 1
            position += 1
            continue
        if "format" in node:
            _fail(f"metadata contains conflicting leaf shape: {getattr(leaf, 'name', '')!r}")
        fields = node.setdefault("fields", {})
        child = fields.setdefault(token, {"type": "unknown"})
        node = child
        array_depth = 0
        position += 1
    if node.get("fields"):
        _fail(f"metadata contains conflicting leaf shape: {getattr(leaf, 'name', '')!r}")
    if any(
        key in node and node[key] != value
        for key, value in leaf_spec.items()
        if key != "type"
    ):
        _fail(f"metadata contains conflicting leaf type: {getattr(leaf, 'name', '')!r}")
    node.update(leaf_spec)


def _generated_flat_values(values, metadata, selected, channel):
    if not isinstance(values, Mapping):
        _fail("generated values must be a mapping")
    leaves = {
        leaf.name: leaf
        for leaf in _metadata_leaves(metadata)
        if _direction(getattr(leaf, "direction", None)) == channel
        or _direction(getattr(leaf, "direction", None)) == "inout"
    }
    schema_paths = list(_schema_leaves(selected))
    leaf_patterns = {
        _array_pattern(name): leaf for name, leaf in leaves.items()
    }
    flat = {}
    for name, spec in selected.items():
        if name in values and name not in leaves:
            _flatten_generated_nested(values[name], spec, name, flat, leaf_patterns)
    for name, value in values.items():
        if name in schema_paths:
            flat[name] = _lexeme(value, leaf_patterns.get(_array_pattern(name)))
    for name, leaf in leaves.items():
        if name in values:
            lexeme = _lexeme(values[name], leaf)
            pattern = _array_pattern(name)
            for path in schema_paths:
                if _array_pattern(path) == pattern:
                    flat[path] = lexeme
    for name in schema_paths:
        flat.setdefault(name, "0")
    return flat


def _metadata_leaves(metadata):
    leaves = getattr(metadata, "leaves", None)
    if leaves is not None:
        return leaves
    return getattr(metadata, "variables", ())


def _flatten_generated_nested(value, spec, path, result, leaf_patterns):
    dimensions = spec.get("dimensions", [])
    if dimensions:
        for index in range(dimensions[0]):
            child = value[index] if isinstance(value, list) and index < len(value) else 0
            _flatten_generated_nested(
                child,
                {**spec, "dimensions": dimensions[1:]},
                f"{path}[{index}]",
                result,
                leaf_patterns,
            )
        return
    fields = spec.get("fields")
    if fields is not None:
        for name, child_spec in fields.items():
            child = value.get(name, 0) if isinstance(value, Mapping) else 0
            _flatten_generated_nested(child, child_spec, f"{path}.{name}", result, leaf_patterns)
        return
    result[path] = _lexeme(value, leaf_patterns.get(_array_pattern(path)))


def _array_pattern(path):
    return re.sub(r"\[[0-9]+\]", "[]", path)


def _lexeme(value, leaf=None):
    if isinstance(value, str):
        return value
    if value is None:
        return "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    if leaf is not None:
        for name, number in getattr(leaf.ctype, "enum_values", ()):
            if number == value:
                return name
    return str(value)


def _is_variable(value: object) -> bool:
    return all(hasattr(value, name) for name in ("name", "ctype", "direction"))


def _base_format(value: object) -> object:
    if not isinstance(value, str):
        return value
    return re.sub(r"(?:\[[0-9]+\])+\Z", "", value)


def _json_native(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return [_json_native(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_native(item) for key, item in value.items()}
    return value


__all__ = [
    "flatten_values",
    "metadata_variables",
    "nest_values",
    "parse_leaf_path",
    "project_generated_values",
    "sync_row_schema",
]
