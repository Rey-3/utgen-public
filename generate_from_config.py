"""Generate UT inputs for every module listed in config.yaml."""
from dataclasses import dataclass, replace
import multiprocessing as mp
from pathlib import Path
from pathlib import PurePosixPath
import sys
from time import perf_counter
from typing import Mapping

import yaml

from utgen.assembler import RowAssembler
from utgen.db import ModuleIndexer, XmlDbReader, XmlDbWriter
from utgen.emit import StubXmlWriter, ValueXmlWriter
from utgen.interp import Interpreter, Verifier
from utgen.mcdc import McdcGenerator
from utgen.model import Direction, Inventory, StubInfo, TestRow
from utgen.solver import Solver
from utgen.iteration import (
    ArtifactRecord,
    BaseSnapshot,
    CandidateBundle,
    FileSystemIterationRepository,
    FunctionIterationState,
    MergeAService,
    MergeBService,
    ModuleManifest,
    RepositoryNotInitialized,
    RowMeta,
    StubXmlAdapter,
    ValueXmlAdapter,
    WorkingSnapshot,
    allocate_case_name,
    allocate_row_id,
    initial_display_version,
    new_persistent_id,
    read_complete_export,
)
from tessy_parse import mainBody


CONFIG_PATH = (Path(sys.executable).with_name("config.yaml")
               if getattr(sys, "frozen", False) else Path(__file__).with_name("config.yaml"))
TIME_LOG_PATH = CONFIG_PATH.with_name("generation_times.txt")
FUNCTION_TIMEOUT_SECONDS = 180
XML_INPUT_PATH = CONFIG_PATH.with_name("input")
_LAST_CONFIG = None


@dataclass(frozen=True)
class _GeneratedFunction:
    rows: tuple[TestRow, ...]
    value_path: Path
    stub_path: Path


def projects_from_config():
    global _LAST_CONFIG
    config = _read_config()
    _LAST_CONFIG = config
    tessy_include = config.get("Tessy", {}).get("include", ())
    if isinstance(tessy_include, str):
        tessy_include = (tessy_include,)
    return config["projects"], tessy_include


def _read_config():
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config.yaml must contain a mapping")
    return config


def iteration_settings_from_config(config=None):
    config = _read_config() if config is None else config
    settings = config.get("iteration") or {}
    if not isinstance(settings, Mapping):
        raise ValueError("iteration settings must be a mapping")
    mode = settings.get("mode", "off")
    if mode not in {"off", "observe", "enforce"}:
        raise ValueError("iteration.mode must be off, observe, or enforce")
    state_root = Path(settings.get("state_root", ".utgen"))
    if not state_root.is_absolute():
        state_root = CONFIG_PATH.parent / state_root
    return mode, state_root, str(settings.get("tessy_dialect", "5.1"))


def elapsed(start):
    return f"{perf_counter() - start:.2f}s"


def report_time(message):
    print(message, flush=True)
    with TIME_LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(message + "\n")


def fallback_rows(function):
    first, second = {}, {}
    for leaf in function.leaves:
        if leaf.direction in (Direction.IN, Direction.INOUT):
            first[leaf.name] = 0
            second[leaf.name] = 1
    return [TestRow(first, "Timeout", "generation timeout"),
            TestRow(second, "Timeout", "generation timeout")]


def write_timeout_outputs(function, name, output, *, include_rows=False):
    rows = fallback_rows(function)
    note = f"generation timed out after {FUNCTION_TIMEOUT_SECONDS}s; fallback values used"
    Verifier(Interpreter(Inventory(function.leaves, function.stubs),
                         execute_static_helpers=True)).check(function, rows)
    ValueXmlWriter().write(function, rows, output / f"value_{name}_{function.name}.xml",
                           default_range="0,1")
    StubXmlWriter().write(function, output / f"stub_{name}_{function.name}.xml")
    return (len(rows), tuple(rows)) if include_rows else len(rows)


_WORKER_DATABASE = None


def initialize_worker(database):
    """Retain only the on-disk database path when the worker starts."""
    global _WORKER_DATABASE
    _WORKER_DATABASE = Path(database)


def worker_ready():
    """Small handshake used to wait until the worker initializer completes."""
    return _WORKER_DATABASE is not None


