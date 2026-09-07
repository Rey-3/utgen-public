"""Build and merge the plain-dict state used by one generation."""

from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .state import allocate_decision_key, allocate_row_id
from .validation import validate_module
from .values import (
    flatten_values,
    metadata_variables,
    nest_values,
    project_generated_values,
    sync_row_schema,
)
from ..mcdc import eval_expr
from ..model import And, AssemblyResult, Atom, GeneratedRowProvenance, Not, Or, RecipeProvenance, atoms_of
_GENERATED_KINDS = frozenset({"Nominal", "Timeout"})

@dataclass(frozen=True)
class DecisionMatch:
    candidate_key: str
    current_key: str | None

@dataclass(frozen=True)
class MergeResult:
    state: dict
    summary: dict

@dataclass(frozen=True)
class UserMergeResult:
    state: dict
    summary: dict
    allocated_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.summary.get("status") == "updated"

    @property
    def skipped(self) -> bool:
        return not self.changed

def merge_user(state: dict, function_name: str, workbook_snapshot) -> UserMergeResult:
    """Merge one complete function workbook into a current module state."""
    validate_module(state)
    if function_name not in state["functions"]:
        raise ValueError(f"unknown function {function_name!r}")
    _validate_workbook_identity(state, function_name, workbook_snapshot)
    cases = _get(workbook_snapshot, "cases")
    if not isinstance(cases, (list, tuple)):
        raise ValueError("workbook snapshot cases must be a sequence")

    current = state["functions"][function_name]
    result = deepcopy(state)
    function = result["functions"][function_name]
    rows = function["rows"]
    active_ids = [row_id for row_id, _ in _ordered_active_rows(current)]
    legacy = all(_case_row_id(case) is None for case in cases)
    seen: set[str] = set()
    allocated: list[str] = []
    row_changed = added = orphaned = 0
    for index, case in enumerate(cases):
        row_id = _case_row_id(case)
        new_row = False
        if row_id is None:
            if legacy and index < len(active_ids):
                row_id = active_ids[index]
            else:
                row_id = allocate_row_id(result)
                allocated.append(row_id)
                added += 1
                new_row = True
        if not isinstance(row_id, str) or not row_id:
            raise ValueError("workbook row ID must be a non-empty string or omitted")
        if row_id in seen:
            raise ValueError(f"duplicate workbook row ID: {row_id!r}")
        seen.add(row_id)
        if row_id not in rows and not new_row:
            raise ValueError(f"workbook references unknown row ID: {row_id!r}")
        incoming = _snapshot_row(case, function, allow_missing=new_row)
        if new_row:
            rows[row_id] = incoming
            continue
        old = rows[row_id]
        if old["status"] != "active" or _row_content(old) != _row_content(incoming):
            old.clear()
            old.update(incoming)
            row_changed += 1

    for row_id, row in rows.items():
        if row["status"] == "active" and row_id not in seen:
            row.update(owner="user", status="orphaned")
            orphaned += 1
    stub_changed, stub_orphaned = _merge_user_stubs(function, workbook_snapshot, result["stub_catalog"])
    validate_module(result)
    updated = bool(row_changed or added or orphaned or stub_changed or stub_orphaned)
    summary = {
        "status": "updated" if updated else "skipped",
        "rows": {"changed": row_changed, "added": added, "orphaned": orphaned},
        "stubs": {"changed": stub_changed, "orphaned": stub_orphaned},
    }
    return UserMergeResult(result, summary, tuple(allocated))

def _validate_workbook_identity(state, function_name: str, snapshot) -> None:
    meta = _get(snapshot, "meta")
    project = _get(meta, "project", "project_name")
    module = _get(meta, "module", "module_name")
    testobject = _get(meta, "testobject", "test_object", "test_object_name")
    module = Path(str(module).replace("\\", "/")).name if module is not None else None
    if module and module.lower().endswith(".c"):
        module = module[:-2]
    source = Path(state["source_file"].replace("\\", "/")).stem
    expected_projects = {state["project"], f"tessy_{state['project']}"}
    expected_modules = {state["module"], source}
    if project not in expected_projects or module not in expected_modules or testobject != function_name:
        raise ValueError(
            "workbook identity does not match "
            f"{state['project']}/{state['module']}/{function_name}"
        )

