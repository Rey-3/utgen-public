"""TESSY value/stub XML and human-readable report emitters."""
from __future__ import annotations

from collections import Counter
from dataclasses import fields, is_dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from utgen.model import CallE, Direction, FunctionIR, TestRow


class ValueXmlWriter:
    """Write row-aligned injectable and observable function arguments."""

    def write(self, function: FunctionIR, rows: list[TestRow], path: str | Path) -> None:
        """Write a TESSY-compatible value XML file for ``function``."""
        root = ET.Element("function", name=function.name, file=_source(function))
        for leaf in function.leaves:
            attrs = {"name": leaf.name, "len": leaf.xml_len, "format": leaf.fmt,
                     "direction": leaf.direction.value, "pointer": _bool(leaf.is_pointer)}
            if leaf.direction in (Direction.IN, Direction.INOUT):
                values = []
                for row in rows:
                    values.append(_render(leaf, row.values.get(leaf.name, 0)))
                attrs["range"] = ",".join(values)
                attrs["testmethod"] = "decision"
            ET.SubElement(root, "argument", **attrs)
        _write_xml(root, path)


class StubXmlWriter:
    """Write the existing TESSY called-function and argument schema."""

    def write(self, function: FunctionIR, path: str | Path) -> None:
        """Write one stub XML file for ``function``."""
        root = ET.Element("function_analysis")
        called = ET.SubElement(root, "called_functions")
        for stub in function.stubs:
            element = ET.SubElement(called, "function", name=stub.name)
            if stub.ret_ctype is not None or stub.ret_struct:
                return_type = stub.ret_ctype.name if stub.ret_ctype is not None else stub.ret_struct
                ET.SubElement(element, "return", type=return_type, name=stub.name)
            arguments = ET.SubElement(element, "arguments")
            for type_name, name in stub.params:
                ET.SubElement(arguments, "argument", type=type_name, name=name)
        _write_xml(root, path)


class ReportWriter:
    """Write deterministic row traceability and diagnostic Markdown."""

    def write(self, function: FunctionIR, rows: list[TestRow], notes: list[str],
              path: str | Path) -> None:
        """Write a report containing every row, output, and diagnostic note."""
        lines = [f"# UTGen report: {function.name}", "",
                 f"- Function: `{function.name}`", f"- File: `{_source(function)}`",
                 f"- Test cases: {len(rows)}",
                 f"- Coverage: {len(function.decisions)} decisions; {_kinds(rows)}", "",
                 "| id | kind | description | expected outputs |",
                 "|---:|---|---|---|"]
        for index, row in enumerate(rows, 1):
            lines.append(f"| {index} | {_cell(row.kind)} | {_cell(row.description)} | "
                         f"{_cell(_expected(row))} |")
        grouped = _diagnostics(function, rows, notes)
        for title, items in grouped:
            lines.extend(("", f"## {title}"))
            if items:
                for item in items:
                    lines.append(f"- {_cell(item)}")
            else:
                lines.append("None.")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _source(function: FunctionIR) -> str:
    return str(getattr(function, "file", "") or getattr(function, "source_file", ""))


def _bool(value: bool) -> str:
    return str(value).lower()


def _render(leaf, value) -> str:
    if isinstance(value, str):
        return value
    for name, number in leaf.ctype.enum_values:
        if number == value:
            return name
    return str(value)


def _write_xml(root, path: str | Path) -> None:
    ET.indent(root, space="    ")
    payload = ET.tostring(root, encoding="utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'<?xml version="1.0" encoding="utf-8"?>\n' + payload + b"\n")


def _kinds(rows: list[TestRow]) -> str:
    counts = Counter(row.kind for row in rows)
    values = []
    for kind in sorted(counts):
        values.append(f"{kind}={counts[kind]}")
    return ", ".join(values) or "none"


def _expected(row: TestRow) -> str:
    values = []
    for name, value in row.expected.items():
        values.append(f"{name}={value}")
    return ", ".join(values) or "—"


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _diagnostics(function: FunctionIR, rows: list[TestRow], notes: list[str]) -> list[tuple[str, list[str]]]:
    infeasible = []
    uncontrollable = []
    manual = []
    for note in notes:
        lower = note.lower()
        if "infeasible" in lower:
            infeasible.append(note)
        elif "uncontrollable" in lower:
            uncontrollable.append(note)
        else:
            manual.append(note)
    if _has_null(rows):
        manual.append("NULL pointer fault rows require manual address setup (D3).")
    calls = Counter()
    _collect_calls(function.body, calls)
    for stub in function.stubs:
        if calls[stub.name] > 1:
            manual.append(f"Stub {stub.name} is called multiple times; one input is shared (E4).")
    return [("Infeasible pairs", infeasible), ("Uncontrollable conditions", uncontrollable),
            ("Manual actions", manual)]


def _has_null(rows: list[TestRow]) -> bool:
    for row in rows:
        for value in row.values.values():
            if value == "NULL":
                return True
    return False


def _collect_calls(value, counts: Counter) -> None:
    if isinstance(value, CallE):
        counts[value.name] += 1
    if is_dataclass(value):
        for item in fields(value):
            _collect_calls(getattr(value, item.name), counts)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _collect_calls(item, counts)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_calls(item, counts)
