"""Build the plain-dict candidate state produced by one tool generation."""

from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from utgen.iteration.state import allocate_decision_key, allocate_row_id
from utgen.iteration.validation import validate_module
from utgen.iteration.values import metadata_variables, project_generated_values, sync_row_schema
from utgen.mcdc import eval_expr
from utgen.model import (
    And,
    AssemblyResult,
    Atom,
    GeneratedRowProvenance,
    Not,
    Or,
    RecipeProvenance,
    atoms_of,
)

_GENERATED_KINDS = frozenset({"Nominal", "Timeout"})

@dataclass(frozen=True)
class DecisionMatch:
    """A candidate Decision and its first unused exact current match."""

    candidate_key: str
    current_key: str | None

@dataclass(frozen=True)
class MergeResult:
    state: dict
    summary: dict

def match_decisions(current_function: dict, candidate_function: dict) -> list[DecisionMatch]:
    """Match candidate Decisions by readable structure in candidate source order."""
    current = current_function.get("decisions", {})
    candidate = candidate_function.get("decisions", {})
    current_items = tuple(current.items())
    candidate_items = tuple(candidate.items())
    used: set[str] = set()
    matches: list[str | None] = [None] * len(candidate_items)
    for exact in (True, False):
        structure = _decision_structure if exact else _decision_core
        for index, (_, item) in enumerate(candidate_items):
            if matches[index] is not None:
                continue
            key = next(
                (
                    key
                    for key, old in current_items
                    if key not in used and structure(old) == structure(item)
                ),
                None,
            )
            if key is not None:
                matches[index] = key
                used.add(key)
    return [DecisionMatch(item[0], matches[index]) for index, item in enumerate(candidate_items)]

def merge_tool(current: dict, candidate: dict) -> MergeResult:
    """Merge one regenerated candidate into the current module state."""
    validate_module(current)
    validate_module(candidate)
    if (
        current["project"],
        current["module"],
        current["source_file"],
    ) != (
        candidate["project"],
        candidate["module"],
        candidate["source_file"],
    ):
        raise ValueError("current and candidate module identities do not match")
    state = deepcopy(current)
    state.update(
        {
            "project": candidate["project"],
            "module": candidate["module"],
            "source_file": candidate["source_file"],
            "module_input_hash": candidate["module_input_hash"],
        }
    )
    state["stub_catalog"] = _merge_stub_catalog(
        current["stub_catalog"], candidate["stub_catalog"]
    )
    counters = {
        "decisions_matched": 0,
        "decisions_added": 0,
        "decisions_orphaned": 0,
        "rows_kept": 0,
        "rows_allocated": 0,
        "rows_orphaned": 0,
    }

    current_functions = current["functions"]
    candidate_functions = candidate["functions"]
    for name in set(current_functions) - set(candidate_functions):
        _orphan_function(state["functions"][name], counters)

    for name, candidate_function in candidate_functions.items():
        if name not in current_functions:
            function = _new_function(state, candidate_function, counters)
        else:
            function = _merge_function(
                state,
                current_functions[name],
                candidate_function,
                counters,
            )
        state["functions"][name] = function

    validate_module(state)
    summary = {
        "decisions": {
            "matched": counters["decisions_matched"],
            "added": counters["decisions_added"],
            "orphaned": counters["decisions_orphaned"],
        },
        "rows": {
            "kept": counters["rows_kept"],
            "allocated": counters["rows_allocated"],
            "orphaned": counters["rows_orphaned"],
        },
        "stubs": {
            "active": sum(
                entry["status"] == "active" for entry in state["stub_catalog"].values()
            ),
            "orphaned": sum(
                entry["status"] == "orphaned" for entry in state["stub_catalog"].values()
            ),
        },
    }
    return MergeResult(state, summary)

def _decision_core(decision: dict) -> tuple:
    atoms = decision.get("atoms", {})
    return decision.get("kind", "decision"), decision["expression"], tuple(atoms.values())