def _get(item, *names, default=None):
    for name in names:
        value = item.get(name) if isinstance(item, Mapping) and name in item else getattr(item, name, None)
        if value is not None:
            return value
    return default

def _ordered_active_rows(function: Mapping) -> list[tuple[str, dict]]:
    """Keep the adapter's generated, decision, then manual export order."""
    rows = function["rows"]
    order = [item["row_id"] for item in function.get("generated_rows", [])]
    order += [
        recipe["row_id"]
        for decision in function.get("decisions", {}).values()
        for recipe in decision.get("recipes", [])
    ]
    order += list(rows)
    seen, result = set(), []
    for row_id in order:
        if row_id in seen or row_id not in rows:
            continue
        seen.add(row_id)
        if rows[row_id]["status"] == "active":
            result.append((row_id, rows[row_id]))
    return result

def _case_row_id(case):
    return _get(case, "row_id", "external_test_case_id")

def _case_description(case) -> str:
    value = _get(case, "state_description", "description", default="")
    if not isinstance(value, str):
        raise ValueError("workbook row description must be text")
    return value

def _snapshot_row(case, function: dict, *, allow_missing: bool = False) -> dict:
    return {
        "owner": "user",
        "status": "active",
        "description": _case_description(case),
        "test_method": "",
        "input": _snapshot_values(case, function, "input", allow_missing),
        "output": _snapshot_values(case, function, "output", allow_missing),
    }

def _snapshot_values(case, function: dict, channel: str, allow_missing: bool) -> dict:
    cells = _get(case, f"{channel}_cells")
    if cells is None:
        cells = _get(case, channel, default={})
    if not isinstance(cells, Mapping):
        raise ValueError(f"workbook {channel} cells must be a mapping")
    variables = function["variables"]
    defaults = flatten_values(project_generated_values({}, variables, channel), variables, channel)
    flat = {}
    for key, cell in cells.items():
        name = key if isinstance(key, str) else getattr(key, "canonical_name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("workbook value column has no canonical name")
        if not isinstance(key, str) and getattr(key, "channel", channel) != channel:
            raise ValueError(f"workbook value channel mismatch for {name!r}")
        if name not in defaults:
            raise ValueError(f"workbook value path is outside metadata: {name!r}")
        if name in flat:
            raise ValueError(f"duplicate workbook value path: {name!r}")
        kind = getattr(cell, "kind", "value")
        if kind == "missing":
            if not allow_missing:
                raise ValueError(f"workbook missing cell cannot update an existing row: {name!r}")
            continue
        if kind == "explicit_empty":
            flat[name] = ""
            continue
        if kind != "value":
            raise ValueError(f"workbook {kind} cell cannot become a JSON lexeme: {name!r}")
        raw = getattr(cell, "raw_text", cell)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"workbook value {name!r} is not a non-empty lexeme")
        flat[name] = raw
    if not allow_missing and set(defaults) - set(flat):
        raise ValueError("workbook is missing one or more schema value cells")
    defaults.update(flat)
    return nest_values(defaults, variables, channel)

def _row_content(row: Mapping) -> tuple:
    return row["description"], row["input"], row["output"]

def _merge_user_stubs(function: dict, snapshot, catalog: Mapping) -> tuple[int, int]:
    stubs = _get(snapshot, "stubs", default=())
    if not isinstance(stubs, (list, tuple)):
        raise ValueError("workbook snapshot stubs must be a sequence")
    seen = {}
    for stub in stubs:
        key = _get(stub, "name", "stub_name")
        body = _get(stub, "body_raw", "body")
        if not isinstance(key, str) or not key or not isinstance(body, str):
            raise ValueError("workbook stub requires a name and body")
        if key in seen:
            raise ValueError(f"duplicate workbook stub: {key!r}")
        seen[key] = body
    current = function["stubs"]
    changed = orphaned = 0
    for key, body in seen.items():
        if key not in catalog or catalog[key]["status"] == "orphaned":
            raise ValueError(f"workbook references an unavailable stub: {key!r}")
        item = current.get(key)
        if item is None:
            current[key] = {"owner": "user", "status": "active", "body": body}
            changed += 1
        elif item["body"] != body or item["status"] != "active":
            item.update(owner="user", status="active", body=body)
            changed += 1
    for key, item in current.items():
        if key not in seen and item["status"] == "active":
            item.update(owner="user", status="orphaned")
            orphaned += 1
    return changed, orphaned

