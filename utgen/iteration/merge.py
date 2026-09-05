"""Build the plain-dict candidate state produced by one tool generation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from utgen.iteration.state import (
    _metadata_variables,
    allocate_decision_key,
    allocate_row_id,
    nest_values,
    validate_module,
)
from utgen.model import And, AssemblyResult, Atom, Not, Or, atoms_of


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
        function_state = {
            "max_decision_number": 0,
            "variables": _metadata_variables(function),
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

            for recipe in generated.recipes:
                if recipe.decision_index != decision_index:
                    continue
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

        for generated_row in generated.generated_rows:
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
            key = str(stub.name)
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
        "input": _nested_channel(row.values, function, "input"),
        "output": _nested_channel(row.expected, function, "output"),
    }


def _nested_channel(values, function, channel):
    variables = _metadata_variables(function)
    selected = {
        name: spec
        for name, spec in variables.items()
        if _direction(spec.get("direction")) == channel
        or _direction(spec.get("direction")) == "inout"
    }
    if not selected:
        return {}
    flat = _generated_flat_values(values, function, selected, channel)
    return nest_values(flat, variables, channel)


def _generated_flat_values(values, function, selected, channel):
    values = values if isinstance(values, Mapping) else {}
    leaves = {
        leaf.name: leaf
        for leaf in getattr(function, "leaves", ())
        if _direction(getattr(leaf, "direction", None)) == channel
        or _direction(getattr(leaf, "direction", None)) == "inout"
    }
    schema_paths = _schema_leaf_paths(selected)
    leaf_patterns = {
        _array_pattern(name): leaf for name, leaf in leaves.items()
    }
    flat = {}
    for name, spec in selected.items():
        if name in values and name not in leaves:
            _flatten_nested(values[name], spec, name, flat, leaf_patterns)
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


def _flatten_nested(value, spec, path, result, leaf_patterns):
    dimensions = spec.get("dimensions", [])
    if dimensions:
        for index in range(dimensions[0]):
            child = value[index] if isinstance(value, list) and index < len(value) else 0
            _flatten_nested(
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
            _flatten_nested(child, child_spec, f"{path}.{name}", result, leaf_patterns)
        return
    result[path] = _lexeme(value, leaf_patterns.get(_array_pattern(path)))


def _array_pattern(path):
    return re.sub(r"\[[0-9]+\]", "[]", path)


def _schema_leaf_paths(variables):
    paths = []
    for name, spec in variables.items():
        _collect_leaf_paths(spec, name, paths)
    return paths


def _collect_leaf_paths(spec, path, result):
    dimensions = spec.get("dimensions", [])
    if dimensions:
        for index in range(dimensions[0]):
            _collect_leaf_paths(
                {**spec, "dimensions": dimensions[1:]},
                f"{path}[{index}]",
                result,
            )
        return
    fields = spec.get("fields")
    if fields is not None:
        for name, child in fields.items():
            _collect_leaf_paths(child, f"{path}.{name}", result)
        return
    result.append(path)


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
    return str(getattr(ctype, "name", "") or getattr(stub, "ret_struct", None) or "void")


def _stub_parameters(stub):
    result = []
    for parameter in getattr(stub, "params", ()) or ():
        if isinstance(parameter, Mapping):
            type_name, name = parameter.get("type"), parameter.get("name")
        else:
            try:
                type_name, name = parameter[:2]
            except (TypeError, IndexError):
                type_name = name = ""
        result.append({"name": str(name), "type": str(type_name)})
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


def _direction(value):
    value = getattr(value, "value", value)
    return {"in": "input", "out": "output"}.get(value, value)


def _relative_source(value):
    text = str(value).replace("\\", "/")
    path = Path(text)
    if path.is_absolute() or ":" in text or ".." in text.split("/"):
        return path.name
    return text.lstrip("/") or "module.c"


__all__ = ["build_candidate", "build_stub_catalog", "default_stub_body"]
