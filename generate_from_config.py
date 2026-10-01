"""Config, CLI, and worker orchestration for the iteration workflow."""

import argparse
import cProfile
from dataclasses import dataclass
from functools import partial
import multiprocessing as mp
from pathlib import Path
import pstats
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Mapping

import yaml
from clone_tessy_project import TessyProjectCloner
from tessy_compilation import Tessy_Compilation
from utgen.assembler import RowAssembler
from utgen.db import ModuleIndexer, XmlDbWriter
from utgen.interp import Interpreter, Verifier
from utgen.mcdc import McdcGenerator
from utgen.model import AssemblyResult, Direction, GeneratedRowProvenance, Inventory, TestRow
from utgen.solver import Solver


CONFIG_PATH = (
    Path(sys.executable).with_name("config.yaml")
    if getattr(sys, "frozen", False)
    else Path(__file__).with_name("config.yaml")
)
FUNCTION_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class _GeneratedFunction:
    result: AssemblyResult


def fallback_rows(function):
    first, second = {}, {}
    for leaf in function.leaves:
        if leaf.direction in (Direction.IN, Direction.INOUT):
            first[leaf.name] = 0
            second[leaf.name] = 1
    return [
        TestRow(first, "Timeout", "generation timeout"),
        TestRow(second, "Timeout", "generation timeout"),
    ]


def _verified_result(function, rows, recipes=(), generated_rows=()):
    rows = list(rows)
    Verifier(
        Interpreter(Inventory(function.leaves, function.stubs), execute_static_helpers=True)
    ).check(function, rows)
    return AssemblyResult(tuple(rows), tuple(recipes), tuple(generated_rows))


def write_timeout_outputs(function):
    """Return verified timeout rows without emitting legacy XML files."""

    rows = fallback_rows(function)
    return _verified_result(
        function,
        rows,
        (),
        (
            GeneratedRowProvenance("Timeout", 0, 0),
            GeneratedRowProvenance("Timeout", 1, 1),
        ),
    )


_WORKER_ANALYSIS = None
_WORKER_PROFILE_DIR = None


def initialize_worker(analysis, profile_dir=None):
    """Reuse the indexed graph; spawn preserves its shared objects."""

    global _WORKER_ANALYSIS, _WORKER_PROFILE_DIR
    _WORKER_ANALYSIS = analysis
    _WORKER_PROFILE_DIR = None if profile_dir is None else Path(profile_dir)


def worker_ready():
    return _WORKER_ANALYSIS is not None


def generate_function(function_name):
    """Generate and verify one function from the worker's Analysis DB."""

    if _WORKER_PROFILE_DIR is None:
        return _generate_function(function_name)
    _WORKER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    profiler = cProfile.Profile()
    try:
        return profiler.runcall(_generate_function, function_name)
    finally:
        profiler.dump_stats(_WORKER_PROFILE_DIR / f"{function_name}.prof")


def _generate_function(function_name):
    try:
        if _WORKER_ANALYSIS is None:
            raise RuntimeError("worker database is not initialized")
        function = _WORKER_ANALYSIS.function(function_name)
        if function is None:
            raise ValueError(f"Function {function_name!r} was not found")
        result = RowAssembler(
            Solver(), McdcGenerator(), include_boundary=False, include_fault=False
        ).assemble_with_provenance(function)
        return _verified_result(
            function, result.rows, result.recipes, result.generated_rows
        )
    except Exception as error:
        return "error", f"{type(error).__name__}: {error}"


def start_worker(context, analysis, profile_dir=None, processes=8):
    pools = [
        context.Pool(
            processes=1,
            initializer=initialize_worker,
            initargs=(analysis, profile_dir),
        )
        for _ in range(processes)
    ]
    try:
        for pool in pools:
            if not pool.apply(worker_ready):
                raise RuntimeError("worker failed to initialize")
    except Exception:
        for pool in pools:
            stop_worker(pool, terminate=True)
        raise
    return pools


def stop_worker(pool, terminate=False):
    if pool is None:
        return
    try:
        if terminate:
            pool.terminate()
        else:
            pool.close()
    except ValueError:
        pass
    pool.join()


def _collect_function(result):
    payload = result.get()
    if isinstance(payload, tuple) and payload and payload[0] == "error":
        raise RuntimeError(payload[1])
    if not isinstance(payload, AssemblyResult):
        raise RuntimeError("generation result did not include an AssemblyResult")
    return payload