def match_decisions(current_function: dict, candidate_function: dict) -> list[DecisionMatch]:
    """Match candidate decisions in source order by exact readable structure."""
    current_items = tuple(current_function.get("decisions", {}).items())
    candidate_items = tuple(candidate_function.get("decisions", {}).items())
    used: set[str] = set()
    matches: list[str | None] = [None] * len(candidate_items)
    for structure in (_decision_structure, _decision_core):
        for index, (_, candidate) in enumerate(candidate_items):
            if matches[index] is not None:
                continue
            key = next((key for key, old in current_items if key not in used and structure(old) == structure(candidate)), None)
            if key is not None:
                matches[index] = key
                used.add(key)
    return [DecisionMatch(item[0], matches[index]) for index, item in enumerate(candidate_items)]

def merge_tool(current: dict, candidate: dict) -> MergeResult:
    """Merge one regenerated candidate into the current module state."""
    validate_module(current)
    validate_module(candidate)
    identity = ("project", "module", "source_file")
    if tuple(current[key] for key in identity) != tuple(candidate[key] for key in identity):
        raise ValueError("current and candidate module identities do not match")
    state = deepcopy(current)
    state.update({key: candidate[key] for key in (*identity, "module_input_hash")})
    state["stub_catalog"] = _merge_stub_catalog(current["stub_catalog"], candidate["stub_catalog"])
    counter_names = (
        "decisions_matched", "decisions_added", "decisions_orphaned",
        "rows_kept", "rows_allocated", "rows_orphaned",
    )
    counters = {key: 0 for key in counter_names}
    current_functions, candidate_functions = current["functions"], candidate["functions"]
    for name in current_functions:
        if name not in candidate_functions:
            _orphan_function(state["functions"][name], counters)
    for name, candidate_function in candidate_functions.items():
        state["functions"][name] = (
            _new_function(state, candidate_function, counters)
            if name not in current_functions
            else _merge_function(state, current_functions[name], candidate_function, counters)
        )
    validate_module(state)
    summary = {
        "decisions": {
            "matched": counters["decisions_matched"],
            "added": counters["decisions_added"],
            "orphaned": counters["decisions_orphaned"],
        },
        "rows": {"kept": counters["rows_kept"], "allocated": counters["rows_allocated"], "orphaned": counters["rows_orphaned"]},
        "stubs": {
            "active": sum(item["status"] == "active" for item in state["stub_catalog"].values()),
            "orphaned": sum(item["status"] == "orphaned" for item in state["stub_catalog"].values()),
        },
    }
    return MergeResult(state, summary)

def _decision_core(decision: dict) -> tuple:
    atoms = decision.get("atoms", {})
    return decision.get("kind", "decision"), decision["expression"], tuple(atoms.values())

def _decision_structure(decision: dict) -> tuple:
    atoms = decision.get("atoms", {})
    return _decision_core(decision), tuple(
        (recipe["result"], tuple(recipe["values"][key] for key in atoms))
        for recipe in decision["recipes"]
    )

def _recipe_structure(decision: dict, recipe: dict) -> tuple:
    return recipe["result"], tuple(recipe["values"][key] for key in decision["atoms"])

def _merge_stub_catalog(current: dict, candidate: dict) -> dict:
    merged = {}
    for key, entry in candidate.items():
        item = deepcopy(entry)
        item["status"] = "active"
        merged[key] = item
    for key, entry in current.items():
        if key not in candidate:
            item = deepcopy(entry)
            item["status"] = "orphaned"
            merged[key] = item
    return merged

def _new_function(state: dict, candidate: dict, counters: dict) -> dict:
    function = deepcopy(candidate)
    function.update(max_decision_number=0, decisions={}, generated_rows=[], rows={})
    candidate_rows = candidate["rows"]
    for decision in candidate["decisions"].values():
        key = allocate_decision_key(function)
        merged = deepcopy(decision)
        merged["recipes"] = []
        for recipe in decision["recipes"]:
            row_id = _allocate_candidate_row(state, function, candidate_rows, recipe["row_id"], counters)
            item = deepcopy(recipe)
            item["row_id"] = row_id
            merged["recipes"].append(item)
        function["decisions"][key] = merged
        counters["decisions_added"] += 1
    for generated in candidate["generated_rows"]:
        row_id = _allocate_candidate_row(state, function, candidate_rows, generated["row_id"], counters)
        function["generated_rows"].append({"row_id": row_id, "kind": generated["kind"]})
    linked = {
        recipe["row_id"]
        for decision in candidate["decisions"].values()
        for recipe in decision["recipes"]
    }
    linked.update(item["row_id"] for item in candidate["generated_rows"])
    for old_row_id, row in candidate_rows.items():
        if old_row_id not in linked:
            row_id = allocate_row_id(state)
            function["rows"][row_id] = _tool(row)
            counters["rows_allocated"] += 1
    function["stubs"] = _merge_stubs({}, candidate, state["stub_catalog"])
    return function


