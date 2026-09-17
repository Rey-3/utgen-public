"""Persistence and identity allocation for one source module's JSON state."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading

from .validation import (
    StateValidationError,
    _DECISION_KEY,
    _fail,
    _reject_json_constant,
    _row_prefix,
    _safe_component,
    _validate_row_id,
    validate_module,
)


@dataclass(frozen=True)
class ModulePaths:
    database: Path
    state: Path


_ROW_ALLOCATOR_LOCK = threading.Lock()


def module_paths(state_root: str | os.PathLike[str], project: str, module: str) -> ModulePaths:
    """Return project-scoped Analysis DB and single-module JSON paths."""

    _safe_component(project, "project")
    _safe_component(module, "module")
    root = Path(state_root)
    project_root = root / "iteration" / project
    return ModulePaths(
        database=project_root / f"db_{module}.xml",
        state=project_root / module / f"{module}.json",
    )


def load_module(path: str | os.PathLike[str]) -> dict:
    """Read, decode, and validate a module JSON document."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        state = json.loads(text, parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, StateValidationError):
            raise
        raise StateValidationError(f"cannot read module JSON: {path}") from exc
    validate_module(state)
    return state


def write_module(path: str | os.PathLike[str], state: dict) -> None:
    """Validate and atomically publish a module JSON document."""

    validate_module(state)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_bytes() if path.exists() else None
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        load_module(temporary)
        os.replace(temporary, path)
        try:
            load_module(path)
        except Exception:
            if previous is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                with temporary.open("wb") as stream:
                    stream.write(previous)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def allocate_row_id(state: dict) -> str:
    """Allocate the next never-reused row ID for the whole module."""

    with _ROW_ALLOCATOR_LOCK:
        if not isinstance(state, dict):
            _fail("state must be a mapping")
        prefix = _row_prefix(state)
        max_row_number = state.get("max_row_number")
        if type(max_row_number) is not int or max_row_number < 0:
            _fail("state.max_row_number must be a non-negative integer")
        highest = 0
        seen: set[str] = set()
        functions = state.get("functions", {})
        if not isinstance(functions, dict):
            _fail("state.functions must be a mapping")
        for function in functions.values():
            if not isinstance(function, dict):
                _fail("function state must be a mapping")
            rows = function.get("rows", {})
            if not isinstance(rows, dict):
                _fail("function.rows must be a mapping")
            for row_id in rows:
                number = _validate_row_id(row_id, prefix)
                if row_id in seen:
                    _fail(f"duplicate row ID: {row_id}")
                seen.add(row_id)
                highest = max(highest, number)
        if max_row_number < highest:
            _fail("state.max_row_number is below an existing row ID")
        next_number = max_row_number + 1
        state["max_row_number"] = next_number
        return f"{prefix}_{next_number:03d}"


def allocate_decision_key(function_state: dict) -> str:
    """Allocate the next never-reused Decision key for one function."""

    if not isinstance(function_state, dict):
        _fail("function state must be a mapping")
    maximum = function_state.get("max_decision_number")
    if type(maximum) is not int or maximum < 0:
        _fail("function.max_decision_number must be a non-negative integer")
    decisions = function_state.get("decisions", {})
    if not isinstance(decisions, dict):
        _fail("function.decisions must be a mapping")
    highest = 0
    for key in decisions:
        match = _DECISION_KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None:
            _fail(f"invalid Decision key: {key!r}")
        highest = max(highest, int(match.group("number")))
    if maximum < highest:
        _fail("function.max_decision_number is below an existing Decision key")
    next_number = maximum + 1
    function_state["max_decision_number"] = next_number
    return f"D{next_number}"


def ordered_active_rows(function: Mapping) -> list[tuple[str, dict]]:
    """Return active physical rows in their persisted mapping order."""
    return [(row_id, row) for row_id, row in function["rows"].items() if row["status"] == "active"]


def _recipe_structure(decision: dict, recipe: dict) -> tuple:
    return recipe["result"], tuple(recipe["values"][key] for key in decision["atoms"])


def _preferred_tool_rows(current: dict, candidate: dict, matches) -> dict:
    choices = {}
    current_rows = current["rows"]
    for match in matches:
        if match.current_key is None:
            continue
        incoming = candidate["decisions"][match.candidate_key]
        old_decision = current["decisions"][match.current_key]
        used: set[int] = set()
        for recipe in incoming["recipes"]:
            index = next((index for index, old in enumerate(old_decision["recipes"])
                          if index not in used and _recipe_structure(old_decision, old) == _recipe_structure(incoming, recipe)), None)
            if index is None:
                continue
            used.add(index)
            old_id = old_decision["recipes"][index]["row_id"]
            if current_rows.get(old_id, {}).get("owner") == "tool":
                choices.setdefault(recipe["row_id"], []).append(old_id)
    old_generated = current["generated_rows"]
    used: set[int] = set()
    for item in candidate["generated_rows"]:
        index = next((index for index, old in enumerate(old_generated)
                      if index not in used and old["kind"] == item["kind"]), None)
        if index is None:
            continue
        used.add(index)
        old_id = old_generated[index]["row_id"]
        if current_rows.get(old_id, {}).get("owner") == "tool":
            choices.setdefault(item["row_id"], []).append(old_id)
    assigned = set()
    preferred = {}
    for candidate_id in candidate["rows"]:
        for old_id in choices.get(candidate_id, ()):
            if old_id not in assigned:
                preferred[candidate_id] = old_id
                assigned.add(old_id)
                break
    return preferred


def _resolve_row(state, function, candidate, candidate_id, old_id, counters, context):
    rows = function["rows"]
    candidate_row = candidate["rows"][candidate_id]
    if old_id is not None and rows[old_id]["owner"] == "user":
        if old_id in context["users"]:
            old_id = None
        else:
            context["users"].add(old_id)
            context["retained"].add(old_id)
            context["order"].setdefault(candidate_id, old_id)
            return old_id
    row_id = context["canonical"].get(candidate_id)
    if row_id is None:
        old_id = context["preferred"].get(candidate_id, old_id)
        if old_id is not None and context["claimed"].get(old_id) not in (None, candidate_id):
            old_id = None
        if old_id is None:
            row_id = allocate_row_id(state)
            rows[row_id] = _tool(candidate_row)
            counters["rows_allocated"] += 1
        else:
            row_id = old_id
            _merge_row(rows[row_id], candidate_row, counters)
            context["claimed"][row_id] = candidate_id
        context["canonical"][candidate_id] = row_id
        context["order"][candidate_id] = row_id
    elif old_id is not None and old_id != row_id and context["claimed"].get(old_id) is None:
        _mark_orphan(rows.get(old_id), counters)
    context["retained"].add(row_id)
    return row_id


def _merge_row(current: dict, candidate: dict, counters: dict) -> None:
    if current["owner"] != "user":
        current.clear()
        current.update(_tool(candidate))
    counters["rows_kept"] += 1


def _tool(item: dict) -> dict:
    result = deepcopy(item)
    result.update(owner="tool", status="active")
    return result


def _mark_orphan(row: dict | None, counters: dict) -> None:
    if row is None:
        return
    if row["status"] != "orphaned":
        counters["rows_orphaned"] += 1
    row["status"] = "orphaned"


__all__ = [
    "ModulePaths",
    "allocate_decision_key",
    "allocate_row_id",
    "load_module",
    "module_paths",
    "ordered_active_rows",
    "write_module",
]