def generate_function(function_name, name, output, include_rows=False):
    """Generate one function by reloading its module database from disk."""
    try:
        if _WORKER_DATABASE is None:
            raise RuntimeError("worker database is not initialized")

        function = XmlDbReader().read(_WORKER_DATABASE).function(function_name)
        if function is None:
            raise ValueError(f"Function {function_name!r} was not found")

        output = Path(output)
        assembler = RowAssembler(Solver(), McdcGenerator(), include_boundary=False,
                                 include_fault=False)
        rows = assembler.assemble(function)
        Verifier(Interpreter(Inventory(function.leaves, function.stubs),
                             execute_static_helpers=True)).check(function, rows)
        ValueXmlWriter().write(function, rows, output / f"value_{name}_{function.name}.xml")
        StubXmlWriter().write(function, output / f"stub_{name}_{function.name}.xml")
        return ("ok", len(rows), tuple(rows)) if include_rows else ("ok", len(rows))
    except Exception as error:
        return "error", f"{type(error).__name__}: {error}"


def start_worker(context, database):
    """Start persistent child processes with only the database path."""
    pool = context.Pool(
        processes=4,
        initializer=initialize_worker,
        initargs=(database,),
    )
    try:
        if not pool.apply(worker_ready):
            raise RuntimeError("worker failed to initialize")
    except Exception:
        pool.terminate()
        pool.join()
        raise
    return pool


def stop_worker(pool, terminate=False):
    if pool is None:
        return
    try:
        if terminate:
            pool.terminate()
        else:
            pool.close()
    except ValueError:
        # The pool may already have been terminated after a timeout.
        pass
    pool.join()


def run_function(function, name, output, pool, *, include_rows=False):
    result = pool.apply_async(
        generate_function,
        (function.name, name, str(output), include_rows),
    )
    try:
        payload = result.get(timeout=FUNCTION_TIMEOUT_SECONDS)
    except mp.TimeoutError:
        stop_worker(pool, terminate=True)
        fallback = write_timeout_outputs(
            function, name, output, include_rows=include_rows
        )
        return (fallback[0], True, fallback[1]) if include_rows else (fallback, True)

    status = payload[0]
    if status != "ok":
        raise RuntimeError(payload[1])
    if include_rows:
        if len(payload) < 3:
            raise RuntimeError("iteration generation result did not include rows")
        _, count, rows = payload
    else:
        _, count = payload
    return (count, False, tuple(rows)) if include_rows else (count, False)


def _persistent_rows(rows, source_file, function_key):
    """Attach iteration identities to the legacy generator rows."""
    result = []
    row_ids = set()
    case_names = set()
    next_case = 1
    for row in rows:
        row_id = allocate_row_id(row_ids)
        row_ids.add(row_id)
        allocation = allocate_case_name(str(source_file), next_case, case_names, on_collision="skip")
        case_names.add(allocation.case_name)
        next_case = allocation.next_case_number
        obligations = tuple(getattr(row, "obligation_keys", ()) or ())
        meta = RowMeta(
            row_id,
            allocation.case_name,
            None,
            "generated",
            "active",
            0,
            0,
            function_key,
            obligations,
        )
        result.append(
            TestRow(
                dict(row.values),
                row.kind,
                row.description,
                dict(row.intent),
                dict(row.expected),
                meta,
                obligations,
            )
        )
    return tuple(result), next_case


def _accepted_state(accepted, function_key):
    for state in getattr(accepted, "function_states", ()):
        if state.function_key == function_key or state.function == function_key:
            return state
    return None


def _merge_rows(rows, source_file, function_key, accepted_state):
    """Reuse accepted BASE identities so duplicate generator rows stay matchable."""
    fresh, next_case = _persistent_rows(rows, source_file, function_key)
    if accepted_state is None:
        return fresh, next_case
    buckets = {}
    for row in accepted_state.base.rows:
        meta = getattr(row, "meta", None)
        if meta is not None and meta.status == "active":
            buckets.setdefault(tuple(sorted(meta.obligation_keys)), []).append(row)
    result = []
    for row in fresh:
        key = tuple(sorted(getattr(row, "obligation_keys", ()) or ()))
        candidates = buckets.get(key, [])
        if not candidates:
            result.append(row)
            continue
        previous = candidates.pop(0)
        previous_meta = previous.meta
        result.append(
            replace(
                row,
                meta=replace(
                    previous_meta,
                    origin="generated",
                    status="active",
                    function_key=function_key,
                    obligation_keys=key,
                ),
            )
        )
    return tuple(result), next_case