def _allocate_candidate_row(state, function, candidate_rows, candidate_id, counters):
    row_id = allocate_row_id(state)
    function["rows"][row_id] = _tool(candidate_rows[candidate_id])
    counters["rows_allocated"] += 1
    return row_id


def _merge_function(state: dict, current: dict, candidate: dict, counters: dict) -> dict:
    function = deepcopy(current)
    function["variables"] = deepcopy(candidate["variables"])
    for row in function["rows"].values():
        sync_row_schema(row, current["variables"], candidate["variables"])
    matches = match_decisions(current, candidate)
    matched = {item.current_key for item in matches if item.current_key is not None}
    decisions = {}
    for item in matches:
        incoming = candidate["decisions"][item.candidate_key]
        if item.current_key is None:
            key = allocate_decision_key(function)
            decisions[key] = _merge_decision_rows(state, function, candidate, incoming, None, counters)
            counters["decisions_added"] += 1
        else:
            key = item.current_key
            decisions[key] = _merge_decision_rows(state, function, candidate, incoming, current["decisions"][key], counters)
            counters["decisions_matched"] += 1
    for key, old in current["decisions"].items():
        if key in matched:
            continue
        orphaned = deepcopy(function["decisions"][key])
        orphaned["status"] = "orphaned"
        for recipe in orphaned["recipes"]:
            _mark_orphan(function["rows"].get(recipe["row_id"]), counters)
        decisions[key] = orphaned
        if old["status"] != "orphaned":
            counters["decisions_orphaned"] += 1
    function["decisions"] = decisions

    generated = []
    used: set[int] = set()
    old_generated = current["generated_rows"]
    for item in candidate["generated_rows"]:
        index = next((index for index, old in enumerate(old_generated) if index not in used and old["kind"] == item["kind"]), None)
        if index is None:
            row_id = _allocate_candidate_row(state, function, candidate["rows"], item["row_id"], counters)
        else:
            used.add(index)
            row_id = old_generated[index]["row_id"]
            _merge_row(function["rows"][row_id], candidate["rows"][item["row_id"]], counters)
        generated.append({"row_id": row_id, "kind": item["kind"]})
    for index, item in enumerate(old_generated):
        if index not in used:
            _mark_orphan(function["rows"].get(item["row_id"]), counters)
            generated.append(deepcopy(item))
    function["generated_rows"] = generated
    function["used_stub_keys"] = deepcopy(candidate["used_stub_keys"])
    function["stubs"] = _merge_stubs(current, candidate, state["stub_catalog"])
    return function


def _merge_decision_rows(state, function, candidate, decision, old_decision, counters):
    result = deepcopy(decision)
    result["status"] = "active"
    result["recipes"] = []
    old_recipes = old_decision["recipes"] if old_decision is not None else ()
    used: set[int] = set()
    for recipe in decision["recipes"]:
        index = next(
            (
                index for index, old in enumerate(old_recipes)
                if index not in used
                and _recipe_structure(old_decision, old) == _recipe_structure(decision, recipe)
            ),
            None,
        ) if old_decision is not None else None
        if index is None:
            row_id = _allocate_candidate_row(state, function, candidate["rows"], recipe["row_id"], counters)
        else:
            used.add(index)
            row_id = old_recipes[index]["row_id"]
            _merge_row(function["rows"][row_id], candidate["rows"][recipe["row_id"]], counters)
        item = deepcopy(recipe)
        item["row_id"] = row_id
        result["recipes"].append(item)
    for index, old in enumerate(old_recipes):
        if index not in used:
            _mark_orphan(function["rows"].get(old["row_id"]), counters)
            result["recipes"].append(deepcopy(old))
    return result


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