def _decision_structure(decision: dict) -> tuple:
    atoms = decision.get("atoms", {})
    return (
        _decision_core(decision),
        tuple((recipe["result"], tuple(recipe["values"][key] for key in atoms))
              for recipe in decision["recipes"]),
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
    function["max_decision_number"] = 0
    function["decisions"] = {}
    function["generated_rows"] = []
    function["rows"] = {}
    candidate_rows = candidate["rows"]

    for candidate_key, decision in candidate["decisions"].items():
        key = allocate_decision_key(function)
        merged_decision = deepcopy(decision)
        merged_decision["recipes"] = []
        for recipe in decision["recipes"]:
            row_id = allocate_row_id(state)
            function["rows"][row_id] = _tool_row(candidate_rows[recipe["row_id"]])
            merged_recipe = deepcopy(recipe)
            merged_recipe["row_id"] = row_id
            merged_decision["recipes"].append(merged_recipe)
            counters["rows_allocated"] += 1
        function["decisions"][key] = merged_decision
        counters["decisions_added"] += 1

    for item in candidate["generated_rows"]:
        row_id = allocate_row_id(state)
        function["rows"][row_id] = _tool_row(candidate_rows[item["row_id"]])
        function["generated_rows"].append({"row_id": row_id, "kind": item["kind"]})
        counters["rows_allocated"] += 1

    linked = {
        recipe["row_id"]
        for decision in candidate["decisions"].values()
        for recipe in decision["recipes"]
    }
    linked.update(item["row_id"] for item in candidate["generated_rows"])
    for old_row_id, row in candidate_rows.items():
        if old_row_id in linked:
            continue
        row_id = allocate_row_id(state)
        function["rows"][row_id] = deepcopy(row)
        counters["rows_allocated"] += 1
    function["stubs"] = _merge_stubs({}, candidate, state["stub_catalog"])
    return function


def _merge_function(state: dict, current: dict, candidate: dict, counters: dict) -> dict:
    function = deepcopy(current)
    function["variables"] = deepcopy(candidate["variables"])
    for row in function["rows"].values():
        sync_row_schema(row, current["variables"], candidate["variables"])

    matches = match_decisions(current, candidate)
    matched_current = {item.current_key for item in matches if item.current_key is not None}
    decisions = {}
    for item in matches:
        candidate_decision = candidate["decisions"][item.candidate_key]
        if item.current_key is None:
            key = allocate_decision_key(function)
            counters["decisions_added"] += 1
            decisions[key] = _new_decision_rows(
                state, function, candidate, candidate_decision, counters
            )
            continue

        key = item.current_key
        counters["decisions_matched"] += 1
        decisions[key] = _matched_decision_rows(
            state,
            function,
            current["decisions"][key],
            candidate,
            candidate_decision,
            counters,
        )

    for key, old_decision in current["decisions"].items():
        if key in matched_current:
            continue
        orphaned = deepcopy(function["decisions"][key])
        orphaned["status"] = "orphaned"
        for recipe in orphaned["recipes"]:
            _mark_orphan(function["rows"].get(recipe["row_id"]), counters)
        decisions[key] = orphaned
        if old_decision["status"] != "orphaned":
            counters["decisions_orphaned"] += 1
    function["decisions"] = decisions

    generated = []
    used_current: set[int] = set()
    old_generated = current["generated_rows"]
    for item in candidate["generated_rows"]:
        index = next(
            (
                index
                for index, old in enumerate(old_generated)
                if index not in used_current and old["kind"] == item["kind"]
            ),
            None,
        )
        if index is None:
            row_id = allocate_row_id(state)
            function["rows"][row_id] = _tool_row(candidate["rows"][item["row_id"]])
            counters["rows_allocated"] += 1
        else:
            used_current.add(index)
            row_id = old_generated[index]["row_id"]
            _merge_row(function["rows"][row_id], candidate["rows"][item["row_id"]], counters)
        generated.append({"row_id": row_id, "kind": item["kind"]})
    for index, item in enumerate(old_generated):
        if index not in used_current:
            _mark_orphan(function["rows"].get(item["row_id"]), counters)
            generated.append(deepcopy(item))
    function["generated_rows"] = generated
    function["used_stub_keys"] = deepcopy(candidate["used_stub_keys"])
    function["stubs"] = _merge_stubs(current, candidate, state["stub_catalog"])
    return function


def _new_decision_rows(state, function, candidate, decision, counters):
    result = deepcopy(decision)
    result["status"] = "active"
    result["recipes"] = []
    for recipe in decision["recipes"]:
        row_id = allocate_row_id(state)
        function["rows"][row_id] = _tool_row(candidate["rows"][recipe["row_id"]])
        item = deepcopy(recipe)
        item["row_id"] = row_id
        result["recipes"].append(item)
        counters["rows_allocated"] += 1
    return result


def _matched_decision_rows(
    state, function, old_decision, candidate, decision, counters
):
    result = deepcopy(decision)
    result["status"] = "active"
    result["recipes"] = []
    used: set[int] = set()
    for recipe in decision["recipes"]:
        index = next(
            (
                index
                for index, old in enumerate(old_decision["recipes"])
                if index not in used
                and _recipe_structure(old_decision, old) == _recipe_structure(decision, recipe)
            ),
            None,
        )
        if index is None:
            row_id = allocate_row_id(state)
            function["rows"][row_id] = _tool_row(candidate["rows"][recipe["row_id"]])
            counters["rows_allocated"] += 1
        else:
            used.add(index)
            row_id = old_decision["recipes"][index]["row_id"]
            _merge_row(function["rows"][row_id], candidate["rows"][recipe["row_id"]], counters)
        item = deepcopy(recipe)
        item["row_id"] = row_id
        result["recipes"].append(item)
    for index, old in enumerate(old_decision["recipes"]):
        if index not in used:
            _mark_orphan(function["rows"].get(old["row_id"]), counters)
            result["recipes"].append(deepcopy(old))
    return result


def _merge_row(current: dict, candidate: dict, counters: dict) -> None:
    if current["owner"] == "user":
        counters["rows_kept"] += 1
        return
    current.clear()
    current.update(_tool_row(candidate))
    counters["rows_kept"] += 1


def _tool_row(row: dict) -> dict:
    result = deepcopy(row)
    result["owner"] = "tool"
    result["status"] = "active"
    return result


def _mark_orphan(row: dict | None, counters: dict) -> None:
    if row is None:
        return
    if row["status"] != "orphaned":
        counters["rows_orphaned"] += 1
    row["status"] = "orphaned"


def _merge_stubs(current: dict, candidate: dict, catalog: dict) -> dict:
    current_stubs = current.get("stubs", {})
    candidate_stubs = candidate.get("stubs", {})
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
        incoming = candidate_stubs.get(key)
        if incoming is None:
            incoming = {
                "owner": "tool",
                "status": "active",
                "body": default_stub_body(entry, key in used),
            }
        if old is not None and old["owner"] == "user":
            merged[key] = deepcopy(old)
        else:
            merged[key] = _tool_stub(incoming)
    for key, old in current_stubs.items():
        if key in merged:
            continue
        item = deepcopy(old)
        item["status"] = "orphaned"
        merged[key] = item
    return merged


def _tool_stub(stub: dict) -> dict:
    result = deepcopy(stub)
    result["owner"] = "tool"
    result["status"] = "active"
    return result


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
    """Project Analysis/AssemblyResult objects into one validated module dict."""

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
            decision_state = {
                "status": "active",
                "expression": _expression_text(decision.expr),
                "atoms": {
                    f"A{index}": _atom_text(atom)
                    for index, atom in enumerate(atoms, 1)
                },
                "recipes": [],
            }
            if getattr(decision, "kind", ""):
                decision_state["kind"] = str(decision.kind)
            if getattr(decision, "location", ""):
                decision_state["location"] = str(decision.location)
            function_state["decisions"][decision_key] = decision_state

            seen_recipes = set()
            for recipe in recipes_by_decision[decision_index]:
                logical_key = _recipe_key(recipe)
                if logical_key in seen_recipes:
                    continue
                seen_recipes.add(logical_key)
                row = _assembly_row(generated, recipe.concrete_row_index, function)
                row_id = allocate_row_id(state)
                state["functions"][name]["rows"][row_id] = row
                decision_state["recipes"].append(
                    {
                        "row_id": row_id,
                        "result": bool(recipe.result),
                        "values": {
                            f"A{index}": bool(value)
                            for index, value in enumerate(recipe.atom_values, 1)
                        },
                    }
                )

        for generated_row in generated_rows:
            row = _assembly_row(generated, generated_row.concrete_row_index, function)
            row_id = allocate_row_id(state)
            function_state["rows"][row_id] = row
            function_state["generated_rows"].append(
                {"row_id": row_id, "kind": str(generated_row.kind)}
            )

        for key in state["stub_catalog"]:
            entry = state["stub_catalog"][key]
            function_state["stubs"][key] = {
                "owner": "tool",
                "status": "active",
                "body": default_stub_body(
                    entry,
                    key in function_state["used_stub_keys"],
                ),
            }

    validate_module(state)
    return state


def build_stub_catalog(analysis) -> dict:
    """Collect one active, signature-checked catalog from all module functions."""

    catalog = {}
    signatures = {}
    for function in _functions(analysis):
        for stub in getattr(function, "stubs", ()):
            key = _stub_name(stub)
            return_type = _stub_return_type(stub)
            parameters = _stub_parameters(stub)
            signature = return_type, tuple(
                (item["type"], item["name"]) for item in parameters
            )
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
    """Return the canonical per-function body for one catalog entry."""

    return_type = catalog_entry["return_type"]
    if return_type == "void":
        return ""
    storage = catalog_entry["return_storage"]
    if used:
        return f"extern {return_type} {storage};\nreturn {storage};"
    return f"{return_type} temp = {{0}};\nreturn temp;"


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
    rows = result.rows
    recipes = result.recipes
    generated_rows = result.generated_rows
    if not isinstance(recipes, (tuple, list)):
        raise ValueError("AssemblyResult.recipes must be a sequence")
    if not isinstance(generated_rows, (tuple, list)):
        raise ValueError("AssemblyResult.generated_rows must be a sequence")

    recipes_by_decision = [[] for _ in decisions]
    for recipe_index, recipe in enumerate(recipes):
        if not isinstance(recipe, RecipeProvenance):
            raise ValueError(f"recipe {recipe_index} is not RecipeProvenance")
        decision_index = recipe.decision_index
        if type(decision_index) is not int or not 0 <= decision_index < len(decisions):
            raise ValueError(f"recipe decision_index is invalid: {decision_index!r}")
        decision = decisions[decision_index]
        if type(recipe.decision_id) is not int or recipe.decision_id != decision.id:
            raise ValueError(
                f"recipe decision_id does not match decision {decision_index}"
            )
        atoms = tuple(atoms_of(decision.expr))
        expected_texts = tuple(_atom_text(atom) for atom in atoms)
        if recipe.atom_texts != expected_texts:
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
            raise ValueError(
                f"recipe result does not match decision {decision_index}"
            )
        _validate_concrete_index(recipe.concrete_row_index, rows, "recipe")
        recipes_by_decision[decision_index].append(recipe)

    generated_keys = set()
    expected_orders = {}
    for generated_index, generated_row in enumerate(generated_rows):
        if not isinstance(generated_row, GeneratedRowProvenance):
            raise ValueError(f"generated row {generated_index} is not GeneratedRowProvenance")
        if type(generated_row.kind) is not str or generated_row.kind not in _GENERATED_KINDS:
            raise ValueError(f"generated row kind is invalid: {generated_row.kind!r}")
        if type(generated_row.source_order) is not int or generated_row.source_order < 0:
            raise ValueError(
                f"generated row source_order is invalid: {generated_row.source_order!r}"
            )
        key = generated_row.kind, generated_row.source_order
        if key in generated_keys:
            raise ValueError(f"duplicate generated row reference: {key!r}")
        generated_keys.add(key)
        expected_order = expected_orders.get(generated_row.kind, 0)
        if generated_row.source_order != expected_order:
            raise ValueError(
                "generated row source_order is not canonical for "
                f"{generated_row.kind!r}: expected {expected_order}, "
                f"got {generated_row.source_order!r}"
            )
        expected_orders[generated_row.kind] = expected_order + 1
        _validate_concrete_index(generated_row.concrete_row_index, rows, "generated row")
        concrete_row = rows[generated_row.concrete_row_index]
        if concrete_row.kind != generated_row.kind:
            raise ValueError(
                f"generated row kind {generated_row.kind!r} does not match "
                f"concrete row kind {concrete_row.kind!r}"
            )
    return recipes_by_decision, tuple(generated_rows)


def _validate_concrete_index(index, rows, source):
    if type(index) is not int or index < 0 or index >= len(rows):
        raise ValueError(f"{source} concrete_row_index is invalid: {index!r}")


def _recipe_key(recipe):
    return (
        recipe.decision_index,
        recipe.decision_id,
        recipe.atom_texts,
        recipe.atom_values,
        recipe.result,
    )


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
    "build_candidate",
    "build_stub_catalog",
    "default_stub_body",
    "match_decisions",
    "merge_tool",
]
