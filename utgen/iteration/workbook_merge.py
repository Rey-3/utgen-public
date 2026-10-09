"""Workbook-to-state value projection helpers used by iteration merge."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .values import (
    flatten_values,
    nest_values,
    pointer_member_storage,
    project_generated_values,
    project_variable_schema,
    preserve_user_variables,
    sync_row_schema,
)


def _get(item, *names, default=None):
    for name in names:
        value = item.get(name) if isinstance(item, Mapping) and name in item else getattr(item, name, None)
        if value is not None:
            return value
    return default


def _pointer_roots(function: Mapping) -> set[str]:
    variables = function.get("variables", {})
    return {
        name[1:]
        for name in variables
        if name.startswith("@") and name[1:] in variables
    }


def _workbook_state_name(function: Mapping, name: str, channel: str, storage_members) -> str:
    if name in storage_members:
        return storage_members[name]
    pointer_roots = _pointer_roots(function)
    if channel == "input" and name in pointer_roots:
        return f"@{name}"
    for root in pointer_roots:
        storage = f"tessy_{root}"
        if name == storage or name.startswith(storage + ".") or name.startswith(storage + "["):
            return root + name[len(storage):]
    return name


def _state_value_root(name: str) -> str:
    return name.split(".", 1)[0].split("[", 1)[0]


def _pointer_address_state_value(name: str, value: str) -> str:
    root = name[1:]
    token = value.strip()
    if token in {"NULL", "0", "0u", "0U"}:
        return "0"
    if token in {f"tessy_{root}", "1", "1u", "1U"}:
        return "1"
    raise ValueError(
        f"workbook pointer {root!r} must be NULL or tessy_{root}, got {value!r}"
    )


def extend_workbook_variables(functions: Mapping, function: dict, cases, columns=()) -> bool:
    grouped = {}
    storage_members = {storage: name for name, (storage, _) in pointer_member_storage(function["variables"]).items()}
    for column in columns or ():
        name = getattr(column, "name", None) or getattr(getattr(column, "key", None), "canonical_name", None)
        channel = getattr(column, "channel", None)
        if isinstance(name, str) and channel in {"input", "output"}:
            name = _workbook_state_name(function, name, channel, storage_members)
        root = _state_value_root(name) if name else None
        if name and root:
            grouped.setdefault(root, set()).add(name)
    for case in cases:
        for channel in ("input", "output"):
            cells = _get(case, f"{channel}_cells")
            if cells is None:
                cells = _get(case, channel, default={})
            if not isinstance(cells, Mapping):
                continue
            for key in cells:
                name = key if isinstance(key, str) else getattr(key, "canonical_name", None)
                if isinstance(name, str) and name:
                    name = _workbook_state_name(function, name, channel, storage_members)
                    root = _state_value_root(name)
                    grouped.setdefault(root, set()).add(name)

    imported = {}
    for root, paths in grouped.items():
        if root in function["variables"]:
            continue
        candidates = []
        for other in functions.values():
            if root not in other["variables"]:
                continue
            projected = project_variable_schema(other["variables"][root], root, paths)
            if projected is not None and projected not in candidates:
                candidates.append(projected)
        if len(candidates) != 1:
            raise ValueError(f"workbook variable {root!r} requires unambiguous C metadata")
        imported[root] = candidates[0]

    if not imported:
        return False
    old = deepcopy(function["variables"])
    preserve_user_variables(function)
    function["variables"].update(deepcopy(imported))
    for row in function["rows"].values():
        sync_row_schema(row, old, function["variables"])
    return True


def snapshot_values(case, function: dict, channel: str, allow_missing: bool) -> dict:
    cells = _get(case, f"{channel}_cells")
    if cells is None:
        cells = _get(case, channel, default={})
    if not isinstance(cells, Mapping):
        raise ValueError(f"workbook {channel} cells must be a mapping")

    variables = function["variables"]
    defaults = flatten_values(project_generated_values({}, variables, channel), variables, channel)
    flat = {}
    members = pointer_member_storage(variables)
    storage_members = {storage: name for name, (storage, _) in members.items()}
    workbook_names = {key if isinstance(key, str) else getattr(key, "canonical_name", None) for key in cells}
    address_columns = {name for name, (storage, _) in members.items() if storage in workbook_names}
    addresses = {}
    pointer_roots = _pointer_roots(function)
    for key, cell in cells.items():
        workbook_name = key if isinstance(key, str) else getattr(key, "canonical_name", None)
        if not isinstance(workbook_name, str) or not workbook_name:
            raise ValueError("workbook value column has no canonical name")
        if not isinstance(key, str) and getattr(key, "channel", channel) != channel:
            raise ValueError(f"workbook value channel mismatch for {workbook_name!r}")
        name = _workbook_state_name(function, workbook_name, channel, storage_members)
        if name not in defaults:
            raise ValueError(f"workbook value path is outside metadata: {workbook_name!r}")
        raw = getattr(cell, "raw_text", cell)
        is_member_address = workbook_name in members and (
            workbook_name in address_columns or raw == members[workbook_name][0]
        )
        destination = addresses if is_member_address else flat
        if name in destination:
            raise ValueError(f"duplicate workbook value path: {workbook_name!r}")

        kind = getattr(cell, "kind", "value")
        if kind == "missing":
            if not allow_missing:
                raise ValueError(f"workbook missing cell cannot update an existing row: {workbook_name!r}")
            continue
        if kind == "explicit_empty":
            if is_member_address:
                raise ValueError(f"workbook pointer address cannot be empty: {workbook_name!r}")
            flat[name] = ""
            continue
        if kind != "value":
            raise ValueError(f"workbook {kind} cell cannot become a JSON lexeme: {workbook_name!r}")
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"workbook value {workbook_name!r} is not a non-empty lexeme")
        if name.startswith("@") and name[1:] in pointer_roots:
            raw = _pointer_address_state_value(name, raw)
        destination[name] = raw

    if not allow_missing and set(defaults) - set(flat):
        raise ValueError("workbook is missing one or more schema value cells")
    for name in defaults.keys() & members.keys():
        if name not in addresses:
            # Older JSON exports stored the pointee directly in the member cell.
            if name in flat and name not in address_columns:
                continue
            if not allow_missing:
                raise ValueError(f"workbook is missing a pointer address: {name!r}")
            continue
        storage = members[name][0]
        address = addresses[name].strip()
        if address == "NULL":
            flat[name] = "NULL"
        elif address != storage:
            raise ValueError(f"workbook pointer {name!r} must be NULL or {storage}, got {address!r}")

    defaults.update(flat)
    return nest_values(defaults, variables, channel)


__all__ = ["extend_workbook_variables", "snapshot_values"]
