"""Config, CLI, and worker orchestration for the iteration workflow."""

import argparse
from dataclasses import dataclass
import multiprocessing as mp
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from typing import Mapping

import yaml

from utgen.assembler import RowAssembler
from utgen.db import ModuleIndexer, XmlDbReader, XmlDbWriter
from utgen.interp import Interpreter, Verifier
from utgen.mcdc import McdcGenerator
from utgen.model import AssemblyResult, Direction, GeneratedRowProvenance, Inventory, TestRow
from utgen.solver import Solver


CONFIG_PATH = (
    Path(sys.executable).with_name("config.yaml")
    if getattr(sys, "frozen", False)
    else Path(__file__).with_name("config.yaml")
)
TIME_LOG_PATH = CONFIG_PATH.with_name("generation_times.txt")
FUNCTION_TIMEOUT_SECONDS = 180
_LAST_CONFIG = None


@dataclass(frozen=True)
class _GeneratedFunction:
    result: AssemblyResult


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


_WORKER_DATABASE = None


def initialize_worker(database):
    """Retain only the on-disk Analysis DB path when a worker starts."""

    global _WORKER_DATABASE
    _WORKER_DATABASE = Path(database)


def worker_ready():
    return _WORKER_DATABASE is not None


def generate_function(function_name):
    """Generate and verify one function from the worker's Analysis DB."""

    try:
        if _WORKER_DATABASE is None:
            raise RuntimeError("worker database is not initialized")
        function = XmlDbReader().read(_WORKER_DATABASE).function(function_name)
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


def start_worker(context, database):
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
        pass
    pool.join()


def run_function(function, pool):
    result = pool.apply_async(
        generate_function,
        (function.name,),
    )
    try:
        payload = result.get(timeout=FUNCTION_TIMEOUT_SECONDS)
    except mp.TimeoutError:
        stop_worker(pool, terminate=True)
        return write_timeout_outputs(function), True

    if isinstance(payload, tuple) and payload and payload[0] == "error":
        raise RuntimeError(payload[1])
    if not isinstance(payload, AssemblyResult):
        raise RuntimeError("generation result did not include an AssemblyResult")
    return payload, False


def generate_module(project, module, include_dirs=()):
    """Index and generate one module using a temporary worker DB.

    The iteration workflow owns the accepted project-scoped DB.  Workers still
    consume the existing XML representation, but it is kept in a temporary
    directory and never becomes a second source of truth.
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
    analysis = indexer.index(source, root)
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="utgen-analysis-") as temporary_dir:
        database = Path(temporary_dir) / f"db_{module['module_name']}.xml"
        XmlDbWriter().write(analysis, database)
        generated = {}
        worker_pool = None
        try:
            worker_pool = start_worker(context, database)
            functions = tuple(analysis.functions)
            for index, function in enumerate(functions):
                result, timed_out = run_function(function, worker_pool)
                generated[function.name] = _GeneratedFunction(result)
                if timed_out:
                    worker_pool = None
                    if index + 1 < len(functions):
                        worker_pool = start_worker(context, database)
        finally:
            if worker_pool is not None:
                stop_worker(worker_pool)
    return analysis, generated


def run_generation(config=None):
    """Run normal generation/next-tool iteration for configured modules."""

    from utgen.iteration.workflow import run_generation as workflow_run_generation

    return workflow_run_generation(
        CONFIG_PATH if config is None else config,
        generator=generate_module,
    )


def run_excel_export(config=None):
    """Project current JSON state to Excel without indexing or generation."""

    from utgen.iteration.workflow import run_excel_export as workflow_run_excel_export

    return workflow_run_excel_export(CONFIG_PATH if config is None else config)


def run_user_merge(config=None):
    """Merge user-edited Excel workbooks into current JSON state."""

    from utgen.iteration.workflow import run_user_merge as workflow_run_user_merge

    return workflow_run_user_merge(CONFIG_PATH if config is None else config)


def run():
    """Backward-compatible entry point for the normal iteration workflow."""

    return run_generation()


def _load_cli_config(path: Path):
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("config.yaml must contain a mapping")
    return value


def main(argv=None):
    """Dispatch the three supported CLI modes.

    Mode 1 performs normal/next-tool generation, mode 2 rebuilds Excel from
    current JSON, and mode 3 merges user-edited Excel into current JSON.
    """

    parser = argparse.ArgumentParser(description="UTGen iteration workflow")
    parser.add_argument("mode", nargs="?", choices=("1", "2", "3"))
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    config_path = args.config or CONFIG_PATH
    mode = args.mode
    if mode is None:
        print("1. Normal generation / next tool iteration")
        print("2. Rebuild Excel from current JSON")
        print("3. Merge user Excel into current JSON")
        mode = input("Select option: ").strip()
        if mode not in {"1", "2", "3"}:
            parser.error("mode must be 1, 2, or 3")
    _load_cli_config(config_path)
    if mode == "1":
        return run_generation(config_path)
    if mode == "2":
        return run_excel_export(config_path)
    return run_user_merge(config_path)


if __name__ == "__main__":
    mp.freeze_support()
    main()
