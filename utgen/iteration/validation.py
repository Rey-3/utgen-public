"""Validation for the plain-dict state of one source module."""

from __future__ import annotations

import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping


class StateValidationError(ValueError):
    """Raised when module state cannot be safely read or published."""


_SAFE_COMPONENT = re.compile(r"[^/\\:\x00]+\Z")
_ROW_NUMBER = re.compile(r"(?P<prefix>.+)_(?P<number>[0-9]{3,})\Z")
_DECISION_KEY = re.compile(r"D(?P<number>[1-9][0-9]*)\Z")
_ATOM_KEY = re.compile(r"A(?P<number>[1-9][0-9]*)\Z")
_FORBIDDEN_KEYS = frozenset({
    "decision_hash",
    "recipe_hash",
    "generated_row_hash",
    "anchor_hash",
    "BASE",
    "WORKING",
    "NEW",
    "revision",
    "revisions",
    "tombstone",
    "tombstones",
    "conflict",
    "conflicts",
})
_TOP_LEVEL_KEYS = frozenset({
    "schema_version",
    "project",
    "module",
    "source_file",
    "module_input_hash",
    "max_row_number",
    "stub_catalog",
    "functions",
})
_FUNCTION_KEYS = frozenset({
    "max_decision_number",
    "variables",
    "decisions",
    "generated_rows",
    "rows",
    "used_stub_keys",
    "stubs",
})
_ROW_KEYS = frozenset({"owner", "status", "description", "test_method", "input", "output"})
_DECISION_REQUIRED_KEYS = frozenset({"status", "expression", "atoms", "recipes"})
_DECISION_OPTIONAL_KEYS = frozenset({"kind", "location"})
_RECIPE_KEYS = frozenset({"row_id", "result", "values"})
_GENERATED_ROW_KEYS = frozenset({"row_id", "kind"})
_STUB_CATALOG_KEYS = frozenset({
    "stub_key",
    "status",
    "function_name",
    "return_type",
    "return_storage",
    "parameters",
})
_STUB_KEYS = frozenset({"owner", "status", "body"})
_DIRECTIONS = frozenset({"input", "output", "inout"})
_CHANNELS = frozenset({"input", "output"})
_GENERATED_KINDS = frozenset({"Nominal", "Timeout"})


def validate_module(state: dict) -> None:
    """Validate the complete plain-dict module state in memory."""

    _check_json_tree(state, "state")
    _mapping(state, "state")
    _keys(state, _TOP_LEVEL_KEYS, "state")
    if state.get("schema_version") != 1:
        _fail("state.schema_version must be 1")
    _safe_component(state.get("project"), "state.project")
    _safe_component(state.get("module"), "state.module")
    _source_file(state.get("source_file"))
    if not _string(state.get("module_input_hash")):
        _fail("state.module_input_hash must be a non-empty string")
    max_row_number = state.get("max_row_number")
    if type(max_row_number) is not int or max_row_number < 0:
        _fail("state.max_row_number must be a non-negative integer")

    catalog = _mapping(state.get("stub_catalog"), "state.stub_catalog")
    _validate_stub_catalog(catalog)
    functions = _mapping(state.get("functions"), "state.functions")

    prefix = _row_prefix(state)
    rows: dict[str, tuple[str, dict]] = {}
    max_existing = 0
    for function_name, function in functions.items():
        if not _string(function_name):
            _fail("function names must be non-empty strings")
        function = _validate_function(function, function_name, catalog)
        for row_id, row in function["rows"].items():
            number = _validate_row_id(row_id, prefix)
            if row_id in rows:
                _fail(f"duplicate row ID: {row_id}")
            rows[row_id] = (function_name, row)
            max_existing = max(max_existing, number)

    if max_row_number < max_existing:
        _fail("state.max_row_number is below an existing row ID")

    links: dict[str, str] = {}
    for function_name, function in functions.items():
        _validate_function_links(function, function_name, links)

    for function in functions.values():
        _validate_return_storage(function, catalog)