def _align_generated_rows(rows, value_snapshot):
    """Keep generated row lexemes in sync with Value XML output."""
    logical_rows = value_snapshot.logical_rows()
    result = []
    for index, row in enumerate(rows):
        if index >= len(logical_rows):
            result.append(row)
            continue
        logical = logical_rows[index]
        values = dict(row.values)
        expected = dict(row.expected)
        for name, value in logical.values.items():
            if value is not None:
                values[name] = value
        for name, value in logical.expected.items():
            if value is not None:
                expected[name] = value
        result.append(replace(row, values=values, expected=expected))
    return tuple(result)


def _stub_signature(state):
    return (
        state.function_name,
        state.return_type,
        state.return_storage_name,
        tuple((item.type_name, item.name) for item in state.parameters),
    )


def _add_module_stubs(target, states):
    for state in states:
        previous = target.get(state.stub_key)
        if previous is None:
            target[state.stub_key] = state
            continue
        if _stub_signature(previous) != _stub_signature(state):
            raise RuntimeError(f"stub signature conflict for {state.stub_key!r}")
        target[state.stub_key] = replace(
            previous,
            owners=previous.owners | state.owners,
        )


def _module_stubs_from_snapshots(snapshots):
    result = {}
    for function_key, snapshot in snapshots.items():
        _add_module_stubs(
            result,
            MergeBService._stub_states_from_snapshot(snapshot, function_key),
        )
    return result


def _complete_analysis_database_from_generated_stubs(database, generated):
    """Fill parser gaps from the already-emitted, per-function stub XML."""
    analysis = XmlDbReader().read(database)
    adapter = StubXmlAdapter(allow_legacy=True)
    snapshots = {
        function_key: adapter.read(item.stub_path, bootstrap=True)
        for function_key, item in generated.items()
    }
    changed = False
    for function in analysis.functions:
        called = {
            item.name: item
            for item in snapshots.get(function.name, ()).called_functions
        }
        if not called:
            continue
        stubs = []
        known = set()
        for stub in function.stubs:
            known.add(stub.name)
            emitted = called.get(stub.name)
            if emitted is not None:
                return_type = (
                    getattr(stub.ret_ctype, "name", None)
                    or stub.ret_struct
                )
                params_complete = all(
                    isinstance(item, (tuple, list))
                    and len(item) >= 2
                    and item[0]
                    and item[1]
                    for item in stub.params
                )
                updates = {}
                if not return_type and emitted.return_type:
                    updates.update(ret_ctype=None, ret_struct=emitted.return_type)
                if not params_complete:
                    updates["params"] = [
                        (argument.type_name, argument.name)
                        for argument in emitted.arguments
                    ]
                if updates:
                    stub = replace(stub, **updates)
                    changed = True
            stubs.append(stub)
        for emitted in called.values():
            if emitted.name in known or not emitted.return_type:
                continue
            stubs.append(
                StubInfo(
                    emitted.name,
                    None,
                    emitted.return_type,
                    [(argument.type_name, argument.name) for argument in emitted.arguments],
                )
            )
            changed = True
        function.stubs = stubs
    if changed:
        XmlDbWriter().write(analysis, database)


