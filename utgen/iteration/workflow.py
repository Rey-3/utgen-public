"""Configuration, workbook discovery, and manual user-merge orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path

import yaml

from .adapters.tessy_workbook import TessyWorkbookAdapter
from .merge import merge_user
from .state import load_module, module_paths, write_module


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


def scan_workbooks(project_config) -> dict[tuple[str, str, str], Path]:
    """Find XLSX files and key them by General-sheet identity."""

    config, base = _config_and_base(project_config)
    result = {}
    seen = {}
    for key, path, snapshot in _scan_entries(config, base):
        if key in seen and snapshot_semantics(snapshot) != seen[key][0]:
            raise ValueError(f"conflicting workbooks for {key!r}")
        if key not in result:
            result[key] = path
            seen[key] = (snapshot_semantics(snapshot), path)
    return result


def run_user_merge(config) -> list[ModuleSummary]:
    """Read, validate, merge, and publish each configured module independently."""

    config, base = _config_and_base(config)
    state_root = _state_root(config, base)
    template = _template_path(config, base)
    summaries = []
    for project, module in _configured_modules(config):
        try:
            entries = _scan_entries(
                {"projects": [{"name": project, "modules": [module]}]}, base
            )
            target = (_normal(project), _normal(_module_name(module)))
            grouped = {}
            for key, path, snapshot in entries:
                if key[:2] == target:
                    grouped.setdefault(key[2], []).append((path, snapshot))
            if not grouped:
                summaries.append(_summary(project, _module_name(module), "skipped", {}))
                continue

            paths = module_paths(state_root, project, _module_name(module))
            current = load_module(paths.state)
            candidate = current
            changed_paths = {}
            function_summaries = {}
            allocated = []
            for function_name, items in grouped.items():
                if len(items) > 1:
                    first = snapshot_semantics(items[0][1])
                    if any(snapshot_semantics(item[1]) != first for item in items[1:]):
                        raise ValueError(
                            f"conflicting workbooks for {project}/{_module_name(module)}/{function_name}"
                        )
                path, snapshot = items[0]
                merged = merge_user(candidate, function_name, snapshot)
                candidate = merged.state
                function_summaries[function_name] = merged.summary
                allocated.extend(merged.allocated_ids)
                if merged.changed:
                    changed_paths[function_name] = path
            if not changed_paths:
                summaries.append(
                    _summary(
                        project,
                        _module_name(module),
                        "skipped",
                        {"functions": function_summaries, "allocated_ids": ()},
                    )
                )
                continue

            write_module(paths.state, candidate)
            adapter = TessyWorkbookAdapter()
            for function_name, path in changed_paths.items():
                adapter.write_function(path, candidate, function_name, template)
            summaries.append(
                _summary(
                    project,
                    _module_name(module),
                    "updated",
                    {"functions": function_summaries, "allocated_ids": tuple(allocated)},
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


def _scan_entries(config, base):
    entries = []
    for _project, module in _configured_modules(config):
        directory = module.get("excel_input_dir")
        if not directory:
            continue
        directory = Path(os.fspath(directory))
        if not directory.is_absolute():
            directory = base / directory
        if not directory.is_dir():
            continue
        adapter = TessyWorkbookAdapter()
        for path in sorted(directory.glob("*.xlsx")):
            if path.name.startswith("~$") or not path.is_file():
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


def snapshot_semantics(snapshot) -> tuple:
    """Return workbook content that can conflict; ignore layout metadata."""
    cases = []
    for case in _items(snapshot, "cases"):
        values = []
        for channel in ("input", "output"):
            cells = _value(case, f"{channel}_cells", _value(case, channel, {})) or {}
            values.append(tuple(sorted((_cell_semantics(key, cell) for key, cell in cells.items()), key=repr)))
        cases.append(
            (
                _value(case, "external_test_case_id", _value(case, "row_id")),
                _value(case, "state_description", _value(case, "description", "")),
                *values,
            )
        )
    stubs = tuple(sorted(
        (
            (
                _value(item, "name", _value(item, "stub_name")),
                _value(item, "body_raw", _value(item, "body")),
            )
            for item in _items(snapshot, "stubs")
        ),
        key=repr,
    ))
    return tuple(sorted(cases, key=repr)), stubs


def _value(item, name, default=None):
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _items(item, name):
    return _value(item, name, ()) or ()


def _cell_semantics(key, cell) -> tuple:
    name = key if isinstance(key, str) else _value(key, "canonical_name")
    return name, _value(cell, "kind", "value"), _value(cell, "raw_text", cell)


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


def _template_path(config, base):
    value = (config.get("Tessy") or {}).get("template_path")
    if not value:
        return None
    path = Path(os.fspath(value))
    return path if path.is_absolute() else base / path


__all__ = ["ModuleSummary", "run_user_merge", "scan_workbooks"]