def _submit_function(pool, function):
    return (
        function,
        pool.apply_async(
            generate_function,
            (function.name,),
        ),
        time.monotonic(),
    )


def _print_function_progress(current, total, name, elapsed, status=None):
    suffix = f"{elapsed:.2f}s" if status is None else f"{status}  {elapsed:.2f}s"
    print(f"[{current}/{total}] {name:<60} {suffix}")


def generate_module(project, module, include_dirs=(), profile_dir=None):
    """Index once and pass the precomputed graph to each worker.

    The iteration workflow owns the accepted project-scoped XML DB. Its temporary
    export is separate from worker transport, which preserves object sharing.
    """

    root = Path(project.get("root_path", "."))
    source = Path(module["source_file"])
    if not source.is_absolute():
        source = root / source
    indexer = ModuleIndexer(
        {
            "defines": (
                "__GHS__",
                "USE_CANTRV_TJA1145",
                "__CCRH__",
                "USE_IOC",
                "USE_PMIC_FS26",
            ),
            "includes": tuple(include_dirs or ()),
        }
    )
    module_name = module["module_name"]
    database_started = time.monotonic()
    print(f"\n{module_name}: Generating analysis database...")

    def report_database_progress(current, total, function_name, elapsed):
        _print_function_progress(current, total, function_name, elapsed)

    analysis = indexer.index(source, root, progress=report_database_progress)
    database_elapsed = time.monotonic() - database_started
    print(f"\n{module_name}: database generation completed")
    print(f"Functions: {len(analysis.functions)}")
    print(f"Database time: {database_elapsed:.2f}s")

    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="utgen-analysis-") as temporary_dir:
        database = Path(temporary_dir) / f"db_{module['module_name']}.xml"
        XmlDbWriter().write(analysis, database)
        generated = {}
        worker_pools = []
        active = {}
        worker_profile_dir = None if profile_dir is None else Path(profile_dir) / module["module_name"]
        functions = tuple(analysis.functions)
        try:
            if functions:
                process_count = min(mp.cpu_count(), len(functions))
                generation_started = time.monotonic()
                completed = 0
                print(
                    f"\n{module_name}: Generating test cases with "
                    f"{process_count} workers..."
                )
                worker_pools = start_worker(
                    context, analysis, worker_profile_dir, process_count
                )
                remaining = iter(functions)
                for pool in worker_pools:
                    function = next(remaining)
                    active[pool] = _submit_function(pool, function)

                while active:
                    now = time.monotonic()
                    progressed = False
                    for pool, (function, pending, started_at) in tuple(active.items()):
                        if pending.ready():
                            elapsed = time.monotonic() - started_at
                            try:
                                result = _collect_function(pending)
                            except Exception:
                                completed += 1
                                _print_function_progress(
                                    completed, len(functions), function.name, elapsed, "ERROR"
                                )
                                raise
                            generated[function.name] = _GeneratedFunction(result)
                            completed += 1
                            _print_function_progress(
                                completed, len(functions), function.name, elapsed
                            )
                            next_function = next(remaining, None)
                            if next_function is None:
                                pool.close()
                                del active[pool]
                            else:
                                active[pool] = _submit_function(pool, next_function)
                            progressed = True
                            continue

                        if now - started_at < FUNCTION_TIMEOUT_SECONDS:
                            continue

                        stop_worker(pool, terminate=True)
                        worker_pools.remove(pool)
                        del active[pool]
                        elapsed = time.monotonic() - started_at
                        generated[function.name] = _GeneratedFunction(
                            write_timeout_outputs(function)
                        )
                        completed += 1
                        _print_function_progress(
                            completed, len(functions), function.name, elapsed, "TIMEOUT"
                        )
                        next_function = next(remaining, None)
                        if next_function is not None:
                            replacement = start_worker(
                                context, analysis, worker_profile_dir, 1
                            )[0]
                            worker_pools.append(replacement)
                            active[replacement] = _submit_function(
                                replacement, next_function
                            )
                        progressed = True

                    if not progressed:
                        time.sleep(0.01)

                generation_elapsed = time.monotonic() - generation_started
                print(f"\n{module_name}: test generation completed")
                print(f"Functions: {len(functions)}")
                print(f"Workers: {process_count}")
                print(f"Wall time: {generation_elapsed:.2f}s")
        except Exception:
            for pool in tuple(worker_pools):
                stop_worker(pool, terminate=True)
            worker_pools.clear()
            raise
        finally:
            for pool in tuple(worker_pools):
                stop_worker(pool)
        raw_database = database.read_bytes()
    return analysis, generated, raw_database


