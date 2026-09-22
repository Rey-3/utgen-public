"""Workbook-to-state value projection helpers used by iteration merge."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import warnings

from .values import (
    flatten_values,
    infer_variable_schema,
    nest_values,
    project_generated_values,
    project_variable_schema,
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


def _workbook_state_name(function: Mapping, name: str, channel: str) -> str:
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
    for column in columns or ():
        name = getattr(column, "name", None) or getattr(getattr(column, "key", None), "canonical_name", None)
        channel = getattr(column, "channel", None)
        if isinstance(name, str) and channel in {"input", "output"}:
            name = _workbook_state_name(function, name, channel)
        root = _state_value_root(name) if name else None
        if name and root:
            grouped.setdefault(root, [set(), []])[0].add(name)
            grouped[root][1].append(column)
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
                    name = _workbook_state_name(function, name, channel)
                    root = _state_value_root(name)
                    grouped.setdefault(root, [set(), []])[0].add(name)

    imported = {}
    for root, (paths, columns_for_root) in grouped.items():
        if root in function["variables"]:
            continue
        known, candidates = False, []
        for other in functions.values():
            if root not in other["variables"]:
                continue
            known = True
            projected = project_variable_schema(other["variables"][root], root, paths)
            if projected is not None and projected not in candidates:
                candidates.append(projected)
        if known:
            if len(candidates) == 1:
                imported[root] = candidates[0]
            continue
        if not columns_for_root:
            continue
        hints = {
            str(getattr(column, "raw_type_hint", "")).strip()
            for column in columns_for_root
            if getattr(column, "raw_type_hint", None)
            and str(getattr(column, "raw_type_hint")).strip()
        }
        channels = {getattr(column, "channel", None) for column in columns_for_root}
        if len(hints) > 1:
            raise ValueError(f"new workbook variable {root!r} has conflicting types")
        if len(channels) != 1:
            raise ValueError(f"new workbook variable {root!r} has conflicting direction")
        type_hint, direction = next(iter(hints), ""), next(iter(channels))
        imported[root] = infer_variable_schema(root, paths, type_hint, direction)
        warnings.warn(
            f"imported new workbook variable {root!r} as {direction}({type_hint}); verify it against C metadata",
            UserWarning,
            stacklevel=2,
        )

    if not imported:
        return False
    old = deepcopy(function["variables"])
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
    pointer_roots = _pointer_roots(function)
    for key, cell in cells.items():
        workbook_name = key if isinstance(key, str) else getattr(key, "canonical_name", None)
        if not isinstance(workbook_name, str) or not workbook_name:
            raise ValueError("workbook value column has no canonical name")
        if not isinstance(key, str) and getattr(key, "channel", channel) != channel:
            raise ValueError(f"workbook value channel mismatch for {workbook_name!r}")
        name = _workbook_state_name(function, workbook_name, channel)
        if name not in defaults:
            raise ValueError(f"workbook value path is outside metadata: {workbook_name!r}")
        if name in flat:
            raise ValueError(f"duplicate workbook value path: {workbook_name!r}")

        kind = getattr(cell, "kind", "value")
        if kind == "missing":
            if not allow_missing:
                raise ValueError(f"workbook missing cell cannot update an existing row: {workbook_name!r}")
            continue
        if kind == "explicit_empty":
            flat[name] = ""
            continue
        if kind != "value":
            raise ValueError(f"workbook {kind} cell cannot become a JSON lexeme: {workbook_name!r}")
        raw = getattr(cell, "raw_text", cell)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"workbook value {workbook_name!r} is not a non-empty lexeme")
        if name.startswith("@") and name[1:] in pointer_roots:
            raw = _pointer_address_state_value(name, raw)
        flat[name] = raw

    if not allow_missing and set(defaults) - set(flat):
        raise ValueError("workbook is missing one or more schema value cells")
    defaults.update(flat)
    return nest_values(defaults, variables, channel)


__all__ = ["extend_workbook_variables", "snapshot_values"]