def _bootstrap_iteration(
    repository,
    module_key,
    source_file,
    database,
    generated,
    tessy_dialect,
):
    value_adapter = ValueXmlAdapter(allow_legacy=True)
    stub_adapter = StubXmlAdapter(allow_legacy=True)
    _complete_analysis_database_from_generated_stubs(database, generated)
    analysis_ref = repository.analysis_store.put(database)
    display_version = initial_display_version()
    base_id = new_persistent_id("snap")
    working_id = new_persistent_id("snap")
    states = []
    artifacts = []
    lifecycle_service = MergeBService(
        repository,
        mode="enforce",
        analysis_store=repository.analysis_store,
    )
    analysis_stubs, _, stub_conflicts, analysis_available = lifecycle_service._analysis_stub_states(
        analysis_ref,
        analysis_id=analysis_ref.analysis_id,
        module_key=module_key,
    )
    if stub_conflicts:
        raise RuntimeError(
            f"{module_key}: invalid analysis stub metadata: "
            + "; ".join(conflict.message for conflict in stub_conflicts)
        )
    stub_snapshots = {
        function_key: stub_adapter.read(item.stub_path, bootstrap=True)
        for function_key, item in generated.items()
    }
    generated_module_stubs = _module_stubs_from_snapshots(stub_snapshots)
    if analysis_available:
        module_stubs = {
            state.stub_key: replace(
                state,
                return_storage_name=generated_module_stubs.get(
                    state.stub_key, state
                ).return_storage_name,
            )
            for state in analysis_stubs
        }
    else:
        module_stubs = generated_module_stubs

    for function_key in sorted(generated):
        item = generated[function_key]
        value = value_adapter.read(item.value_path, bootstrap=True)
        stub = stub_snapshots[function_key]
        rows, next_case = _persistent_rows(
            _align_generated_rows(item.rows, value), source_file, function_key
        )
        arguments, diagnostics = MergeBService._argument_states_from_value_snapshot(value)
        if diagnostics:
            raise RuntimeError(
                f"{module_key}/{function_key}: invalid generated argument metadata: "
                + "; ".join(message for _, message, _ in diagnostics)
            )
        base = BaseSnapshot(
            base_id,
            0,
            analysis_ref.analysis_id,
            None,
            None,
            rows,
        )
        working = WorkingSnapshot(
            working_id,
            0,
            analysis_ref.analysis_id,
            tuple(row.meta.row_id for row in rows),
            rows,
            arguments or (),
        )
        states.append(
            FunctionIterationState(
                function_key,
                str(source_file),
                value.name,
                next_case,
                analysis_ref.analysis_id,
                display_version,
                base,
                working,
            )
        )
        if not analysis_available:
            _add_module_stubs(
                module_stubs,
                MergeBService._stub_states_from_snapshot(stub, function_key),
            )
        artifacts.extend(
            (
                ArtifactRecord(
                    f"base/{function_key}/value_{module_key}_{function_key}.xml",
                    "value_xml",
                    value_adapter.render(value, display_version=display_version),
                    value_adapter.read,
                ),
                ArtifactRecord(
                    f"base/{function_key}/stub_{module_key}_{function_key}.xml",
                    "stub_xml",
                    stub_adapter.render(stub, display_version=display_version),
                    stub_adapter.read,
                ),
                ArtifactRecord(
                    f"working/{function_key}/value_{module_key}_{function_key}.xml",
                    "value_xml",
                    value_adapter.render(value, display_version=display_version),
                    value_adapter.read,
                ),
                ArtifactRecord(
                    f"working/{function_key}/stub_{module_key}_{function_key}.xml",
                    "stub_xml",
                    stub_adapter.render(stub, display_version=display_version),
                    stub_adapter.read,
                ),
            )
        )

    if not states:
        raise RuntimeError(f"{module_key}: no generated functions to initialize iteration state")
    manifest = ModuleManifest(
        module_key,
        str(source_file),
        0,
        base_id,
        working_id,
        "utgen",
        None,
        tessy_dialect,
        analysis_ref,
        {state.function: display_version for state in states},
        1,
        new_persistent_id("rev"),
    )
    candidate = CandidateBundle(
        module_key,
        manifest,
        tuple(states),
        tuple(artifacts),
        tuple(module_stubs.values()),
    )
    transaction = repository.begin(module_key, 0)
    try:
        transaction.stage(candidate)
        report = transaction.validate()
        if not report.ok:
            raise RuntimeError(
                f"{module_key}: iteration bootstrap validation failed: "
                + "; ".join(issue.message for issue in report.issues)
            )
        return transaction.commit()
    except Exception:
        transaction.abort()
        raise


