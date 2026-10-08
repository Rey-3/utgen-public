"""Configuration, workbook discovery, and manual user-merge orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

import yaml

from utgen import __version__
from .adapters.analysis_db import AnalysisDbAdapter, AnalysisDbWriter
from .adapters.tessy_workbook import TessyWorkbookAdapter
from .merge import build_candidate, merge_tool, merge_user
from .state import load_module, module_paths, ordered_active_rows, write_module


@dataclass(frozen=True)
class ModuleSummary:
    project: str
    module: str
    status: str
    summary: dict
    error: str | None = None

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def updated(self) -> bool:
        return self.status == "updated"


def run_generation(config, generator) -> list[ModuleSummary]:
    """Run first generation or a complete next-tool merge per module."""

    config, base = _config_and_base(config)
    state_root = _state_root(config, base)
    include_dirs = _include_dirs(config, base)
    summaries = []
    for project, module in _configured_modules(config):
        name = _module_name(module)
        module_started = time.monotonic()
        print(f"\n{project}/{name}: Starting module...", flush=True)
        try:
            summaries.append(
                _run_generation_module(
                    config,
                    base,
                    state_root,
                    include_dirs,
                    project,
                    module,
                    generator,
                )
            )
        except Exception as exc:
            summaries.append(_summary(project, name, "error", {"error": str(exc)}, str(exc)))
        finally:
            print(f"{project}/{name}: Module runtime: {time.monotonic() - module_started:.3f}s", flush=True)
    return summaries


def run_excel_export(config) -> list[ModuleSummary]:
    """Rebuild each configured function workbook from current JSON only."""

    config, base = _config_and_base(config)
    state_root = _state_root(config, base)
    summaries = []
    for project, module in _configured_modules(config):
        name = _module_name(module)
        try:
            paths = module_paths(state_root, project, name)
            state = load_module(paths.state)
            if paths.database.exists() and not _database_matches(
                AnalysisDbAdapter().inspect(paths.database), module, base
            ):
                raise ValueError("saved analysis does not match current source; run mode 1 before exporting Excel")
            written = _write_active_workbooks(state, module, base)
            summaries.append(
                _summary(
                    project,
                    name,
                    "updated" if written else "skipped",
                    {
                        "json": "skipped",
                        "excel_written": written,
                        "rows": _state_row_summary(state),
                        "stubs": _state_stub_summary(state),
                    },
                )
            )
        except Exception as exc:
            summaries.append(_summary(project, name, "error", {"error": str(exc)}, str(exc)))
    return summaries


def _run_generation_module(
    config,
    base: Path,
    state_root: Path,
    include_dirs,
    project: str,
    module: Mapping,
    generator,
) -> ModuleSummary:
    name = _module_name(module)
    paths = module_paths(state_root, project, name)
    load_started = time.monotonic()
    print(f"{name}: Loading saved JSON/XML...", flush=True)
    current = None
    if paths.state.exists():
        current = load_module(paths.state)

    database_analysis = None
    database_metadata = None
    if paths.database.exists():
        try:
            database_metadata, database_analysis = AnalysisDbAdapter().read_module(paths.database)
        except Exception:
            database_analysis = None
    print(f"{name}: Saved state load time: {time.monotonic() - load_started:.3f}s", flush=True)

    cache_started = time.monotonic()
    if current is not None and database_analysis is not None and _database_matches(
        database_analysis, module, base
    ):
        try:
            input_hash = _module_input_hash(
                config,
                project,
                module,
                base,
                database_analysis,
                database_identity=database_metadata.analysis_id,
            )
        except Exception:
            input_hash = None
        if input_hash == current.get("module_input_hash"):
            workbooks_valid = _workbooks_valid(current, module, base)
            print(f"{name}: Cache check time: {time.monotonic() - cache_started:.3f}s", flush=True)
            if workbooks_valid:
                return _summary(
                    project,
                    name,
                    "skipped",
                    {
                        "json": "skipped",
                        "excel_written": 0,
                        "rows": _state_row_summary(current),
                        "stubs": _state_stub_summary(current),
                    },
                )
            written = _write_active_workbooks(current, module, base)
            return _summary(
                project,
                name,
                "skipped",
                {
                    "json": "skipped",
                    "excel_written": written,
                    "rows": _state_row_summary(current),
                    "stubs": _state_stub_summary(current),
                },
            )

    print(f"{name}: Cache check time: {time.monotonic() - cache_started:.3f}s", flush=True)
    generation_project, generation_module = _generation_config(
        config, base, project, module
    )
    generation = generator(
        generation_project, generation_module, include_dirs
    )
    if len(generation) == 2:
        analysis, generated = generation
        raw_database = None
    elif len(generation) == 3:
        analysis, generated, raw_database = generation
    else:
        raise ValueError("generator must return (analysis, generated[, raw_database])")
    prepare_started = time.monotonic()
    print(f"{name}: Preparing/merging JSON state...", flush=True)
    candidate = build_candidate(analysis, generated, project, name, "pending")
    if current is None:
        state = candidate
        summary = {
            "rows": _state_row_summary(state),
            "stubs": _state_stub_summary(state),
        }
    else:
        merged = merge_tool(current, candidate)
        state = merged.state
        summary = {
            "rows": merged.summary["rows"],
            "stubs": merged.summary["stubs"],
            "decisions": merged.summary["decisions"],
        }
    print(f"{name}: JSON preparation time: {time.monotonic() - prepare_started:.3f}s", flush=True)
    xml_started = time.monotonic()
    database_metadata = _write_current_database(
        analysis, paths.database, raw_database=raw_database
    )
    print(f"{name}: Database XML write time: {time.monotonic() - xml_started:.3f}s", flush=True)
    hash_started = time.monotonic()
    state["module_input_hash"] = _module_input_hash(
        config,
        project,
        module,
        base,
        analysis,
        database_identity=database_metadata.analysis_id,
    )
    print(f"{name}: Input hash time: {time.monotonic() - hash_started:.3f}s", flush=True)
    json_started = time.monotonic()
    write_module(paths.state, state)
    print(f"{name}: JSON write time: {time.monotonic() - json_started:.3f}s", flush=True)
    written = _write_active_workbooks(state, module, base)
    summary.update(json="updated", excel_written=written)
    return _summary(project, name, "updated", summary)


def _generation_config(config, base: Path, project_name: str, module: Mapping):
    """Resolve source/root paths once at the generator boundary."""

    project = next(
        item
        for item in config.get("projects", ())
        if isinstance(item, Mapping)
        and item.get("name", item.get("project_name")) == project_name
    )
    result_project = dict(project)
    result_project["description"] = config.get("settings", {}).get("description")
    root = Path(os.fspath(project.get("root_path", base)))
    if not root.is_absolute():
        root = base / root
    result_project["root_path"] = str(root)
    result_module = dict(module)
    source = Path(os.fspath(module["source_file"]))
    if not source.is_absolute():
        source = base / source
    result_module["source_file"] = str(source)
    return result_project, result_module


def _database_matches(analysis, module: Mapping, base: Path) -> bool:
    expected = Path(os.fspath(module["source_file"]))
    if not expected.is_absolute():
        expected = base / expected
    actual = Path(os.fspath(getattr(analysis, "file", getattr(analysis, "source_file", ""))))
    try:
        if actual.resolve() != expected.resolve():
            return False
        return hashlib.sha256(expected.read_bytes()).hexdigest() == getattr(
            analysis, "sha256", getattr(analysis, "source_sha256", None)
        )
    except OSError:
        return False


def _module_input_hash(
    config,
    project,
    module,
    base: Path,
    analysis,
    *,
    database_identity=None,
) -> str:
    source = Path(os.fspath(module["source_file"]))
    if not source.is_absolute():
        source = base / source
    analyzed_source = getattr(analysis, "file", None)
    if analyzed_source:
        candidate_source = Path(os.fspath(analyzed_source))
        if not candidate_source.is_absolute():
            candidate_source = base / candidate_source
        if candidate_source.is_file():
            source = candidate_source
    files = [("source", source)]
    for item in getattr(analysis, "headers", ()) or ():
        try:
            path = item[1]
        except (IndexError, TypeError):
            path = getattr(item, "path", None)
        if path is None:
            continue
        header = Path(os.fspath(path))
        if not header.is_absolute():
            header = base / header
        files.append(("header", header))
    file_payload = []
    for kind, path in files:
        data = path.read_bytes()
        file_payload.append(
            {"kind": kind, "path": path.resolve().as_posix(), "sha256": hashlib.sha256(data).hexdigest()}
        )
    module_config = {
        key: value
        for key, value in module.items()
        if key not in {"excel_input_dir", "excel_output_dir"}
    }
    payload = {
        "schema_version": 1,
        "project": project,
        "module": module_config,
        "generator_config": {
            "version": __version__,
            "include_dirs": list(_include_dirs(config, base)),
            "description": config.get("settings", {}).get("description"),
        },
        "files": file_payload,
    }
    if database_identity is not None:
        payload["database_identity"] = str(database_identity)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _write_current_database(analysis, path: Path, raw_database=None):
    if raw_database is not None:
        return AnalysisDbWriter().write_atomic(raw_database, Path(path))

    from utgen.db import XmlDbWriter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".raw.xml", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        XmlDbWriter().write(analysis, temporary)
        return AnalysisDbWriter().write_atomic(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _excel_directory(module: Mapping, base: Path) -> Path:
    value = module.get("excel_output_dir", module.get("excel_input_dir"))
    directory = Path(os.fspath(value)) if value else base / "excel"
    return directory if directory.is_absolute() else base / directory


def _workbook_paths(state: Mapping, module: Mapping, base: Path) -> dict[str, Path]:
    directory = _excel_directory(module, base)
    return {name: directory / f"{name}.xlsx" for name, function in state["functions"].items()
            if ordered_active_rows(function)}


def _workbooks_valid(state: Mapping, module: Mapping, base: Path) -> bool:
    adapter = TessyWorkbookAdapter()
    for function_name, path in _workbook_paths(state, module, base).items():
        if not path.is_file():
            path = path.with_suffix(".xls")
        if not path.is_file():
            return False
        try:
            adapter.read_complete_export(
                path,
                expected_module=Path(state["source_file"]).name,
                expected_testobject=function_name,
                expected_project=f"tessy_{state['project']}",
            )
        except Exception:
            return False
    return True


def _write_active_workbooks(state: Mapping, module: Mapping, base: Path) -> int:
    adapter = TessyWorkbookAdapter()
    paths = _workbook_paths(state, module, base)
    started = time.monotonic()
    print(f"{state['module']}: Exporting {len(paths)} Excel files...", flush=True)
    adapter.write_functions(paths.items(), state)
    for name in state["functions"].keys() - paths.keys():
        for suffix in (".xlsx", ".xls"):
            obsolete = _excel_directory(module, base) / f"{name}{suffix}"
            if obsolete.is_file():
                obsolete.rename(obsolete.with_name(f"{obsolete.name}.{time.time_ns()}.orphaned"))
    print(f"{state['module']}: Excel export time: {time.monotonic() - started:.3f}s", flush=True)
    return len(paths)


def _state_row_summary(state: Mapping) -> dict:
    rows = [row for function in state["functions"].values() for row in function["rows"].values()]
    return {
        "generated": sum(row["owner"] == "tool" and row["status"] == "active" for row in rows),
        "kept": sum(row["owner"] == "user" and row["status"] == "active" for row in rows),
        "allocated": 0,
        "orphaned": sum(row["status"] == "orphaned" for row in rows),
    }


def _state_stub_summary(state: Mapping) -> dict:
    stubs = state.get("stub_catalog", {}).values()
    return {
        "active": sum(stub["status"] == "active" for stub in stubs),
        "orphaned": sum(stub["status"] == "orphaned" for stub in stubs),
    }


def _merge_user_row_summary(function_summaries: Mapping) -> dict:
    return {
        key: sum(
            summary.get("rows", {}).get(key, 0)
            for summary in function_summaries.values()
        )
        for key in ("changed", "added", "orphaned")
    }


def _merge_user_stub_summary(function_summaries: Mapping) -> dict:
    return {
        key: sum(
            summary.get("stubs", {}).get(key, 0)
            for summary in function_summaries.values()
        )
        for key in ("changed", "orphaned")
    }


def scan_workbooks(project_config) -> dict[tuple[str, str, str], Path]:
    """Find XLSX files and key them by General-sheet identity."""

    config, base = _config_and_base(project_config)
    result = {}
    for key, path, _snapshot in _scan_entries(config, base):
        if key in result:
            raise ValueError(f"duplicate workbooks for {key!r}")
        result[key] = path
    return result


def run_user_merge(config) -> list[ModuleSummary]:
    """Read, validate, merge, and publish each configured module independently."""

    config, base = _config_and_base(config)
    state_root = _state_root(config, base)
    summaries = []
    for project, module in _configured_modules(config):
        try:
            entries = _scan_entries(
                {"projects": [{"name": project, "modules": [module]}]}, base
            )
            target = (_normal(project), _module_identities(module))
            grouped = {}
            for key, path, snapshot in entries:
                if key[0] == target[0] and key[1] in target[1]:
                    grouped.setdefault(key[2], []).append((path, snapshot))
            if not grouped:
                summaries.append(
                    _summary(
                        project,
                        _module_name(module),
                        "skipped",
                        {"json": "skipped", "excel_written": 0},
                    )
                )
                continue

            paths = module_paths(state_root, project, _module_name(module))
            current = load_module(paths.state)
            candidate = current
            changed_paths = {}
            rewrite_paths = {}
            function_summaries = {}
            allocated = []
            for function_name, items in grouped.items():
                if len(items) > 1:
                    raise ValueError(f"duplicate workbooks for {project}/{_module_name(module)}/{function_name}")
                path, snapshot = items[0]
                merged = merge_user(candidate, function_name, snapshot)
                candidate = merged.state
                function_summaries[function_name] = merged.summary
                allocated.extend(merged.allocated_ids)
                if merged.changed:
                    changed_paths[function_name] = tuple(item[0] for item in items)
                if merged.rewrite_required:
                    rewrite_paths[function_name] = tuple(item[0] for item in items)
            if not changed_paths and not rewrite_paths:
                summaries.append(
                    _summary(
                        project,
                        _module_name(module),
                        "skipped",
                        {
                            "functions": function_summaries,
                            "allocated_ids": (),
                            "rows": _merge_user_row_summary(function_summaries),
                            "stubs": _merge_user_stub_summary(function_summaries),
                            "json": "skipped",
                            "excel_written": 0,
                        },
                    )
                )
                continue

            if changed_paths:
                write_module(paths.state, candidate)
            adapter = TessyWorkbookAdapter()
            workbook_paths = [
                (function_name, path)
                for function_name in grouped
                for path in (rewrite_paths.get(function_name) or changed_paths.get(function_name) or ())
            ]
            adapter.write_functions(workbook_paths, candidate)
            written_count = len(workbook_paths)
            status = "updated" if changed_paths else "skipped"
            summaries.append(
                _summary(
                    project,
                    _module_name(module),
                    status,
                    {
                        "functions": function_summaries,
                        "allocated_ids": tuple(allocated),
                        "rows": _merge_user_row_summary(function_summaries),
                        "stubs": _merge_user_stub_summary(function_summaries),
                        "json": "updated" if changed_paths else "skipped",
                        "excel_written": written_count,
                    },
                )
            )
        except Exception as exc:
            summaries.append(
                _summary(
                    project,
                    _module_name(module),
                    "error",
                    {"status": "error", "error": str(exc)},
                    str(exc),
                )
            )
    return summaries


def _summary(project, module, status, summary, error=None) -> ModuleSummary:
    return ModuleSummary(project, module, status, {"status": status, **summary}, error)


def _config_and_base(value):
    if isinstance(value, (str, os.PathLike)):
        path = Path(value)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return _require_config(data), path.parent
    return _require_config(value), Path.cwd()


def _require_config(value):
    if not isinstance(value, Mapping):
        raise ValueError("configuration must be a mapping")
    if "projects" in value:
        return value
    if "modules" in value:
        return {"projects": [value]}
    if "module_name" in value:
        return {"projects": [{"name": value.get("project", ""), "modules": [value]}]}
    raise ValueError("configuration must contain projects")


def _configured_modules(config):
    for project in config.get("projects", ()):
        if not isinstance(project, Mapping) or project.get("enabled", True) is False:
            continue
        project_name = project.get("name", project.get("project_name"))
        if not isinstance(project_name, str) or not project_name:
            raise ValueError("project name must be a non-empty string")
        for module in project.get("modules", ()):
            if not isinstance(module, Mapping) or module.get("enabled", True) is False:
                continue
            yield project_name, module


def _module_name(module: Mapping) -> str:
    name = module.get("module_name", module.get("name"))
    if not isinstance(name, str) or not name:
        raise ValueError("module name must be a non-empty string")
    return name


def _module_identities(module: Mapping) -> set[str]:
    identities = {_normal(_module_name(module))}
    source = module.get("source_file")
    if source:
        identities.add(_normal(Path(os.fspath(source).replace("\\", "/")).stem))
    return identities


def _scan_entries(config, base):
    entries, scanned = [], set()
    for _project, module in _configured_modules(config):
        directory = module.get("excel_input_dir")
        if not directory:
            continue
        directory = Path(os.fspath(directory))
        if not directory.is_absolute():
            directory = base / directory
        identity = directory.resolve()
        if identity in scanned or not directory.is_dir():
            continue
        scanned.add(identity)
        adapter = TessyWorkbookAdapter()
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in {".xlsx", ".xls"} or path.name.startswith("~$") or not path.is_file():
                continue
            snapshot = adapter.read_complete_export(path)
            entries.append((_snapshot_identity(snapshot), path, snapshot))
    return entries


def _snapshot_identity(snapshot):
    meta = snapshot.meta
    project = _meta(meta, "project", "project_name")
    module = _meta(meta, "module", "module_name")
    function = _meta(meta, "testobject", "test_object", "test_object_name")
    if not all(isinstance(value, str) and value for value in (project, module, function)):
        raise ValueError("workbook General identity is incomplete")
    module = Path(module.replace("\\", "/")).name
    if module.lower().endswith(".c"):
        module = module[:-2]
    return _normal(project), _normal(module), function


def _meta(meta, *names):
    if isinstance(meta, Mapping):
        return next((meta[name] for name in names if name in meta), None)
    return next((getattr(meta, name, None) for name in names if hasattr(meta, name)), None)


def _normal(value: str) -> str:
    return value[6:] if value.startswith("tessy_") else value


def _state_root(config, base):
    value = (config.get("iteration") or {}).get("state_root", ".utgen")
    path = Path(os.fspath(value))
    return path if path.is_absolute() else base / path


def _include_dirs(config, base):
    value = (config.get("Tessy") or {}).get("include", ())
    if isinstance(value, str):
        value = (value,)
    result = []
    for item in value or ():
        path = Path(os.fspath(item))
        result.append(str(path if path.is_absolute() else base / path))
    return tuple(result)


__all__ = [
    "ModuleSummary",
    "run_excel_export",
    "run_generation",
    "run_user_merge",
    "scan_workbooks",
]
