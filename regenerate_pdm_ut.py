"""Rebuild PDM databases from source, then regenerate UT XML and reports."""
import argparse
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
args = parser.parse_args()

root = Path("PDM_UT")
targets = (
    ("PDM_MOS_1", "PDM_MOS_1.c"),
)
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
        print(f"{module}: generating {function.name}", flush=True)
        rows, notes = assembler.assemble(function)
        notes.extend(Verifier(Interpreter(Inventory(function.leaves, function.stubs))).check(function, rows))
        ValueXmlWriter().write(function, rows, output / f"value_{module}_{function.name}.xml")
        StubXmlWriter().write(function, output / f"stub_{module}_{function.name}.xml")
        ReportWriter().write(function, rows, notes, output / f"report_{module}_{function.name}.md")
        print(f"{module}: {function.name}: {len(rows)} rows", flush=True)

