"""Build the plain-dict candidate state produced by one tool generation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from utgen.iteration.state import allocate_decision_key, allocate_row_id
from utgen.iteration.validation import validate_module
from utgen.iteration.values import metadata_variables, project_generated_values
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


__all__ = ["build_candidate", "build_stub_catalog", "default_stub_body"]