def _validate_function(
    function: object,
    function_name: str,
    catalog: Mapping[str, Any],
) -> dict:
    function = _mapping(function, f"functions.{function_name}")
    _keys(function, _FUNCTION_KEYS, f"functions.{function_name}")
    maximum = function["max_decision_number"]
    if type(maximum) is not int or maximum < 0:
        _fail(f"functions.{function_name}.max_decision_number must be non-negative")
    variables = _mapping(function["variables"], f"functions.{function_name}.variables")
    for name, spec in variables.items():
        if not _string(name):
            _fail(f"functions.{function_name} has an invalid variable name")
        _validate_variable(spec, f"functions.{function_name}.variables.{name}", root=True)

    decisions = _mapping(function["decisions"], f"functions.{function_name}.decisions")
    highest_decision = 0
    for key, decision in decisions.items():
        match = _DECISION_KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None:
            _fail(f"invalid Decision key: {key!r}")
        highest_decision = max(highest_decision, int(match.group("number")))
        _validate_decision(decision, f"functions.{function_name}.decisions.{key}")
    if maximum < highest_decision:
        _fail(f"functions.{function_name}.max_decision_number is below an existing Decision key")

    rows = _mapping(function["rows"], f"functions.{function_name}.rows")
    for row_id, row in rows.items():
        _validate_row(row, variables, f"functions.{function_name}.rows.{row_id}")

    generated_rows = function["generated_rows"]
    if not isinstance(generated_rows, list):
        _fail(f"functions.{function_name}.generated_rows must be a list")
    for item in generated_rows:
        item = _mapping(item, f"functions.{function_name}.generated_rows")
        _keys(item, _GENERATED_ROW_KEYS, "generated row")
        if not _string(item["row_id"]):
            _fail(f"generated row ID must be a non-empty string: {item.get('row_id')!r}")
        if item["row_id"] not in rows:
            _fail(f"generated row refers to an unknown row: {item['row_id']!r}")
        if not isinstance(item["kind"], str) or item["kind"] not in _GENERATED_KINDS:
            _fail(f"invalid generated row kind: {item['kind']!r}")

    used_stub_keys = function["used_stub_keys"]
    if not isinstance(used_stub_keys, list):
        _fail(f"functions.{function_name}.used_stub_keys must be a list")
    for key in used_stub_keys:
        if not _string(key) or key not in catalog:
            _fail(f"unknown stub reference: {key!r}")
        if catalog[key]["status"] == "orphaned":
            _fail(f"orphaned stub catalog key is still used: {key!r}")
    if len(set(used_stub_keys)) != len(used_stub_keys):
        _fail(f"functions.{function_name}.used_stub_keys contains duplicates")

    stubs = _mapping(function["stubs"], f"functions.{function_name}.stubs")
    for key, stub in stubs.items():
        if not _string(key) or key not in catalog:
            _fail(f"unknown function stub reference: {key!r}")
        _validate_stub(stub, f"functions.{function_name}.stubs.{key}")
        if catalog[key]["status"] == "orphaned" and stub["status"] != "orphaned":
            _fail(f"orphaned stub catalog key has an active function record: {key!r}")
    missing_records = set(used_stub_keys) - set(stubs)
    if missing_records:
        _fail(f"function stubs are missing used keys: {sorted(missing_records)!r}")

    return function


def _validate_function_links(
    function: Mapping[str, Any],
    function_name: str,
    links: dict[str, str],
) -> None:
    local_rows = function["rows"]
    for decision_key, decision in function["decisions"].items():
        for recipe_index, recipe in enumerate(decision["recipes"]):
            row_id = recipe["row_id"]
            if row_id not in local_rows:
                _fail(f"recipe refers to unknown row: {row_id!r}")
            if row_id in links:
                _fail(f"row has multiple logical links: {row_id}")
            links[row_id] = f"{function_name}.{decision_key}.recipes[{recipe_index}]"
            row = local_rows[row_id]
            if decision["status"] == "orphaned":
                if row["status"] != "orphaned":
                    _fail("orphaned Decision recipes must reference orphaned rows")
            elif row["status"] not in {"active", "orphaned"}:
                _fail("active Decision recipes must reference active or orphaned rows")

    for generated_index, item in enumerate(function["generated_rows"]):
        row_id = item["row_id"]
        if row_id in links:
            _fail(f"row has multiple logical links: {row_id}")
        links[row_id] = f"{function_name}.generated_rows[{generated_index}]"


