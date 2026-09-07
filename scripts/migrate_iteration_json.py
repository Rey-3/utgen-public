"""Migrate the legacy TESSY XML/Excel export into current iteration JSON.

Legacy parsing lives here on purpose.  The ordinary generation and merge
workflows do not import this boundary script or fall back to XML.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Mapping
import xml.etree.ElementTree as ET

if __package__ in {None, ""}:  # direct ``python scripts/...`` execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import yaml
except ImportError:  # JSON remains a supported CLI format without PyYAML.
    yaml = None

from tessy_parse import expandArrayName, expandArrayRanges
from utgen.assembler import RowAssembler
from utgen.mcdc import McdcGenerator
from utgen.solver import Solver
from utgen.iteration.adapters.analysis_db import AnalysisDbAdapter, AnalysisDbWriter
from utgen.iteration.adapters.tessy_workbook import TessyWorkbookAdapter
from utgen.iteration.merge import (
    _ordered_active_rows,
    build_candidate,
    build_stub_catalog,
    default_stub_body,
)
from utgen.iteration.state import load_module, module_paths, write_module
from utgen.iteration.validation import validate_module
from utgen.iteration.values import (
    _schema_leaves as _value_schema_leaves,
    _metadata_path_tokens,
    flatten_values,
    metadata_variables,
    nest_values,
)
_FORMAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*_[0-9]{3,}$")

@dataclass
class MigrationReport:
    functions: int = 0
    rows: int = 0
    stubs: int = 0
    ambiguities: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"functions": self.functions, "rows": self.rows,
                "stubs": self.stubs, "ambiguities": self.ambiguities,
                "errors": self.errors, "writes": self.writes}

    def add(self, other: "MigrationReport") -> None:
        self.functions += other.functions
        self.rows += other.rows
        self.stubs += other.stubs
        self.ambiguities.extend(other.ambiguities)
        self.errors.extend(other.errors)
        self.writes.extend(other.writes)

@dataclass(frozen=True)
class _Arg:
    name: str
    direction: str
    fmt: str = "unsigned"
    ranges: str = ""
    output: str = ""
    row_ids: tuple[str, ...] = ()
    pointer: bool = False
    address_of: str = ""

@dataclass
class _LegacyFunction:
    name: str
    args: list[_Arg] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    row_ids: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class _Stub:
    key: str
    return_type: str
    parameters: tuple[dict[str, str], ...] = ()
    storage: str | None = None

def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].lower()

def _child(node: ET.Element, name: str) -> ET.Element | None:
    name = name.lower()
    return next((item for item in list(node) if _tag(item) == name), None)

def _text(value: Any, default: str = "") -> str:
    return default if value is None else str(value)

def _resolve(value: Any, base: Path) -> Path:
    path = Path(os.fspath(value))
    return path if path.is_absolute() else base / path


def _direct_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("project_config must be a mapping")
    if value.get("module") or value.get("module_name"):
        return dict(value)
    modules = value.get("modules")
    if isinstance(modules, Mapping) and modules:
        name, module = next(iter(modules.items()))
        item = dict(module) if isinstance(module, Mapping) else {}
        item.setdefault("module", name)
    elif isinstance(modules, (list, tuple)) and modules:
        module = modules[0]
        item = dict(module) if isinstance(module, Mapping) else {"module": module}
    else:
        raise ValueError("configuration does not identify a module")
    if "project" not in item and "project_name" not in item and value.get("name"):
        item["project"] = value["name"]
    for key in ("project", "project_name", "source_file", "state_root", "config_dir", "excel_output_dir"):
        if key in value and key not in item:
            item[key] = value[key]
    return item


def _identity(config: Mapping[str, Any], module_hint: str | None = None) -> tuple[str, str]:
    project = config.get("project") or config.get("project_name") or config.get("name")
    module = config.get("module") or config.get("module_name") or module_hint
    if not isinstance(project, str) or not project:
        raise ValueError("configuration is missing project")
    if not isinstance(module, str) or not module:
        raise ValueError("configuration is missing module")
    if any(ch in project + module for ch in "/\\:\x00"):
        raise ValueError("project/module must be a safe path component")
    return project, module

def _source_file(config: Mapping[str, Any], module: str, metadata: Any) -> str:
    value = config.get("relative_path") or config.get("source_file") or config.get("source")
    value = _text(value or getattr(metadata, "source_file", "") or module)
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or ":" in normalized or ".." in normalized.split("/"):
        raise ValueError("source_file must be a non-empty relative path")
    return value

def _paths(config: Mapping[str, Any], root: Path, module: str) -> tuple[Path, Path, Path, Path]:
    base = Path(config.get("config_dir") or config.get("base_dir") or Path.cwd())
    working_value = config.get("legacy_working_dir") or config.get("working_dir")
    working = _resolve(working_value, base) if working_value else root / "iteration" / "modules" / Path(module).stem / "working"
    db_value = config.get("analysis_db") or config.get("analysis_path") or config.get("db_path")
    if db_value:
        database = _resolve(db_value, base)
    else:
        database = working / f"db_{Path(module).stem}.xml"
        if not database.exists():
            database = working / f"db_{module}.xml"
    excel_value = config.get("excel_input_dir") or config.get("workbook_dir") or config.get("excel_dir")
    excel_in = _resolve(excel_value, base) if excel_value else None
    out_value = config.get("excel_output_dir") or config.get("current_excel_dir")
    excel_out = _resolve(out_value, base) if out_value else (excel_in or root / "iteration" / "current_excel" / Path(module).stem)
    return working, database, excel_in, excel_out

def _read_root(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"cannot parse legacy XML: {path}") from exc

def _direction(value: Any) -> str:
    value = _text(value, "in").strip().lower()
    return {"in": "input", "i": "input", "input": "input",
            "out": "output", "o": "output", "output": "output"}.get(value, "inout" if value in {"io", "inout", "in/out"} else "input")

def _ids(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[,|]", value or "") if part.strip())

def _read_values(paths: list[Path], report: MigrationReport) -> dict[str, _LegacyFunction]:
    result: dict[str, _LegacyFunction] = {}
    for path in sorted(paths, key=lambda item: item.name.lower()):
        try:
            root = _read_root(path)
            name = root.get("name") or root.get("function_name")
            if not name:
                report.errors.append(f"{path}: missing function name")
                continue
            if name in result:
                report.ambiguities.append(f"duplicate Value XML function {name!r}: {path}")
            function = result.setdefault(name, _LegacyFunction(name))
            for node in root.iter():
                if _tag(node) != "argument":
                    continue
                arg = _Arg(
                    node.get("name", ""), _direction(node.get("direction")),
                    node.get("format") or node.get("type") or "unsigned",
                    node.get("range") or node.get("input") or "",
                    node.get("output") or node.get("expected") or "",
                    _ids(node.get("row_ids") or node.get("ids") or node.get("row_id")),
                    node.get("pointer", "false").strip().lower() in {"1", "true", "yes"},
                    node.get("address_of", ""),
                )
                if not arg.name:
                    report.errors.append(f"{path}: argument is missing name")
                    continue
                function.args.append(arg)
                if arg.row_ids and not function.row_ids:
                    function.row_ids = list(arg.row_ids)
                elif arg.row_ids and tuple(function.row_ids) != arg.row_ids:
                    report.ambiguities.append(f"{path}: argument row IDs disagree")
            for node in root.iter():
                if _tag(node) in {"description", "row", "testcase"}:
                    value = node.get("description") or (node.text or "").strip()
                    if value:
                        function.descriptions.append(value)
        except ValueError as exc:
            report.errors.append(str(exc))
    return result

def _split(value: str) -> list[str]:
    return [] if value == "" else [item.strip() for item in value.split(",")]

def _arg_values(arg: _Arg, output: bool) -> dict[str, list[str]]:
    try:
        paths = list(expandArrayName(arg.name)) or [arg.name]
    except Exception:
        paths = [arg.name]
    raw = arg.output if output else arg.ranges
    try:
        groups = expandArrayRanges(raw, len(paths))
    except Exception:
        groups = [raw] * len(paths)
    return {re.sub(r"\(\)$", "", path): _split(groups[i] if i < len(groups) else raw)
            for i, path in enumerate(paths)}

def _stub_xml(path: Path, function: str) -> tuple[dict[str, str], dict[str, _Stub]]:
    root = _read_root(path)
    root_function = root.get("function_name") or root.get("name")
    if root_function and root_function != function:
        return {}, {}
    bodies: dict[str, str] = {}
    signatures: dict[str, _Stub] = {}
    called = [node for node in root.iter() if _tag(node) == "function"]
    for node in called:
        key = node.get("name") or ""
        if not key:
            continue
        ret = _child(node, "return")
        ret_type = node.get("return") or node.get("type") or "void"
        if ret is not None:
            type_node = _child(ret, "type")
            ret_type = _text(type_node.text if type_node is not None else None, ret_type)
        params = []
        for param in node.iter():
            if _tag(param) not in {"param", "parameter", "argument"}:
                continue
            name = param.get("name") or ""
            ptype = param.get("type") or ""
            type_node = _child(param, "type")
            ptype = _text(type_node.text if type_node is not None else None, ptype)
            if name and ptype:
                params.append({"name": name, "type": ptype})
        storage_node = _child(ret, "name") if ret is not None else None
        storage = storage_node.text.strip() if storage_node is not None and storage_node.text else None
        signatures[key] = _Stub(key, ret_type, tuple(params), storage)
        body = _child(node, "body")
        if body is not None and body.text:
            bodies[key] = body.text
    if len(signatures) == 1 and not bodies:
        body = _child(root, "body")
        if body is not None and body.text:
            bodies[next(iter(signatures))] = body.text
    return bodies, signatures

def _db(path: Path, report: MigrationReport) -> tuple[Any, Any]:
    try:
        metadata, analysis = AnalysisDbAdapter().read_module(path, require_metadata=False)
    except Exception as exc:
        report.errors.append(f"Analysis DB {path}: {exc}")
        return None, None
    return metadata, analysis

def _analysis_signatures(function: Any | None, catalog: Mapping[str, Any]) -> dict[str, _Stub]:
    names = {stub.name for stub in getattr(function, "stubs", ())}
    return {key: _Stub(key, entry["return_type"], tuple(entry["parameters"]), entry["return_storage"] or None)
            for key, entry in catalog.items() if key in names}

def _add_variable(variables: dict[str, dict[str, Any]], arg: _Arg, logical: bool) -> None:
    tokens = _metadata_path_tokens(re.sub(r"\(\)$", "", arg.name))
    if not tokens or not isinstance(tokens[0], str):
        raise ValueError(f"unsupported legacy variable path: {arg.name!r}")
    root = variables.setdefault(tokens[0], {"type": arg.fmt or "unsigned", "direction": arg.direction, "dimensions": []})
    if arg.pointer:
        root["pointer"] = True
    if arg.address_of:
        root["address_of"] = arg.address_of
    if root.get("direction") != arg.direction and {root.get("direction"), arg.direction} <= {"input", "output"}:
        root["direction"] = "inout"
    current, slot = root, 0
    for token in tokens[1:]:
        if isinstance(token, int):
            dims = current.setdefault("dimensions", [])
            extent = max(token if logical else token + 1, 1)
            if slot < len(dims):
                dims[slot] = max(dims[slot], extent)
            else:
                dims.append(extent)
            slot += 1
            continue
        fields = current.setdefault("fields", {})
        current = fields.setdefault(token, {"type": arg.fmt or "unsigned", "dimensions": []})
        slot = 0
    current.setdefault("type", arg.fmt or "unsigned")
    current.setdefault("dimensions", [])

def _schema_leaves(variables: Mapping[str, Any], channel: str) -> list[str]:
    selected = {name: spec for name, spec in variables.items()
                if spec.get("direction") in ({"input", "inout"} if channel == "input" else {"output", "inout"})}
    return list(_value_schema_leaves(selected))

def _variables(legacy: _LegacyFunction, analysis_function: Any | None, columns: list[Any], report: MigrationReport):
    try:
        result = deepcopy(metadata_variables(analysis_function)) if analysis_function is not None else {}
    except Exception as exc:
        report.errors.append(f"{legacy.name}: Analysis DB metadata is invalid: {exc}")
        result = {}
    for arg in legacy.args:
        try:
            logical = any(int(value) > 0 for value in re.findall(r"\[([0-9]+)\]", arg.name))
            _add_variable(result, arg, logical)
        except ValueError as exc:
            report.errors.append(f"{legacy.name}: {exc}")
    for column in columns:
        name = getattr(column, "name", "")
        if not name:
            continue
        try:
            _add_variable(result, _Arg(name, _direction(getattr(column, "direction", "inout")),
                                       getattr(column, "raw_type_hint", None) or "unsigned"), False)
        except ValueError as exc:
            report.errors.append(f"{legacy.name}: {exc}")
    return result

def _baseline(legacy: _LegacyFunction, variables: Mapping[str, Any]) -> list[dict[str, Any]]:
    inputs: dict[str, list[str]] = {}
    outputs: dict[str, list[str]] = {}
    count = 0
    for arg in legacy.args:
        if arg.direction in {"input", "inout"}:
            for path, values in _arg_values(arg, False).items():
                inputs[path] = values
                count = max(count, len(values))
        if arg.direction in {"output", "inout"}:
            for path, values in _arg_values(arg, True).items():
                outputs[path] = values
                count = max(count, len(values))
    count = count or (1 if legacy.args else 0)
    result = []
    for index in range(count):
        result.append({
            "input": {path: values[index] if index < len(values) else "" for path, values in inputs.items()},
            "output": {path: values[index] if index < len(values) else "" for path, values in outputs.items()},
            "description": legacy.descriptions[index] if index < len(legacy.descriptions) else "",
        })
    for row in result:
        for channel in ("input", "output"):
            row[channel].update({path: "" for path in _schema_leaves(variables, channel) if path not in row[channel]})
    return result


def _workbook(directory: Path | None, project: str, module: str, function: str, report: MigrationReport) -> Any | None:
    if directory is None or not directory.exists():
        return None
    path = directory / f"{function}.xlsx"
    if not path.exists():
        return None
    try:
        return TessyWorkbookAdapter().read_complete_export(
            path, expected_module=Path(module).name,
            expected_testobject=function, expected_project=f"tessy_{project}")
    except Exception as exc:
        report.errors.append(f"Excel {path}: {exc}")
        return None

def _combine(rows, snapshot, variables, report):
    if snapshot is None:
        return rows, [None] * len(rows), {}
    result, ids = [], []
    for index, case in enumerate(snapshot.cases):
        base = rows[index] if index < len(rows) else {
            "input": {path: "" for path in _schema_leaves(variables, "input")},
            "output": {path: "" for path in _schema_leaves(variables, "output")}, "description": ""}
        row = {"input": dict(base["input"]), "output": dict(base["output"]), "description": base["description"]}
        for channel, cells in (("input", case.input_cells), ("output", case.output_cells)):
            for key, cell in cells.items():
                if cell.kind == "missing":
                    continue
                path = re.sub(r"\(\)$", "", key.canonical_name)
                value = cell.raw_text if cell.raw_text is not None else ""
                if path in row[channel]:
                    row[channel][path] = value
        # Properties is present in a valid export, so even an explicitly
        # empty description is an Excel value and takes precedence.
        row["description"] = case.state_description
        result.append(row)
        ids.append(case.external_test_case_id)
    if len(rows) > len(result):
        result.extend(rows[len(result):])
        ids.extend([None] * (len(rows) - len(result)))
    bodies = {stub.name: stub.body_raw for stub in snapshot.stubs}
    return result, ids, bodies

def _valid_id(value, prefix):
    match = _FORMAL_ID.fullmatch(value) if isinstance(value, str) else None
    return bool(match and value.rsplit("_", 1)[0] == prefix and int(value.rsplit("_", 1)[1]))

def _allocate(prefix, records, existing, report):
    highest = int(existing.get("max_row_number", 0)) if existing else 0
    if existing:
        for function in existing.get("functions", {}).values():
            for value in function.get("rows", {}):
                if _valid_id(value, prefix):
                    highest = max(highest, int(value.rsplit("_", 1)[1]))
    for _name, _rows, ids in records:
        for value in ids:
            if _valid_id(value, prefix):
                highest = max(highest, int(value.rsplit("_", 1)[1]))
    allocated, seen = {}, set()
    for name, rows, ids in records:
        old_ids = list((existing or {}).get("functions", {}).get(name, {}).get("rows", {}))
        ordered = []
        for index, row in enumerate(rows):
            value = ids[index] if index < len(ids) else None
            if value is None and index < len(old_ids) and _valid_id(old_ids[index], prefix):
                value = old_ids[index]
            if not _valid_id(value, prefix) or value in seen:
                if value is not None:
                    report.ambiguities.append(f"{name}: invalid or duplicate row ID {value!r}; allocated a new ID")
                highest += 1
                value = f"{prefix}_{highest:03d}"
            else:
                highest = max(highest, int(value.rsplit("_", 1)[1]))
            seen.add(value)
            ordered.append((value, row))
        allocated[name] = ordered
    return allocated, highest

def _same_stub_signature(left: _Stub, right: _Stub) -> bool:
    return (left.return_type, left.parameters) == (right.return_type, right.parameters)

def _provenance(analysis: Any, project: str, module: str, input_hash: str, report: MigrationReport) -> dict | None:
    generated = {}
    assembler = RowAssembler(Solver(), McdcGenerator(), include_boundary=False, include_fault=False)
    for function in getattr(analysis, "functions", ()):
        try:
            generated[function.name] = assembler.assemble_with_provenance(function)
        except Exception as exc:
            report.errors.append(f"{function.name}: cannot assemble provenance: {exc}")
    if report.errors:
        return None
    try:
        return build_candidate(analysis, generated, project, module, input_hash)
    except Exception as exc:
        report.errors.append(f"cannot build Analysis DB candidate: {exc}")
        return None

def _attach_provenance(state: dict, candidate: dict | None, report: MigrationReport) -> None:
    if candidate is None:
        return
    for name, function in state["functions"].items():
        source = candidate["functions"].get(name)
        if source is None:
            continue
        refs = [recipe["row_id"] for decision in source["decisions"].values() for recipe in decision["recipes"]]
        refs += [item["row_id"] for item in source["generated_rows"]]
        targets = list(function["rows"])
        if len(refs) > len(targets):
            report.errors.append(f"{name}: Analysis provenance has more logical rows than legacy rows")
            continue
        mapping = dict(zip(refs, targets))
        function["max_decision_number"] = source["max_decision_number"]
        function["decisions"] = deepcopy(source["decisions"])
        function["generated_rows"] = deepcopy(source["generated_rows"])
        for decision in function["decisions"].values():
            for recipe in decision["recipes"]:
                recipe["row_id"] = mapping[recipe["row_id"]]
        for generated in function["generated_rows"]:
            generated["row_id"] = mapping[generated["row_id"]]

def _state(project, module, source, metadata, rows_by_function, existing, report):
    prefix = Path(source).stem
    records = [(item[0].name, item[1], item[2]) for item in rows_by_function]
    allocated, maximum = _allocate(prefix, records, existing, report)
    catalog: dict[str, dict[str, Any]] = {}
    functions: dict[str, dict[str, Any]] = {}
    for legacy, _rows, _ids, signatures, bodies, variables in rows_by_function:
        local = {}
        for key, signature in signatures.items():
            storage = None if signature.return_type == "void" else (signature.storage or f"tessy_{key}")
            if storage and storage not in variables:
                variables[storage] = {"type": signature.return_type, "direction": "input", "dimensions": []}
                for _row_id, row in allocated.get(legacy.name, []):
                    row["input"].setdefault(storage, "")
            entry = {"stub_key": key, "status": "active", "function_name": key,
                     "return_type": signature.return_type, "return_storage": storage,
                     "parameters": [dict(item) for item in signature.parameters]}
            if key in catalog and catalog[key] != entry:
                report.ambiguities.append(f"stub signature differs for {key!r}")
            catalog.setdefault(key, entry)
            local[key] = {"owner": "user" if key in bodies else "tool", "status": "active",
                          "body": bodies.get(key, default_stub_body(
                              {"return_type": signature.return_type, "return_storage": storage}, key in signatures))}
        state_rows = {}
        generated = []
        for row_id, row in allocated.get(legacy.name, []):
            try:
                inputs = nest_values(row["input"], variables, channel="input")
                outputs = nest_values(row["output"], variables, channel="output")
            except Exception as exc:
                report.errors.append(f"{legacy.name} row {row_id}: invalid values: {exc}")
                continue
            state_rows[row_id] = {"owner": "user" if row["description"] else "tool", "status": "active",
                                  "description": row["description"], "test_method": "", "input": inputs, "output": outputs}
            generated.append({"row_id": row_id, "kind": "Nominal"})
        functions[legacy.name] = {"max_decision_number": 0, "variables": variables, "decisions": {},
                                  "generated_rows": generated, "rows": state_rows, "used_stub_keys": list(signatures), "stubs": local}
    result = {"schema_version": 1, "project": project, "module": module, "source_file": source,
              "module_input_hash": f"sha256:{getattr(metadata, 'source_sha256', 'unknown')}",
              "max_row_number": maximum, "stub_catalog": catalog, "functions": functions}
    try:
        validate_module(result)
    except Exception as exc:
        report.errors.append(f"candidate state validation failed: {exc}")
    return result

def _semantic(state: Mapping[str, Any], function: str) -> list[tuple[str, str, dict, dict]]:
    return [(key, row["description"], row["input"], row["output"])
            for key, row in _ordered_active_rows(state["functions"][function])]

def _project_and_verify(state: dict[str, Any], output_dir: Path, template: Any, report: MigrationReport) -> tuple[dict[str, Path], Path]:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".migration-", dir=output_dir.parent))
    staged = staging / "workbooks"
    staged.mkdir()
    paths: dict[str, Path] = {}
    adapter = TessyWorkbookAdapter()
    try:
        for function in state["functions"]:
            path = staged / f"{function}.xlsx"
            adapter.write_function(path, state, function, template_path=template)
            paths[function] = path
        for function, path in paths.items():
            snapshot = adapter.read_complete_export(path, expected_module=Path(state["source_file"]).name,
                                                    expected_testobject=function, expected_project=f"tessy_{state['project']}")
            expected = [(key, description,
                         flatten_values(inputs, state["functions"][function]["variables"], channel="input"),
                         flatten_values(outputs, state["functions"][function]["variables"], channel="output"))
                        for key, description, inputs, outputs in _semantic(state, function)]
            actual = [(case.external_test_case_id, case.state_description, case.input, case.output) for case in snapshot.cases]
            if actual != expected:
                raise ValueError(f"independent workbook verification failed for {function}")
            expected_stubs = [(key, state["functions"][function]["stubs"][key]["body"])
                              for key in state["functions"][function]["stubs"]]
            if [(stub.name, stub.body_raw) for stub in snapshot.stubs] != expected_stubs:
                raise ValueError(f"independent stub verification failed for {function}")
    except Exception as exc:
        report.errors.append(str(exc))
    return paths, staging

def _publish_workbooks(staged: Mapping[str, Path], output_dir: Path, report: MigrationReport) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    backups = [(output_dir / source.name,
                (output_dir / source.name).read_bytes() if (output_dir / source.name).exists() else None)
               for source in staged.values()]
    published: list[tuple[Path, bytes | None]] = []
    try:
        for source, (target, previous) in zip(staged.values(), backups):
            os.replace(source, target)
            published.append((target, previous))
    except OSError as exc:
        target, previous = backups[len(published)]
        report.errors.append(f"cannot publish workbook {target}: {exc}")
        for target, previous in [(target, previous), *reversed(published)]:
            try:
                _rollback(target, previous)
            except Exception as rollback_exc:
                report.errors.append(f"workbook rollback failed for {target}: {rollback_exc}")
        raise
    return [target for target, _previous in backups]

def _verify_stub_signatures(state: Mapping[str, Any], analysis: Any) -> None:
    expected = build_stub_catalog(analysis)
    actual = state["stub_catalog"]
    if set(actual) != set(expected):
        raise ValueError("re-read Analysis DB stub catalog differs from migrated JSON")
    for key, entry in expected.items():
        current = actual[key]
        if (current["return_type"], current["return_storage"], current["parameters"]) != (
            entry["return_type"], entry["return_storage"], entry["parameters"]
        ):
            raise ValueError(f"re-read Analysis DB stub signature differs for {key!r}")

def _rollback(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".restore", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != previous:
            raise OSError("restore bytes do not match backup")
        os.replace(temporary, path)
    except Exception as exc:
        raise OSError(f"cannot restore {path}; recovery sibling: {temporary}") from exc

def migrate_module(project_config: Mapping[str, Any], state_root: str | os.PathLike[str], dry_run: bool = True) -> MigrationReport:
    """Migrate one module; dry-run performs no writes."""
    report = MigrationReport()
    try:
        config = _direct_config(project_config)
        project, module = _identity(config)
        root = Path(state_root).resolve()
        working, database, excel_in, excel_out = _paths(config, root, module)
        metadata, analysis = _db(database, report)
        if metadata is None:
            return report
        source = _source_file(config, module, metadata)
        paths = module_paths(root, project, module)
        existing = None
        if paths.state.exists():
            try:
                existing = load_module(paths.state)
            except Exception as exc:
                report.errors.append(f"existing current JSON is invalid: {paths.state}: {exc}")
        legacy = _read_values(list(working.glob("value_*.xml")), report)
        analysis_functions = {item.name: item for item in getattr(analysis, "functions", ())}
        analysis_catalog = build_stub_catalog(analysis)
        for name in analysis_functions:
            legacy.setdefault(name, _LegacyFunction(name))
        if not legacy:
            report.errors.append(f"no legacy Value XML functions found under {working}")
            return report
        rows_by_function = []
        for name, function in legacy.items():
            snapshot = _workbook(excel_in, project, Path(source).name, name, report)
            vars = _variables(function, analysis_functions.get(name), snapshot.columns if snapshot else [], report)
            signatures = _analysis_signatures(analysis_functions.get(name), analysis_catalog)
            bodies, xml_signatures = {}, {}
            for stub_path in sorted(working.glob("stub_*.xml"), key=lambda item: item.name.lower()):
                if name.lower() not in stub_path.stem.lower():
                    continue
                try:
                    parsed_bodies, parsed_signatures = _stub_xml(stub_path, name)
                except Exception as exc:
                    report.errors.append(f"{stub_path}: {exc}")
                    continue
                for key, body in parsed_bodies.items():
                    if key in bodies and bodies[key] != body:
                        report.ambiguities.append(f"{name}: stub body differs for {key!r}")
                    bodies[key] = body
                for key, signature in parsed_signatures.items():
                    if key in signatures and not _same_stub_signature(signatures[key], signature):
                        report.ambiguities.append(f"{name}: stub signature differs for {key!r}")
                    if key in xml_signatures and not _same_stub_signature(xml_signatures[key], signature):
                        report.ambiguities.append(f"{name}: stub XML signatures disagree for {key!r}")
                    if key in signatures and not signatures[key].storage and signature.storage:
                        signatures[key] = signature
                    xml_signatures[key] = signature
            signatures.update(xml_signatures)
            for signature in signatures.values():
                storage = signature.storage or f"tessy_{signature.key}"
                if signature.return_type != "void" and storage not in vars:
                    vars[storage] = {"type": signature.return_type, "direction": "input", "dimensions": []}
            rows = _baseline(function, vars)
            rows, ids, workbook_bodies = _combine(rows, snapshot, vars, report)
            for key in workbook_bodies:
                if key not in signatures:
                    report.ambiguities.append(f"{name}: Excel stub {key!r} is not in Analysis DB/XML")
            bodies.update(workbook_bodies)
            legacy_ids = function.row_ids[:len(rows)] + [None] * max(0, len(rows) - len(function.row_ids))
            ids = [value or legacy_ids[index] for index, value in enumerate(ids)] if snapshot is not None else legacy_ids
            rows_by_function.append((function, rows, ids, signatures, bodies, vars))
        report.functions, report.rows = len(rows_by_function), sum(len(item[1]) for item in rows_by_function)
        report.stubs = sum(len(item[3]) for item in rows_by_function)
        candidate = _provenance(analysis, project, module, f"sha256:{metadata.source_sha256}", report)
        state = _state(project, module, source, metadata, rows_by_function, existing, report)
        _attach_provenance(state, candidate, report)
        try:
            validate_module(state)
        except Exception as exc:
            report.errors.append(f"provenance state validation failed: {exc}")
        if report.errors or report.ambiguities or dry_run:
            return report
        if database.resolve() == paths.database.resolve():
            report.errors.append("legacy Analysis DB and current DB path are identical")
            return report
        staged, staging = _project_and_verify(state, excel_out, config.get("template") or config.get("template_path"), report)
        if report.errors:
            shutil.rmtree(staging, ignore_errors=True)
            return report
        previous_db = paths.database.read_bytes() if paths.database.exists() else None
        previous_json = paths.state.read_bytes() if paths.state.exists() else None
        try:
            AnalysisDbWriter().write_atomic(database, paths.database)
            write_module(paths.state, state)
            if load_module(paths.state) != state:
                raise ValueError("re-read current JSON differs from migrated candidate")
            _metadata_after_write, written_analysis = AnalysisDbAdapter().read_module(paths.database, require_metadata=True)
            _verify_stub_signatures(state, written_analysis)
            published_workbooks = _publish_workbooks(staged, excel_out, report)
            report.writes.extend([str(item) for item in published_workbooks])
            report.writes.extend([str(paths.database), str(paths.state)])
        except Exception as exc:
            report.errors.append(f"migration publication failed: {exc}")
            try:
                _rollback(paths.database, previous_db)
                _rollback(paths.state, previous_json)
            except Exception as rollback_exc:
                report.errors.append(f"migration rollback failed: {rollback_exc}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return report
    except Exception as exc:
        report.errors.append(str(exc))
        return report


def _load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or yaml is None:
        return json.loads(text)
    return yaml.safe_load(text)


def _configs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValueError("config root must be a mapping")
    projects = value.get("projects")
    if isinstance(projects, list):
        result = []
        for project in projects:
            if not isinstance(project, Mapping):
                continue
            modules = project.get("modules", [])
            for module in modules.values() if isinstance(modules, Mapping) else modules:
                item = dict(module) if isinstance(module, Mapping) else {"module": module}
                item.setdefault("project", project.get("name") or project.get("project"))
                for key in ("state_root", "config_dir", "excel_output_dir"):
                    if key in project and key not in item:
                        item[key] = project[key]
                result.append(item)
        return result
    modules = value.get("modules")
    if isinstance(modules, Mapping):
        return [_direct_config({**value, "modules": {name: item}}) for name, item in modules.items()]
    if isinstance(modules, (list, tuple)):
        return [_direct_config({**value, "modules": [item]}) for item in modules]
    return [_direct_config(value)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy TESSY exports")
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    raw = _load(args.config)
    root = raw.get("state_root") if isinstance(raw, Mapping) else None
    if not root and isinstance(raw, Mapping):
        iteration = raw.get("iteration")
        root = iteration.get("state_root") if isinstance(iteration, Mapping) else None
    root = _resolve(root or ".utgen", args.config.parent)
    combined = MigrationReport()
    for item in _configs(raw):
        item.setdefault("config_dir", str(args.config.parent))
        combined.add(migrate_module(item, root, dry_run=not args.apply))
    print(json.dumps(combined.as_dict(), ensure_ascii=False, indent=2))
    return 1 if combined.errors or combined.ambiguities else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MigrationReport", "main", "migrate_module"]