def _merge_stubs(current: dict, candidate: dict, catalog: dict) -> dict:
    current_stubs, candidate_stubs = current.get("stubs", {}), candidate.get("stubs", {})
    used = set(candidate.get("used_stub_keys", ()))
    merged = {}
    for key, entry in catalog.items():
        old = current_stubs.get(key)
        if entry["status"] == "orphaned":
            if old is not None:
                item = deepcopy(old)
                item["status"] = "orphaned"
                merged[key] = item
            continue
        incoming = candidate_stubs.get(key) or {"owner": "tool", "status": "active", "body": default_stub_body(entry, key in used)}
        merged[key] = deepcopy(old) if old is not None and old["owner"] == "user" else _tool(incoming)
    for key, old in current_stubs.items():
        if key not in merged:
            item = deepcopy(old)
            item["status"] = "orphaned"
            merged[key] = item
    return merged


def _orphan_function(function: dict, counters: dict) -> None:
    for decision in function["decisions"].values():
        if decision["status"] != "orphaned":
            counters["decisions_orphaned"] += 1
        decision["status"] = "orphaned"
        for recipe in decision["recipes"]:
            _mark_orphan(function["rows"].get(recipe["row_id"]), counters)
    for item in function["generated_rows"]:
        _mark_orphan(function["rows"].get(item["row_id"]), counters)
    for row in function["rows"].values():
        _mark_orphan(row, counters)
    for stub in function["stubs"].values():
        stub["status"] = "orphaned"
    function["used_stub_keys"] = []


def build_candidate(analysis, generated_functions, project, module, input_hash) -> dict:
    """Project analysis and assembly results into one validated module dict."""
    source_file = _relative_source(getattr(analysis, "file", "") or f"{module}.c")
    state = {
        "schema_version": 1,
        "project": str(project),
        "module": str(module),
        "source_file": source_file,
        "module_input_hash": str(input_hash),
        "max_row_number": 0,
        "stub_catalog": build_stub_catalog(analysis),
        "functions": {},
    }
    for function in _functions(analysis):
        name = str(function.name)
        generated = _generated_result(generated_functions, name)
        recipes_by_decision, generated_rows = _validated_provenance(function, generated)
        function_state = {
            "max_decision_number": 0,
            "variables": metadata_variables(function),
            "decisions": {},
            "generated_rows": [],
            "rows": {},
            "used_stub_keys": _used_stub_keys(function),
            "stubs": {},
        }
        state["functions"][name] = function_state
        for decision_index, decision in enumerate(getattr(function, "decisions", ())):
            decision_key = allocate_decision_key(function_state)
            atoms = tuple(atoms_of(decision.expr))
            item = {
                "status": "active",
                "expression": _expression_text(decision.expr),
                "atoms": {f"A{index}": _atom_text(atom) for index, atom in enumerate(atoms, 1)},
                "recipes": [],
            }
            if getattr(decision, "kind", ""):
                item["kind"] = str(decision.kind)
            if getattr(decision, "location", ""):
                item["location"] = str(decision.location)
            function_state["decisions"][decision_key] = item
            seen_recipes = set()
            for recipe in recipes_by_decision[decision_index]:
                key = _recipe_key(recipe)
                if key in seen_recipes:
                    continue
                seen_recipes.add(key)
                row_id = allocate_row_id(state)
                function_state["rows"][row_id] = _assembly_row(generated, recipe.concrete_row_index, function)
                item["recipes"].append({
                    "row_id": row_id,
                    "result": bool(recipe.result),
                    "values": {f"A{index}": bool(value) for index, value in enumerate(recipe.atom_values, 1)},
                })
        for generated_row in generated_rows:
            row_id = allocate_row_id(state)
            function_state["rows"][row_id] = _assembly_row(generated, generated_row.concrete_row_index, function)
            function_state["generated_rows"].append({"row_id": row_id, "kind": str(generated_row.kind)})
        for key, entry in state["stub_catalog"].items():
            function_state["stubs"][key] = {
                "owner": "tool",
                "status": "active",
                "body": default_stub_body(entry, key in function_state["used_stub_keys"]),
            }
    validate_module(state)
    return state


