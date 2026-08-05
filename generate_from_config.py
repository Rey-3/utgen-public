"""Generate UT inputs for every module listed in config.yaml."""
import multiprocessing as mp
from queue import Empty
from pathlib import Path
import sys
from time import perf_counter

import yaml

from utgen.assembler import RowAssembler
from utgen.db import ModuleIndexer, XmlDbWriter
from utgen.emit import ReportWriter, StubXmlWriter, ValueXmlWriter
from utgen.interp import Interpreter, Verifier
from utgen.mcdc import McdcGenerator
from utgen.model import Direction, Inventory, TestRow
from utgen.solver import Solver


CONFIG_PATH = (Path(sys.executable).with_name("config.yaml")
               if getattr(sys, "frozen", False) else Path(__file__).with_name("config.yaml"))
TIME_LOG_PATH = CONFIG_PATH.with_name("generation_times.txt")
FUNCTION_TIMEOUT_SECONDS = 200


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
    ReportWriter().write(function, rows, [note],
                         output / f"report_{name}_{function.name}.md")
    return len(rows)


def generate_function(function, name, output, result_queue):
    try:
        assembler = RowAssembler(Solver(), McdcGenerator(), include_boundary=False,
                                 include_fault=False)
        rows, notes = assembler.assemble(function)
        notes.extend(Verifier(Interpreter(Inventory(function.leaves, function.stubs))).check(
            function, rows))
        ValueXmlWriter().write(function, rows, output / f"value_{name}_{function.name}.xml")
        StubXmlWriter().write(function, output / f"stub_{name}_{function.name}.xml")
        ReportWriter().write(function, rows, notes,
                             output / f"report_{name}_{function.name}.md")
        result_queue.put(("ok", len(rows)))
    except Exception as error:
        result_queue.put(("error", f"{type(error).__name__}: {error}"))


def run_function(function, name, output, context):
    result_queue = context.Queue()
    process = context.Process(target=generate_function,
                              args=(function, name, output, result_queue))
    try:
        process.start()
        process.join(FUNCTION_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join()
            return write_timeout_outputs(function, name, output), True
        try:
            status, payload = result_queue.get(timeout=1)
        except Empty as error:
            raise RuntimeError(f"function process exited with code {process.exitcode}") from error
        if status != "ok":
            raise RuntimeError(payload)
        return payload, False
    finally:
        result_queue.close()


def run():
    projects, include_dirs = projects_from_config()
    TIME_LOG_PATH.write_text("", encoding="utf-8")
    indexer = ModuleIndexer({"defines": ("__GHS__",), "includes": include_dirs})
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
                        output = Path(module["excel_input_dir"])
                        output.mkdir(parents=True, exist_ok=True)
                        print(f"{name}: indexing {source}", flush=True)
                        analysis = indexer.index(source, root)
                        db_start = perf_counter()
                        XmlDbWriter().write(analysis, output / f"db_{name}.xml")
                        report_time(f"{name}: db xml time {elapsed(db_start)}")
                        print(f"{name}: generating {len(analysis.functions)} functions", flush=True)
                        for function in analysis.functions:
                            function_start = perf_counter()
                            row_count = 0
                            try:
                                print(f"{name}: generating {function.name}", flush=True)
                                row_count, timed_out = run_function(function, name, output, context)
                                if timed_out:
                                    print(f"{name}: {function.name}: timeout; fallback files written",
                                          flush=True)
                            finally:
                                report_time(f"{name}: {function.name}: {row_count} rows; "
                                            f"time {elapsed(function_start)}")
                    finally:
                        report_time(f"{name}: total time {elapsed(module_start)}")
            finally:
                report_time(f"{project['name']}: total time {elapsed(project_start)}")
    finally:
        report_time(f"ALL PROJECTS: total time {elapsed(total_start)}")


if __name__ == "__main__":
    mp.freeze_support()
    run()