def _validate_return_storage(
    function: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> None:
    variables = function["variables"]
    for key in function["used_stub_keys"]:
        entry = catalog[key]
        storage = entry["return_storage"]
        if entry["return_type"] == "void" or not storage:
            continue
        if storage not in variables:
            _fail(f"stub {key!r} return storage {storage!r} is not a function variable")
        if variables[storage].get("direction") not in {"input", "inout"}:
            _fail(f"stub {key!r} return storage must be an input variable")
        for row in function["rows"].values():
            if storage not in row["input"]:
                _fail(f"row is missing stub return storage {storage!r}")


def _validate_stub_catalog(catalog: Mapping[str, Any]) -> None:
    for key, entry in catalog.items():
        if not _string(key):
            _fail("stub catalog keys must be non-empty strings")
        entry = _mapping(entry, f"stub_catalog.{key}")
        _keys(entry, _STUB_CATALOG_KEYS, f"stub_catalog.{key}")
        if entry["stub_key"] != key:
            _fail(f"stub catalog key mismatch for {key!r}")
        if not isinstance(entry["status"], str) or entry["status"] not in {"active", "orphaned"}:
            _fail(f"invalid stub catalog status for {key!r}")
        for field in ("function_name", "return_type"):
            if not _string(entry[field]):
                _fail(f"stub_catalog.{key}.{field} must be a non-empty string")
        storage = entry["return_storage"]
        if storage is not None and not isinstance(storage, str):
            _fail(f"stub_catalog.{key}.return_storage must be a string or null")
        if entry["return_type"] != "void" and not storage:
            _fail(f"non-void stub {key!r} must have return_storage")
        parameters = entry["parameters"]
        if not isinstance(parameters, list):
            _fail(f"stub_catalog.{key}.parameters must be a list")
        names: set[str] = set()
        for parameter in parameters:
            parameter = _mapping(parameter, f"stub_catalog.{key}.parameters")
            if set(parameter) - {"name", "type"} or "name" not in parameter or "type" not in parameter:
                _fail(f"stub_catalog.{key}.parameters entries need name and type")
            if not _string(parameter["name"]) or not _string(parameter["type"]):
                _fail(f"stub_catalog.{key}.parameters entries need non-empty strings")
            if parameter["name"] in names:
                _fail(f"stub_catalog.{key} has duplicate parameter names")
            names.add(parameter["name"])


def _validate_stub(stub: object, path: str) -> None:
    stub = _mapping(stub, path)
    _keys(stub, _STUB_KEYS, path)
    if not isinstance(stub["owner"], str) or stub["owner"] not in {"tool", "user"}:
        _fail(f"{path}.owner is invalid")
    if not isinstance(stub["status"], str) or stub["status"] not in {"active", "orphaned"}:
        _fail(f"{path}.status is invalid")
    if not isinstance(stub["body"], str):
        _fail(f"{path}.body must be a string")


def _validate_decision(decision: object, path: str) -> None:
    decision = _mapping(decision, path)
    _keys(decision, _DECISION_REQUIRED_KEYS | _DECISION_OPTIONAL_KEYS, path, _DECISION_REQUIRED_KEYS)
    for field in _DECISION_OPTIONAL_KEYS:
        if field in decision and not _string(decision[field]):
            _fail(f"{path}.{field} must be a non-empty string")
    if not isinstance(decision["status"], str) or decision["status"] not in {"active", "orphaned"}:
        _fail(f"{path}.status is invalid")
    if not _string(decision["expression"]):
        _fail(f"{path}.expression must be a non-empty string")
    atoms = _mapping(decision["atoms"], f"{path}.atoms")
    atom_numbers: list[int] = []
    for key, text in atoms.items():
        match = _ATOM_KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or not _string(text):
            _fail(f"{path}.atoms contains an invalid atom")
        atom_numbers.append(int(match.group("number")))
    if sorted(atom_numbers) != list(range(1, len(atom_numbers) + 1)):
        _fail(f"{path}.atoms must be numbered consecutively")
    recipes = decision["recipes"]
    if not isinstance(recipes, list):
        _fail(f"{path}.recipes must be a list")
    for recipe in recipes:
        recipe = _mapping(recipe, f"{path}.recipes")
        _keys(recipe, _RECIPE_KEYS, "recipe")
        if not _string(recipe["row_id"]):
            _fail("recipe.row_id must be a non-empty string")
        if type(recipe["result"]) is not bool:
            _fail("recipe.result must be boolean")
        values = _mapping(recipe["values"], "recipe.values")
        if set(values) != set(atoms):
            _fail("recipe.values must contain every Decision atom exactly once")
        if any(type(value) is not bool for value in values.values()):
            _fail("recipe atom assignments must be boolean")


def _validate_row(row: object, variables: Mapping[str, Any], path: str) -> None:
    row = _mapping(row, path)
    _keys(row, _ROW_KEYS, path)
    if not isinstance(row["owner"], str) or row["owner"] not in {"tool", "user"}:
        _fail(f"{path}.owner is invalid")
    if not isinstance(row["status"], str) or row["status"] not in {"active", "orphaned"}:
        _fail(f"{path}.status is invalid")
    if not isinstance(row["description"], str):
        _fail(f"{path}.description must be a string")
    if row["test_method"] != "":
        _fail(f"{path}.test_method must be empty")
    for channel in _CHANNELS:
        values = _mapping(row[channel], f"{path}.{channel}")
        selected = _selected_variables(variables, channel)
        if set(values) != set(selected):
            _fail(f"{path}.{channel} does not match function variable metadata")
        for name, spec in selected.items():
            _validate_value(values[name], spec, f"{path}.{channel}.{name}")


def _validate_variable(spec: object, path: str, *, root: bool = False) -> None:
    spec = _mapping(spec, path)
    if "type" not in spec or not _string(spec["type"]):
        _fail(f"{path}.type must be a non-empty string")
    if root:
        if not isinstance(spec.get("direction"), str) or spec.get("direction") not in _DIRECTIONS:
            _fail(f"{path}.direction is invalid")
    elif "direction" in spec and (
        not isinstance(spec["direction"], str) or spec["direction"] not in _DIRECTIONS
    ):
        _fail(f"{path}.direction is invalid")
    dimensions = spec.get("dimensions", [])
    if not isinstance(dimensions, list) or any(type(size) is not int or size < 0 for size in dimensions):
        _fail(f"{path}.dimensions must be a list of non-negative integers")
    fields = spec.get("fields")
    if fields is not None:
        fields = _mapping(fields, f"{path}.fields")
        for name, child in fields.items():
            if not _string(name):
                _fail(f"{path}.fields contains an invalid name")
            _validate_variable(child, f"{path}.fields.{name}")


def _validate_value(value: object, spec: Mapping[str, Any], path: str) -> None:
    dimensions = spec.get("dimensions", [])
    _validate_value_dimensions(value, spec, dimensions, 0, path)


def _validate_value_dimensions(
    value: object,
    spec: Mapping[str, Any],
    dimensions: list[int],
    index: int,
    path: str,
) -> None:
    if index < len(dimensions):
        if not isinstance(value, list) or len(value) != dimensions[index]:
            _fail(f"{path} has an invalid array length/shape")
        for item_index, item in enumerate(value):
            _validate_value_dimensions(item, spec, dimensions, index + 1, f"{path}[{item_index}]")
        return
    fields = spec.get("fields")
    if fields is not None:
        if not isinstance(value, dict) or set(value) != set(fields):
            _fail(f"{path} has an invalid struct shape")
        for name, child in fields.items():
            _validate_value(value[name], child, f"{path}.{name}")
        return
    if not isinstance(value, str):
        _fail(f"{path} must preserve a string C lexeme")


def _selected_variables(variables: Mapping[str, Any], channel: str | None) -> dict[str, Mapping[str, Any]]:
    if channel is not None and channel not in _CHANNELS:
        _fail(f"invalid value channel: {channel!r}")
    result = {}
    for name, spec in variables.items():
        spec = _mapping(spec, f"metadata.{name}")
        direction = _direction(spec.get("direction"))
        if channel is None or direction == "inout" or direction == channel:
            result[name] = spec
    return result


def _direction(value: object) -> str | None:
    text = getattr(value, "value", value)
    if text == "in":
        return "input"
    if text == "out":
        return "output"
    if text in _DIRECTIONS:
        return text
    return None


def _row_prefix(state: Mapping[str, Any]) -> str:
    return _source_stem(state.get("source_file"))


def _source_file(value: object) -> str:
    if not _string(value):
        _fail("state.source_file must be a non-empty relative path")
    text = value.replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or ":" in text:
        _fail("state.source_file must be a non-empty relative path")
    _source_stem(value)
    return value


def _source_stem(value: object) -> str:
    if not _string(value):
        _fail("source_file must be a non-empty string")
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    stem = PurePosixPath(name).stem
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_.")
    if not normalized:
        _fail("source_file must have a usable stem")
    return normalized


def _validate_row_id(value: object, prefix: str) -> int:
    if not isinstance(value, str):
        _fail(f"row ID must be a string: {value!r}")
    match = _ROW_NUMBER.fullmatch(value)
    if match is None or match.group("prefix") != prefix or int(match.group("number")) == 0:
        _fail(f"row ID has an invalid module prefix/sequence: {value!r}")
    number = int(match.group("number"))
    if value != f"{prefix}_{number:03d}":
        _fail(f"row ID is not canonical: {value!r}")
    return number


def _safe_component(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or _SAFE_COMPONENT.fullmatch(value) is None:
        _fail(f"{path} must be one safe path component")
    if value in {".", ".."}:
        _fail(f"{path} must not be a traversal component")
    return value


def _mapping(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    return value


def _keys(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    path: str,
    required: frozenset[str] | None = None,
) -> None:
    if required is None:
        required = allowed
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown or missing:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)!r}")
        if unknown:
            details.append(f"unknown {sorted(unknown)!r}")
        _fail(f"{path} has invalid fields ({'; '.join(details)})")


def _string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _check_json_tree(value: object, path: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail(f"{path} has a non-string key")
            if key in _FORBIDDEN_KEYS:
                _fail(f"{path} contains forbidden state field {key!r}")
            _check_json_tree(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_json_tree(child, f"{path}[{index}]")
    elif value is None or type(value) in {str, bool, int}:
        return
    elif type(value) is float and math.isfinite(value):
        return
    else:
        _fail(f"{path} contains a non-JSON value")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _fail(message: str) -> None:
    raise StateValidationError(message)


__all__ = ["StateValidationError", "validate_module"]