def build_stub_catalog(analysis) -> dict:
    """Collect one active, signature-checked catalog from all module functions."""
    catalog, signatures = {}, {}
    for function in _functions(analysis):
        for stub in getattr(function, "stubs", ()):
            key = _stub_name(stub)
            return_type, parameters = _stub_return_type(stub), _stub_parameters(stub)
            signature = return_type, tuple((item["type"], item["name"]) for item in parameters)
            if key in signatures and signatures[key] != signature:
                raise ValueError(f"conflicting stub signature for {key!r}")
            if key in catalog:
                continue
            signatures[key] = signature
            catalog[key] = {
                "stub_key": key,
                "status": "active",
                "function_name": key,
                "return_type": return_type,
                "return_storage": "" if return_type == "void" else f"tessy_{key}",
                "parameters": parameters,
            }
    return catalog


def default_stub_body(catalog_entry, used) -> str:
    """Return the canonical body for one catalog entry."""
    return_type = catalog_entry["return_type"]
    if return_type == "void":
        return ""
    storage = catalog_entry["return_storage"]
    return f"extern {return_type} {storage};\nreturn {storage};" if used else f"{return_type} temp = {{0}};\nreturn temp;"


def _assembly_row(result: AssemblyResult, index: int, function) -> dict:
    try:
        row = result.rows[index]
    except (IndexError, TypeError) as exc:
        raise ValueError(f"assembly provenance points to missing row {index}") from exc
    return {
        "owner": "tool",
        "status": "active",
        "description": str(row.description),
        "test_method": "",
        "input": project_generated_values(row.values, function, "input"),
        "output": project_generated_values(row.expected, function, "output"),
    }


def _validated_provenance(function, result):
    decisions = tuple(getattr(function, "decisions", ()))
    rows, recipes, generated_rows = result.rows, result.recipes, result.generated_rows
    if not isinstance(recipes, (tuple, list)):
        raise ValueError("AssemblyResult.recipes must be a sequence")
    if not isinstance(generated_rows, (tuple, list)):
        raise ValueError("AssemblyResult.generated_rows must be a sequence")
    grouped = [[] for _ in decisions]
    for recipe_index, recipe in enumerate(recipes):
        if not isinstance(recipe, RecipeProvenance):
            raise ValueError(f"recipe {recipe_index} is not RecipeProvenance")
        decision_index = recipe.decision_index
        if type(decision_index) is not int or not 0 <= decision_index < len(decisions):
            raise ValueError(f"recipe decision_index is invalid: {decision_index!r}")
        decision = decisions[decision_index]
        if type(recipe.decision_id) is not int or recipe.decision_id != decision.id:
            raise ValueError(f"recipe decision_id does not match decision {decision_index}")
        atoms = tuple(atoms_of(decision.expr))
        if recipe.atom_texts != tuple(_atom_text(atom) for atom in atoms):
            raise ValueError(f"recipe atom_texts do not match decision {decision_index}")
        if (
            not isinstance(recipe.atom_values, tuple)
            or len(recipe.atom_values) != len(atoms)
            or any(type(value) is not bool for value in recipe.atom_values)
        ):
            raise ValueError(f"recipe atom_values are invalid for decision {decision_index}")
        if type(recipe.result) is not bool:
            raise ValueError(f"recipe result is not boolean for decision {decision_index}")
        outcome = eval_expr(decision.expr, dict(zip(atoms, recipe.atom_values)))
        if outcome is None or outcome != recipe.result:
            raise ValueError(f"recipe result does not match decision {decision_index}")
        _validate_concrete_index(recipe.concrete_row_index, rows, "recipe")
        grouped[decision_index].append(recipe)
    keys, expected = set(), {}
    for generated_index, item in enumerate(generated_rows):
        if not isinstance(item, GeneratedRowProvenance):
            raise ValueError(f"generated row {generated_index} is not GeneratedRowProvenance")
        if type(item.kind) is not str or item.kind not in _GENERATED_KINDS:
            raise ValueError(f"generated row kind is invalid: {item.kind!r}")
        if type(item.source_order) is not int or item.source_order < 0:
            raise ValueError(f"generated row source_order is invalid: {item.source_order!r}")
        key = item.kind, item.source_order
        if key in keys:
            raise ValueError(f"duplicate generated row reference: {key!r}")
        keys.add(key)
        order = expected.get(item.kind, 0)
        if item.source_order != order:
            raise ValueError(f"generated row source_order is not canonical for {item.kind!r}: expected {order}, got {item.source_order!r}")
        expected[item.kind] = order + 1
        _validate_concrete_index(item.concrete_row_index, rows, "generated row")
        if rows[item.concrete_row_index].kind != item.kind:
            raise ValueError(f"generated row kind {item.kind!r} does not match concrete row kind {rows[item.concrete_row_index].kind!r}")
    return grouped, tuple(generated_rows)


