"""Rebuild PDM databases from source, then regenerate UT XML and reports."""
import argparse
import cProfile
import pstats
from pathlib import Path

from utgen.assembler import RowAssembler
from utgen.db import ModuleIndexer, XmlDbReader, XmlDbWriter
from utgen.emit import ReportWriter, StubXmlWriter, ValueXmlWriter
from utgen.interp import Interpreter, Verifier
from utgen.mcdc import McdcGenerator
from utgen.model import Inventory
from utgen.solver import Solver


parser = argparse.ArgumentParser()
parser.add_argument("--rebuild", action="store_true")
parser.add_argument("--source-root", type=Path, default=Path(r"D:\SW\liauto_pdc\Application"))
parser.add_argument("--define", action="append", dest="defines")
parser.add_argument("--profile", action="store_true", help="print the top 50 cumulative profiling results")
args = parser.parse_args()

def regenerate():
    root = Path("PDM_UT")
    targets = (("PDM_MOS_1", "PDM_MOS_1.c"),)
    if args.rebuild:
        for module, filename in targets:
            source = args.source_root / "app_source" / "AppSWC" / "PDM" / filename
            if not source.is_file():
                raise SystemExit(f"Source not found: {source}")
            output = root / module
            print(f"{module}: indexing {source}", flush=True)
            analysis = ModuleIndexer({"defines": args.defines or ("__GHS__",)}).index(source, args.source_root)
            print(f"{module}: writing database ({len(analysis.functions)} functions)", flush=True)
            XmlDbWriter().write(analysis, output / f"db_{module}.xml")

    assembler = RowAssembler(Solver(), McdcGenerator(), include_boundary=False, include_fault=False)
    for database in sorted(root.glob("*/db_*.xml")):
        output = database.parent
        module = database.stem.removeprefix("db_")
        print(f"{module}: loading {database}", flush=True)
        for function in XmlDbReader().read(database).functions:
            if function.name == "PDM_MOS_Core0_SequentialClose_Handler" or function.name == "PDM_MOS_Core0_StuckCloseSlfDiag_HSD" or function.name == "PDM_MOS_Core0_StuckOpenSlfDiag" or function.name == "PDM_MOS_Core0_StuckCloseSlfDiag":
                print(f"skip {function.name}")
                continue
            print(f"{module}: generating {function.name}", flush=True)
            rows = assembler.assemble(function)
            Verifier(Interpreter(Inventory(function.leaves, function.stubs),
                                 execute_static_helpers=True)).check(function, rows)
            ValueXmlWriter().write(function, rows, output / f"value_{module}_{function.name}.xml")
            StubXmlWriter().write(function, output / f"stub_{module}_{function.name}.xml")
            ReportWriter().write(function, rows, output / f"report_{module}_{function.name}.md")
            print(f"{module}: {function.name}: {len(rows)} rows", flush=True)


if args.profile:
    profiler = cProfile.Profile()
    profiler.enable()
    regenerate()
    profiler.disable()
    print("\nTop 50 cumulative profile results:")
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(50)
else:
    regenerate()
