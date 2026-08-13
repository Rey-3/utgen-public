"""Generate UT inputs for every module listed in config.yaml."""
import multiprocessing as mp
from pathlib import Path
import sys
from time import perf_counter

import yaml

from utgen.assembler import RowAssembler
from utgen.db import ModuleIndexer
from utgen.emit import StubXmlWriter, ValueXmlWriter
from utgen.interp import Interpreter, Verifier
from utgen.mcdc import McdcGenerator
from utgen.model import Direction, Inventory, TestRow
from utgen.solver import Solver
from tessy_parse import mainBody


CONFIG_PATH = (Path(sys.executable).with_name("config.yaml")
               if getattr(sys, "frozen", False) else Path(__file__).with_name("config.yaml"))
TIME_LOG_PATH = CONFIG_PATH.with_name("generation_times.txt")
FUNCTION_TIMEOUT_SECONDS = 200
XML_INPUT_PATH = CONFIG_PATH.with_name("input")


def projects_from_config():
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    tessy_include = config.get("Tessy", {}).get("include", ())
    if isinstance(tessy_include, str):
        tessy_include = (tessy_include,)
    return config["projects"], tessy_include


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


def write_timeout_outputs(function, name, output):
    rows = fallback_rows(function)
    note = f"generation timed out after {FUNCTION_TIMEOUT_SECONDS}s; fallback values used"
    ValueXmlWriter().write(function, rows, output / f"value_{name}_{function.name}.xml",
                           default_range="0,1")
    StubXmlWriter().write(function, output / f"stub_{name}_{function.name}.xml")
    return len(rows)


_WORKER_ANALYSIS = None


def initialize_worker(analysis):
    """Retain the indexed module when the persistent worker starts."""
    global _WORKER_ANALYSIS
    _WORKER_ANALYSIS = analysis


def worker_ready():
    """Small handshake used to wait until the worker initializer completes."""
    return _WORKER_ANALYSIS is not None


def generate_function(function_name, name, output):
    """Generate one function using the module already loaded in the worker."""
    try:
        if _WORKER_ANALYSIS is None:
            raise RuntimeError("worker analysis is not initialized")

        function = _WORKER_ANALYSIS.function(function_name)
        if function is None:
            raise ValueError(f"Function {function_name!r} was not found")

        output = Path(output)
        assembler = RowAssembler(Solver(), McdcGenerator(), include_boundary=False,
                                 include_fault=False)
        rows, notes = assembler.assemble(function)
        notes.extend(Verifier(Interpreter(Inventory(function.leaves, function.stubs))).check(
            function, rows))
        ValueXmlWriter().write(function, rows, output / f"value_{name}_{function.name}.xml")
        StubXmlWriter().write(function, output / f"stub_{name}_{function.name}.xml")
        return "ok", len(rows)
    except Exception as error:
        return "error", f"{type(error).__name__}: {error}"


def start_worker(context, analysis):
    """Start one persistent child process with the indexed module."""
    pool = context.Pool(
        processes=1,
        initializer=initialize_worker,
        initargs=(analysis,),
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


def run_function(function, name, output, pool):
    result = pool.apply_async(
        generate_function,
        (function.name, name, str(output)),
    )
    try:
        status, payload = result.get(timeout=FUNCTION_TIMEOUT_SECONDS)
    except mp.TimeoutError:
        stop_worker(pool, terminate=True)
        return write_timeout_outputs(function, name, output), True

    if status != "ok":
        raise RuntimeError(payload)
    return payload, False


def run():
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
                        source = Path(module["source_file"])
                        root = Path(project["root_path"])
                        output = XML_INPUT_PATH / name
                        output.mkdir(parents=True, exist_ok=True)
                        print(f"{name}: indexing {source}", flush=True)
                        analysis = indexer.index(source, root)
                        print(f"{name}: generating {len(analysis.functions)} functions", flush=True)

                        worker_pool = None
                        try:
                            worker_start = perf_counter()
                            worker_pool = start_worker(context, analysis)
                            report_time(f"{name}: worker init time {elapsed(worker_start)}")

                            function_count = len(analysis.functions)
                            for function_index, function in enumerate(analysis.functions):
                                function_start = perf_counter()
                                row_count = 0
                                try:
                                    print(f"{name}: generating {function.name}", flush=True)
                                    row_count, timed_out = run_function(
                                        function, name, output, worker_pool
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
                                            worker_pool = start_worker(context, analysis)
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
    option = input("Select option: ").strip()
    if option == "1":
        run()
    elif option == "2":
        mainBody()
    else:
        raise SystemExit("Invalid option; enter 1 or 2")