def _validate_concrete_index(index, rows, source):
    if type(index) is not int or index < 0 or index >= len(rows):
        raise ValueError(f"{source} concrete_row_index is invalid: {index!r}")


def _recipe_key(recipe):
    return recipe.decision_index, recipe.decision_id, recipe.atom_texts, recipe.atom_values, recipe.result


def _generated_result(generated_functions, name):
    try:
        item = generated_functions[name]
    except (KeyError, TypeError):
        raise ValueError(f"missing generated result for function {name!r}") from None
    result = item if isinstance(item, AssemblyResult) else getattr(item, "result", None)
    if not isinstance(result, AssemblyResult):
        raise TypeError(f"generated result for {name!r} must be an AssemblyResult")
    return result


def _functions(analysis):
    functions = getattr(analysis, "functions", None)
    if functions is None and isinstance(analysis, Mapping):
        functions = analysis.get("functions", ())
    return tuple(functions or ())


def _used_stub_keys(function):
    result = []
    for stub in getattr(function, "stubs", ()):
        key = str(stub.name)
        if key not in result:
            result.append(key)
    return result


def _stub_return_type(stub):
    ctype = getattr(stub, "ret_ctype", None)
    if ctype is not None:
        name = getattr(ctype, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError(f"stub {getattr(stub, 'name', '')!r} has an invalid return type")
        return name
    struct = getattr(stub, "ret_struct", None)
    if struct is not None:
        if not isinstance(struct, str) or not struct:
            raise ValueError(f"stub {getattr(stub, 'name', '')!r} has an invalid return type")
        return struct
    return "void"


def _stub_name(stub):
    name = getattr(stub, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError(f"stub name must be a non-empty string: {name!r}")
    return name


def _stub_parameters(stub):
    parameters = getattr(stub, "params", ())
    if not isinstance(parameters, (list, tuple)):
        raise ValueError(f"stub {getattr(stub, 'name', '')!r} parameters must be a sequence")
    result = []
    for parameter in parameters:
        if isinstance(parameter, Mapping):
            if set(parameter) != {"type", "name"}:
                raise ValueError(f"stub parameter mapping has invalid fields: {parameter!r}")
            type_name, name = parameter["type"], parameter["name"]
        elif isinstance(parameter, (list, tuple)) and len(parameter) == 2:
            type_name, name = parameter
        else:
            raise ValueError(f"stub parameter must be a type/name pair: {parameter!r}")
        if not isinstance(type_name, str) or not type_name:
            raise ValueError(f"stub parameter type must be a non-empty string: {type_name!r}")
        if not isinstance(name, str) or not name:
            raise ValueError(f"stub parameter name must be a non-empty string: {name!r}")
        result.append({"name": name, "type": type_name})
    return result


def _atom_text(atom):
    if getattr(atom, "text", ""):
        return str(atom.text)
    left = getattr(getattr(atom, "var", None), "name", "") or "<unknown>"
    operator = getattr(getattr(atom, "op", None), "value", getattr(atom, "op", ""))
    if operator == "truthy":
        return left
    right = getattr(atom, "rhs", None)
    if right is None:
        right = getattr(getattr(atom, "rhs_var", None), "name", "<unknown>")
    return f"{left} {operator} {right}"


def _expression_text(expression):
    if isinstance(expression, Atom):
        return _atom_text(expression)
    if isinstance(expression, Not):
        return f"!({_expression_text(expression.child)})"
    if isinstance(expression, And):
        return "(" + " && ".join(_expression_text(child) for child in expression.children) + ")"
    if isinstance(expression, Or):
        return "(" + " || ".join(_expression_text(child) for child in expression.children) + ")"
    return str(expression)


def _relative_source(value):
    text = str(value).replace("\\", "/")
    path = Path(text)
    if path.is_absolute() or ":" in text or ".." in text.split("/"):
        return path.name
    return text.lstrip("/") or "module.c"


__all__ = [
    "DecisionMatch",
    "MergeResult",
    "UserMergeResult",
    "build_candidate",
    "build_stub_catalog",
    "default_stub_body",
    "match_decisions",
    "merge_user",
    "merge_tool",
]