def run_generation(config=None, profile_dir=None):
    """Run normal generation/next-tool iteration for configured modules."""

    from utgen.iteration.workflow import run_generation as workflow_run_generation

    generator = generate_module if profile_dir is None else partial(generate_module, profile_dir=profile_dir)
    return workflow_run_generation(CONFIG_PATH if config is None else config, generator=generator)


def run_excel_export(config=None):
    """Project current JSON state to Excel without indexing or generation."""

    from utgen.iteration.workflow import run_excel_export as workflow_run_excel_export

    return workflow_run_excel_export(CONFIG_PATH if config is None else config)


def run_user_merge(config=None):
    """Merge user-edited Excel workbooks into current JSON state."""

    from utgen.iteration.workflow import run_user_merge as workflow_run_user_merge

    return workflow_run_user_merge(CONFIG_PATH if config is None else config)


def run_tessy_import(config):
    tessy = config["Tessy"]
    for project in config.get("projects", ()):
        workspace = Path(project.get("workspace") or tessy["default_workspace"])
        project_name = project["name"]
        root_path = project["root_path"]
        if not (workspace / project_name).exists():
            failed = TessyProjectCloner(
                workspace=workspace,
                template=tessy.get("template_path") or None,
            ).run(project_name, root_path)
            if failed:
                raise RuntimeError(f"Failed to clone TESSY project {project_name}")

        Tessy_Compilation(
            work_dir=workspace,
            root_path=root_path,
            project_name=project_name,
            collection_name=project["collection_name"],
            report_dir=project.get("report_dir", workspace / project_name / "reports"),
            modules=[SimpleNamespace(**module) for module in project.get("modules", ())],
        ).compilation()
    return []


def _load_cli_config(path: Path):
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("config.yaml must contain a mapping")
    return value


def _print_summaries(summaries):
    for item in summaries:
        if item.status == "error":
            print(f"{item.project}/{item.module}: error: {item.error}")
            continue
        details = item.summary
        print(
            f"{item.project}/{item.module}: {item.status}; "
            f"rows {details.get('rows', {})}; "
            f"stubs {details.get('stubs', {})}; "
            f"JSON {details.get('json', 'skipped')}; "
            f"{details.get('excel_written', 0)} Excel files written"
        )


def main(argv=None):
    """Dispatch the three supported CLI modes.

    Mode 1 performs normal/next-tool generation, mode 2 rebuilds Excel from
    current JSON, and mode 3 merges user-edited Excel into current JSON.
    """

    parser = argparse.ArgumentParser(description="UTGen iteration workflow")
    parser.add_argument("mode", nargs="?", choices=("1", "2", "3", "4"))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--profile-dir", type=Path)
    args = parser.parse_args(argv)
    config_path = args.config or CONFIG_PATH
    mode = args.mode
    if mode is None:
        print("1. Normal generation / next tool iteration")
        print("2. Rebuild Excel from current JSON")
        print("3. import excel")
        print("4. Merge user Excel into current JSON")
        mode = input("Select option: ").strip()
        if mode not in {"1", "2", "3", "4"}:
            parser.error("mode must be 1, 2, 3, or 4")
    config = _load_cli_config(config_path)
    if mode == "1":
        if args.profile_dir is None:
            result = run_generation(config_path)
        else:
            profiler = cProfile.Profile()
            profiler.enable()
            try:
                result = run_generation(config_path, args.profile_dir)
            finally:
                profiler.disable()
                print("\nTotal profile:")
                pstats.Stats(profiler).sort_stats("cumulative").print_stats(100)
    elif mode == "2":
        result = run_excel_export(config_path)
    elif mode == "3":
        result = run_tessy_import(config)
    else:
        result = run_user_merge(config_path)
    _print_summaries(result)
    return result


if __name__ == "__main__":
    mp.freeze_support()
    main()
