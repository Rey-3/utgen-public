"""Persistence and identity allocation for one source module's JSON state."""

from __future__ import annotations

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


__all__ = [
    "ModulePaths",
    "StateValidationError",
    "allocate_decision_key",
    "allocate_row_id",
    "load_module",
    "module_paths",
    "validate_module",
    "write_module",
]
