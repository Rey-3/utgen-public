"""Generate verified rows and candidate JSON data for configured modules."""

from dataclasses import dataclass
import multiprocessing as mp
from pathlib import Path
import sys
from time import perf_counter
from typing import Mapping

import yaml

from utgen.assembler import RowAssembler
from utgen.db import ModuleIndexer, XmlDbReader, XmlDbWriter
from utgen.interp import Interpreter, Verifier
from utgen.mcdc import McdcGenerator
from utgen.model import AssemblyResult, Direction, GeneratedRowProvenance, Inventory, TestRow
from utgen.solver import Solver
from utgen.iteration.merge import build_candidate


CONFIG_PATH = (
    Path(sys.executable).with_name("config.yaml")
    if getattr(sys, "frozen", False)
    else Path(__file__).with_name("config.yaml")
)
TIME_LOG_PATH = CONFIG_PATH.with_name("generation_times.txt")
FUNCTION_TIMEOUT_SECONDS = 180
XML_INPUT_PATH = CONFIG_PATH.with_name("input")
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


def write_timeout_outputs(function, name=None, output=None, *, include_rows=False):
    """Return verified timeout rows without emitting legacy XML files."""

    del name, output, include_rows
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


def generate_function(function_name, name=None, output=None, include_rows=False):
    """Generate and verify one function from the worker's Analysis DB."""

    del name, output, include_rows
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


def run_function(function, name, output, pool, *, include_rows=False):
    del include_rows
    result = pool.apply_async(
        generate_function,
        (function.name, name, str(output)),
    )
    try:
        payload = result.get(timeout=FUNCTION_TIMEOUT_SECONDS)
    except mp.TimeoutError:
        stop_worker(pool, terminate=True)
        return write_timeout_outputs(function, name, output), True

    if isinstance(payload, tuple) and payload and payload[0] == "error":
        raise RuntimeError(payload[1])
    if not isinstance(payload, AssemblyResult):
        raise RuntimeError("generation result did not include an AssemblyResult")
    return payload, False


def _module_database(analysis, module):
    output = XML_INPUT_PATH / module
    output.mkdir(parents=True, exist_ok=True)
    database = output / f"db_{module}.xml"
    XmlDbWriter().write(analysis, database)
    return database, output


def run():
    """Generate candidates for all configured modules.

    Persistence and workbook projection belong to the later workflow layer; this
    boundary only indexes, generates, verifies, and returns ordinary dictionaries.
    """

    global _LAST_CONFIG
    _LAST_CONFIG = None
    projects, include_dirs = projects_from_config()
    indexer = ModuleIndexer(
        {
            "defines": (
                "__GHS__",
                "USE_CANTRV_TJA1145",
                "__CCRH__",
                "USE_IOC",
                "USE_PMIC_FS26",
            ),
            "includes": include_dirs,
        }
    )
    context = mp.get_context("spawn")
    candidates = []
    total_start = perf_counter()

    try:
        for project in projects:
            project_start = perf_counter()
            try:
                for module in project["modules"]:
                    module_start = perf_counter()
                    name = module["module_name"]
                    source = Path(module["source_file"])
                    root = Path(project["root_path"])
                    print(f"{name}: indexing {source}", flush=True)
                    analysis = indexer.index(source, root)
                    database, output = _module_database(analysis, name)
                    print(
                        f"{name}: generating {len(analysis.functions)} functions",
                        flush=True,
                    )

                    generated = {}
                    worker_pool = None
                    try:
                        worker_start = perf_counter()
                        worker_pool = start_worker(context, database)
                        report_time(f"{name}: worker init time {elapsed(worker_start)}")
                        function_count = len(analysis.functions)
                        for function_index, function in enumerate(analysis.functions):
                            function_start = perf_counter()
                            result = None
                            try:
                                print(f"{name}: generating {function.name}", flush=True)
                                result, timed_out = run_function(
                                    function,
                                    name,
                                    output,
                                    worker_pool,
                                )
                                generated[function.name] = _GeneratedFunction(result)
                                if timed_out:
                                    print(
                                        f"{name}: {function.name}: timeout; fallback rows used",
                                        flush=True,
                                    )
                                    worker_pool = None
                                    if function_index + 1 < function_count:
                                        worker_start = perf_counter()
                                        worker_pool = start_worker(context, database)
                                        report_time(
                                            f"{name}: worker restart time {elapsed(worker_start)}"
                                        )
                            finally:
                                report_time(
                                    f"{name}: {function.name}: "
                                    f"{len(result.rows) if result is not None else 0} rows; "
                                    f"time {elapsed(function_start)}"
                                )
                    finally:
                        if worker_pool is not None:
                            stop_worker(worker_pool)
                    candidates.append(
                        build_candidate(
                            analysis,
                            generated,
                            project["name"],
                            name,
                            getattr(analysis, "sha256", ""),
                        )
                    )
                    print(f"{name}: candidate built", flush=True)
                    report_time(f"{name}: total time {elapsed(module_start)}")
            finally:
                report_time(f"{project['name']}: total time {elapsed(project_start)}")
    finally:
        report_time(f"ALL PROJECTS: total time {elapsed(total_start)}")
    return candidates


if __name__ == "__main__":
    mp.freeze_support()
    run()