def _merge_b_iteration(
    repository,
    module_key,
    source_file,
    database,
    generated,
    tessy_dialect,
    mode,
):
    accepted = repository.load(module_key)
    _complete_analysis_database_from_generated_stubs(database, generated)
    analysis_ref = repository.analysis_store.put(database)
    value_adapter = ValueXmlAdapter(allow_legacy=True)
    stub_adapter = StubXmlAdapter(allow_legacy=True)
    generation = accepted.module_manifest.accepted_generation + 1
    new_snapshots = {}
    value_snapshots = {}
    stub_snapshots = {}
    for function_key in sorted(generated):
        item = generated[function_key]
        value = value_adapter.read(item.value_path, bootstrap=True)
        stub = stub_adapter.read(item.stub_path, bootstrap=True)
        aligned_rows = _align_generated_rows(item.rows, value)
        rows, _ = _merge_rows(
            aligned_rows,
            source_file,
            function_key,
            _accepted_state(accepted, function_key),
        )
        new_snapshots[function_key] = BaseSnapshot(
            new_persistent_id("snap"),
            generation,
            analysis_ref.analysis_id,
            None,
            None,
            rows,
        )
        value_snapshots[function_key] = value
        stub_snapshots[function_key] = stub
    if not new_snapshots:
        raise RuntimeError(f"{module_key}: no generated functions to merge")
    service = MergeBService(
        repository,
        mode=mode,
        analysis_store=repository.analysis_store,
        value_adapter=value_adapter,
        stub_adapter=stub_adapter,
    )
    analysis_stubs, _, stub_conflicts, analysis_available = service._analysis_stub_states(
        analysis_ref,
        analysis_id=analysis_ref.analysis_id,
        module_key=module_key,
    )
    if stub_conflicts:
        raise RuntimeError(
            f"{module_key}: invalid analysis stub metadata: "
            + "; ".join(conflict.message for conflict in stub_conflicts)
        )
    generated_module_stubs = _module_stubs_from_snapshots(stub_snapshots)
    new_stub_states = tuple(
        replace(
            state,
            return_storage_name=generated_module_stubs.get(
                state.stub_key, state
            ).return_storage_name,
        )
        for state in analysis_stubs
    ) if analysis_available else None
    outcome = service.merge(
        module_key,
        new_snapshots,
        complete=True,
        analysis_new=analysis_ref,
        source_file=str(source_file),
        new_stubs=new_stub_states,
        new_value_snapshot=value_snapshots,
        new_stub_snapshot=stub_snapshots,
        function_names={key: snapshot.name for key, snapshot in value_snapshots.items()},
    )
    if outcome.blocked or outcome.accepted is None:
        details = ", ".join(
            f"{conflict.code}: {conflict.message}" for conflict in outcome.conflicts
        ) or outcome.reason or "unknown error"
        raise RuntimeError(
            f"{module_key}: Merge B was not accepted: {details} "
            f"(accepted_analysis={accepted.module_manifest.analysis.analysis_id}, "
            f"new_analysis={analysis_ref.analysis_id})"
        )
    return outcome.accepted


def _publish_working_xml(accepted, output):
    for artifact in getattr(accepted, "artifacts", ()):
        path = PurePosixPath(artifact.relative_path)
        if len(path.parts) < 3 or path.parts[0] != "working" or path.suffix.lower() != ".xml":
            continue
        if artifact.kind not in {"value_xml", "stub_xml"}:
            continue
        target = Path(output) / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact.data)


def _inventory_for(repository, module_key, function_key):
    if not hasattr(repository, "load"):
        return None
    try:
        accepted = repository.load(module_key)
        analysis = repository.analysis_store.resolve(
            accepted.module_manifest.analysis.analysis_id
        ).analysis
        function = analysis.function(function_key)
    except RepositoryNotInitialized:
        return None
    return None if function is None else Inventory(function.leaves, function.stubs)


def merge_a_from_config():
    config = _read_config()
    mode, state_root, _ = iteration_settings_from_config(config)
    results = []
    for project in config.get("projects", ()):
        expected_project = f"tessy_{project['name']}"
        for module in project.get("modules", ()):
            module_key = module["module_name"]
            source_file = Path(module["source_file"])
            folder = Path(module.get("excel_input_dir", ""))
            if not folder.is_absolute():
                folder = CONFIG_PATH.parent / folder
            if not folder.is_dir():
                print(f"{module_key}: Excel folder not found: {folder}", flush=True)
                continue
            repository = FileSystemIterationRepository(state_root, current_only=True)
            repository.migrate_current()
            service = MergeAService(repository, mode=mode)
            for workbook_path in sorted(
                path
                for path in folder.rglob("*.xlsx")
                if path.is_file() and not path.name.startswith("~$")
            ):
                try:
                    workbook = read_complete_export(
                        workbook_path,
                        expected_module=source_file.name,
                        expected_project=expected_project,
                    )
                    function_key = workbook.meta.testobject
                    outcome = service.merge(
                        module_key,
                        workbook,
                        function_key=function_key,
                        inventory=_inventory_for(repository, module_key, function_key),
                    )
                    if getattr(outcome, "committed", False):
                        repository.compact_current(module_key)
                    results.append(outcome)
                    print(
                        f"{module_key}: {workbook_path.name}: Merge A {outcome.reason or 'ok'}",
                        flush=True,
                    )
                except Exception as error:
                    print(
                        f"{module_key}: {workbook_path.name}: Merge A failed: {error}",
                        flush=True,
                    )
    return results


