"""TESSY value/stub XML and human-readable report emitters."""
from __future__ import annotations

from collections import Counter
from dataclasses import fields, is_dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from utgen.model import CallE, Direction, FunctionIR, TestRow


class ValueXmlWriter:
    """Write row-aligned injectable and observable function arguments."""

    def write(self, function: FunctionIR, rows: list[TestRow], path: str | Path,
              default_range: str | None = None) -> None:
        """Write a TESSY-compatible value XML file for ``function``."""
        root = ET.Element("function", name=function.name, file=_source(function))
        addresses = {leaf.address_of: leaf for leaf in function.leaves if leaf.address_of}
        for leaf in function.leaves:
            if leaf.address_of:
                continue
            attrs = {"name": leaf.name, "len": _validation_len(leaf), "format": leaf.fmt,
                     "direction": leaf.direction.value, "pointer": _bool(leaf.is_pointer)}
            address = _pointer_address(leaf.name, addresses)
            if leaf.direction in (Direction.IN, Direction.INOUT):
                if default_range is None:
                    values = []
                    for row in rows:
                        value = row.values.get(leaf.name, 0)
                        values.append("NULL" if address is not None and
                                      row.values.get(address.name, 1) in (0, "NULL") else
                                      _render(leaf, value))
                    attrs["range"] = ",".join(values)
                else:
                    attrs["range"] = default_range
                attrs["testmethod"] = "decision"
            ET.SubElement(root, "argument", **attrs)
        _write_xml(root, path)


def _pointer_address(name, addresses):
    for root, address in addresses.items():
        if name == root or name.startswith((root + ".", root + "[")):
            return address
    return None


class StubXmlWriter:
    """Write the TESSY called-function and argument schema."""

    def write(self, function: FunctionIR, path: str | Path) -> None:
        """Write one stub XML file for ``function``."""
        root = ET.Element("function_analysis", function_name=function.name,
                          file=_stub_path(_source(function)), line=str(function.line))
        called = ET.SubElement(root, "called_functions", count=str(len(function.stubs)))
        for stub in function.stubs:
            element = ET.SubElement(called, "function", name=stub.name,
                                    file=_stub_path(getattr(stub, "file", "")),
                                    line=str(getattr(stub, "line", "")))
            return_type = stub.ret_ctype.name if stub.ret_ctype is not None else stub.ret_struct or "void"
            return_element = ET.SubElement(element, "return")
            ET.SubElement(return_element, "type").text = return_type
            ET.SubElement(return_element, "name").text = f"tessy_{stub.name}"
            if stub.params:
                arguments = ET.SubElement(element, "arguments")
                for index, (type_name, param_name) in enumerate(stub.params):
                    argument = ET.SubElement(arguments, "argument", type=type_name,
                                             name=param_name)
                    ET.SubElement(argument, "type").text = type_name
                    ET.SubElement(argument, "name").text = f"tessy_{stub.name}__Arg{index}"
        _write_stub_xml(root, path)


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


def _stub_path(value: str) -> str:
    parts = str(value or "").replace("\\", "/").split("/")
    for index, part in enumerate(parts):
        if part.lower() == "app_source":
            return "/".join(parts[index:])
    return "/".join(parts)


def _write_stub_xml(root, path: str | Path) -> None:
    ET.indent(root, space="  ")
    payload = ET.tostring(root, encoding="utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<?xml version='1.0' encoding='utf-8'?>\n" + payload + b"\n")


def _bool(value: bool) -> str:
    return str(value).lower()


def _render(leaf, value) -> str:
    if isinstance(value, str):
        return value
    for name, number in leaf.ctype.enum_values:
        if number == value:
            return name
    return str(value)


def _validation_len(leaf) -> str:
    if leaf.ctype.signed or leaf.ctype.enum_values or leaf.fmt.startswith(("signed", "unsigned")):
        return leaf.xml_len
    try:
        return str(int(leaf.xml_len) + 1)
    except ValueError:
        return leaf.xml_len


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