def run():
    global _LAST_CONFIG
    _LAST_CONFIG = None
    projects, include_dirs = projects_from_config()
    TIME_LOG_PATH.write_text("", encoding="utf-8")
    indexer = ModuleIndexer({"defines": ("__GHS__","USE_CANTRV_TJA1145", "__CCRH__", "USE_IOC", "USE_PMIC_FS26"), "includes": include_dirs})
    context = mp.get_context("spawn")
    total_start = perf_counter()

    try:
        for project in projects:
            project_start = perf_counter()
            try:
                for module in project["modules"]:
                    module_start = perf_counter()
                    name = module["module_name"]
                    try:
                        iteration_mode, state_root, tessy_dialect = iteration_settings_from_config(
                            _LAST_CONFIG or {}
                        )
                        iteration_enabled = iteration_mode == "enforce"
                        source = Path(module["source_file"])
                        root = Path(project["root_path"])
                        output = XML_INPUT_PATH / name
                        output.mkdir(parents=True, exist_ok=True)
                        print(f"{name}: indexing {source}", flush=True)
                        analysis = indexer.index(source, root)
                        database = output / f"db_{name}.xml"
                        XmlDbWriter().write(analysis, database)
                        print(f"{name}: generating {len(analysis.functions)} functions", flush=True)

                        generated = {}
                        worker_pool = None
                        try:
                            worker_start = perf_counter()
                            worker_pool = start_worker(context, database)
                            report_time(f"{name}: worker init time {elapsed(worker_start)}")

                            function_count = len(analysis.functions)
                            for function_index, function in enumerate(analysis.functions):
                                function_start = perf_counter()
                                row_count = 0
                                try:
                                    print(f"{name}: generating {function.name}", flush=True)
                                    if iteration_enabled:
                                        row_count, timed_out, rows = run_function(
                                            function,
                                            name,
                                            output,
                                            worker_pool,
                                            include_rows=True,
                                        )
                                    else:
                                        row_count, timed_out = run_function(
                                            function, name, output, worker_pool
                                        )
                                        rows = ()
                                    if iteration_enabled:
                                        generated[function.name] = _GeneratedFunction(
                                            tuple(rows),
                                            output / f"value_{name}_{function.name}.xml",
                                            output / f"stub_{name}_{function.name}.xml",
                                        )
                                    if timed_out:
                                        print(
                                            f"{name}: {function.name}: timeout; "
                                            "fallback files written",
                                            flush=True,
                                        )
                                        # run_function already terminated the timed-out pool.
                                        worker_pool = None
                                        if function_index + 1 < function_count:
                                            worker_start = perf_counter()
                                            worker_pool = start_worker(context, database)
                                            report_time(
                                                f"{name}: worker restart time "
                                                f"{elapsed(worker_start)}"
                                            )
                                finally:
                                    report_time(
                                        f"{name}: {function.name}: {row_count} rows; "
                                        f"time {elapsed(function_start)}"
                                    )
                        finally:
                            if worker_pool is not None:
                                stop_worker(worker_pool)
                        if iteration_enabled:
                            repository = FileSystemIterationRepository(state_root, current_only=True)
                            repository.migrate_current()
                            try:
                                repository.load(name)
                            except RepositoryNotInitialized:
                                accepted = _bootstrap_iteration(
                                    repository,
                                    name,
                                    source,
                                    database,
                                    generated,
                                    tessy_dialect,
                                )
                                print(f"{name}: iteration state bootstrapped", flush=True)
                            else:
                                accepted = _merge_b_iteration(
                                    repository,
                                    name,
                                    source,
                                    database,
                                    generated,
                                    tessy_dialect,
                                    iteration_mode,
                                )
                                print(f"{name}: iteration Merge B accepted", flush=True)
                            accepted = repository.compact_current(name)
                            _publish_working_xml(accepted, output)
                    finally:
                        report_time(f"{name}: total time {elapsed(module_start)}")
            finally:
                report_time(f"{project['name']}: total time {elapsed(project_start)}")
    finally:
        report_time(f"ALL PROJECTS: total time {elapsed(total_start)}")
    mainBody()


if __name__ == "__main__":
    mp.freeze_support()
    print("1. Normal run")
    print("2. Excel only")
    print("3. Merge A (all Excel folders)")
    option = input("Select option: ").strip()
    if option == "1":
        run()
    elif option == "2":
        mainBody()
    elif option == "3":
        merge_a_from_config()
    else:
        raise SystemExit("Invalid option; enter 1, 2, or 3")
