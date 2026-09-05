"""Secure, read-only normalization of TESSY 5.1 complete exports.

This module is deliberately only a workbook *adapter*.  It knows the TESSY
coordinates and wire spelling, but it does not know how rows are matched to
iteration state, how IDs are allocated, or how Merge A/B should behave.

The real TESSY export used by UTGen is an ``.xlsx`` workbook with the
following relevant sheets:

* ``General``      - identity and compatibility metadata;
* ``Properties``  - one record per testcase, beginning at row 5;
* ``Values``      - names on row 1, directions on row 2, rows beginning at 3;
* ``Stubs``       - headers on row 4 and records beginning at row 5.

``openpyxl`` is used only after the ZIP container has been checked.  Values
are represented with :class:`CellValue` so a missing
cell, a physically present empty cell, and a value such as ``0x00`` cannot be
silently conflated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import zipfile
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping
import xml.etree.ElementTree as ET

from openpyxl import load_workbook

# The limits are intentionally conservative for a TESSY workbook while still
# leaving ample room for a future export containing more test rows.  They also
# prevent a ZIP bomb or a workbook dimension from turning a linear import into
# an unbounded allocation.
MAX_ARCHIVE_ENTRIES = 4096
MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000

_DIRECTION_RE = re.compile(r"^(i|o)(?:\((.+)\))?$")
_CASE_LABEL_RE = re.compile(r"^tc([1-9][0-9]*)\.([0-9]+)$")
_ARRAY_INDEX_RE = re.compile(r"\[([0-9]+)\]")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TESSY_51_RE = re.compile(r"^5\.1(?:\.[0-9]+)?$")


@dataclass(frozen=True)
class CellValue:
    """A workbook cell retaining both its lexical and semantic value."""

    kind: str
    raw_text: str | None
    semantic_value: object | None

    def __post_init__(self) -> None:
        if self.kind not in {"value", "explicit_empty", "missing"}:
            raise ValueError(f"unsupported cell kind: {self.kind!r}")
        if self.raw_text is not None and not isinstance(self.raw_text, str):
            raise TypeError("raw_text must be a string or None")


@dataclass(frozen=True)
class ExcelColumnKey:
    """Immutable identity for one Values column and array element."""

    canonical_name: str
    channel: str
    element_path: tuple[int, ...]
    occurrence: int

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_name, str) or not self.canonical_name:
            raise ValueError("canonical_name must be a non-empty string")
        if self.channel not in {"input", "output"}:
            raise ValueError(f"unsupported column channel: {self.channel!r}")
        if type(self.element_path) is not tuple or any(
            type(index) is not int or index < 0 for index in self.element_path
        ):
            raise TypeError("element_path must be a tuple of non-negative integers")
        if type(self.occurrence) is not int or self.occurrence < 0:
            raise TypeError("occurrence must be a non-negative integer")


class WorkbookAdapterError(ValueError):
    """Base class for a rejected or unreadable workbook.

    ``code`` is stable enough for callers to turn a failure into a conflict
    report without matching human-readable error text.  ``sheet`` and
    ``coordinate`` are diagnostic only.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        sheet: str | None = None,
        coordinate: str | None = None,
    ) -> None:
        self.code = code
        self.sheet = sheet
        self.coordinate = coordinate
        location = ""
        if sheet is not None:
            location = f" [{sheet}"
            if coordinate is not None:
                location += f"!{coordinate}"
            location += "]"
        super().__init__(f"{code}{location}: {message}")


class WorkbookSecurityError(WorkbookAdapterError):
    """Raised when the XLSX container is unsafe to process."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("UNSAFE_XLSX", message, **kwargs)


class WorkbookValidationError(WorkbookAdapterError):
    """Raised when the workbook violates the selected TESSY dialect."""


class MissingSheetError(WorkbookValidationError):
    """Raised when a complete export lacks a mandatory sheet."""

    def __init__(self, sheet_name: str) -> None:
        super().__init__("MISSING_REQUIRED_SHEET", f"required sheet {sheet_name!r} is missing")


class UnsupportedFormulaError(WorkbookValidationError):
    """Raised instead of evaluating a formula in an active data region."""

    def __init__(self, sheet: str, coordinate: str) -> None:
        super().__init__(
            "UNSUPPORTED_FORMULA_CELL",
            "formulas are not supported in active TESSY data regions",
            sheet=sheet,
            coordinate=coordinate,
        )


class GeneralIdentityError(WorkbookValidationError):
    """Raised when General does not identify the requested Test Object."""

    def __init__(self, field: str, expected: str, actual: str) -> None:
        super().__init__(
            "GENERAL_IDENTITY_MISMATCH",
            f"General {field} does not match expected {expected!r}; got {actual!r}",
            sheet="General",
        )


@dataclass(frozen=True)
class WorkbookMeta:
    """Identity and non-identity metadata decoded from ``General``."""

    project: str
    module: str
    testobject: str
    tessy_version: str
    export_date: str | None = None
    pdb_file: str | None = None
    dialect: str = "tessy_5_1"

    @property
    def project_name(self) -> str:
        return self.project

    @property
    def module_name(self) -> str:
        return self.module

    @property
    def test_object(self) -> str:
        return self.testobject

    @property
    def test_object_name(self) -> str:
        return self.testobject

    @property
    def tessy(self) -> str:
        return self.tessy_version


@dataclass(frozen=True)
class ExportColumn:
    """One physical Values column, normalized without merge semantics."""

    key: ExcelColumnKey
    name: str
    direction: str
    channel: str
    raw_direction: str
    raw_type_hint: str | None
    element_path: tuple[int, ...]
    base_name: str
    source_column: int

    @property
    def canonical_name(self) -> str:
        return self.base_name

    @property
    def type_hint(self) -> str | None:
        return self.raw_type_hint

    @property
    def column(self) -> int:
        return self.source_column

    @property
    def direction_token(self) -> str:
        return self.raw_direction


@dataclass
class ExportCase:
    """One Values row joined to its Properties record by testcase number."""

    export_case_number: int
    export_step: int
    row_label: str
    case_name: str | None
    external_test_case_id: str | None
    description_raw: str
    input_cells: dict[ExcelColumnKey, CellValue] = field(default_factory=dict)
    output_cells: dict[ExcelColumnKey, CellValue] = field(default_factory=dict)
    properties_test_step: int | None = None
    properties_row: int | None = None
    values_row: int | None = None
    description_type: str | None = None
    overview: str | None = None

    @property
    def test_case(self) -> int:
        return self.export_case_number

    @property
    def test_step(self) -> int:
        return self.export_step

    @property
    def name(self) -> str | None:
        return self.case_name

    @property
    def description(self) -> str:
        return self.description_raw


@dataclass(frozen=True)
class ExportStub:
    """One module-level Stubs row; body text is intentionally opaque."""

    name: str
    test_case: int
    test_step: int
    body_raw: str
    source_row: int | None = None

    @property
    def stub_name(self) -> str:
        return self.name

    @property
    def body(self) -> str:
        return self.body_raw


@dataclass
class WorkbookSnapshot:
    """Complete normalized workbook input for a later merge stage."""

    meta: WorkbookMeta
    cases: list[ExportCase]
    columns: list[ExportColumn]
    stubs: list[ExportStub]
    completeness: str
    workbook_hash: str

    @property
    def hash(self) -> str:
        return self.workbook_hash


@dataclass(frozen=True)
class PhysicalWorksheet:
    """Public, immutable view of the cells physically present in one sheet.

    ``openpyxl.Worksheet._cells`` is an implementation detail and must not
    leak into parser or scan code.  The adapter constructs this project-owned
    index once at the XLSX boundary.  The mapping includes styled/explicitly
    empty cells because coordinates come from worksheet XML, not from a value
    filter.
    """

    title: str
    physical_cells: Mapping[tuple[int, int], Any]

    def __post_init__(self) -> None:
        if type(self.title) is not str or not self.title:
            raise ValueError("physical worksheet title must be non-empty")
        normalized: dict[tuple[int, int], Any] = {}
        for key, cell in self.physical_cells.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or type(key[0]) is not int
                or type(key[1]) is not int
                or key[0] < 1
                or key[1] < 1
            ):
                raise ValueError("physical cell coordinates must be positive (row, column) tuples")
            normalized[(key[0], key[1])] = cell
        object.__setattr__(self, "physical_cells", MappingProxyType(normalized))

    @property
    def used_cells(self) -> Mapping[tuple[int, int], Any]:
        """Alias used by integrations that prefer a descriptive name."""

        return self.physical_cells

    def iter_used_cells(self) -> Iterator[Any]:
        """Yield exactly the physically instantiated cells, once each."""

        return iter(self.physical_cells.values())

    iter_physical_cells = iter_used_cells

    def cell_at(self, row: int, column: int) -> Any | None:
        return self.physical_cells.get((row, column))


@dataclass(frozen=True)
class PhysicalWorkbook:
    """Public workbook contract consumed by the parser and scan utilities."""

    worksheets: tuple[PhysicalWorksheet, ...]

    def __post_init__(self) -> None:
        if type(self.worksheets) is not tuple:
            raise TypeError("physical workbook worksheets must be a tuple")
        if not all(isinstance(item, PhysicalWorksheet) for item in self.worksheets):
            raise TypeError("physical workbook worksheets must contain PhysicalWorksheet records")
        names = [item.title for item in self.worksheets]
        if len(names) != len(set(names)):
            raise ValueError("physical workbook sheet names must be unique")

    @property
    def sheetnames(self) -> tuple[str, ...]:
        return tuple(item.title for item in self.worksheets)

    def __getitem__(self, title: str) -> PhysicalWorksheet:
        for sheet in self.worksheets:
            if sheet.title == title:
                return sheet
        raise KeyError(title)

    def iter_used_cells(self) -> Iterator[tuple[str, Any]]:
        for sheet in self.worksheets:
            for cell in sheet.iter_used_cells():
                yield sheet.title, cell


_CELL_REFERENCE_RE = re.compile(r"^\$?([A-Z]+)\$?([1-9][0-9]*)$")


def _column_number(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _cell_reference(value: object) -> tuple[int, int]:
    if not isinstance(value, str):
        raise WorkbookValidationError("INVALID_CELL_REFERENCE", "worksheet cell has no coordinate")
    match = _CELL_REFERENCE_RE.fullmatch(value)
    if match is None:
        raise WorkbookValidationError("INVALID_CELL_REFERENCE", f"invalid worksheet cell coordinate {value!r}")
    return int(match.group(2), 10), _column_number(match.group(1))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _workbook_sheet_parts(archive: zipfile.ZipFile) -> dict[str, str]:
    """Resolve workbook sheet names to worksheet XML parts without internals."""

    try:
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError, OSError) as exc:
        raise WorkbookValidationError("INVALID_XLSX", "workbook sheet metadata is not readable") from exc

    relationships = {
        element.attrib.get("Id"): element.attrib.get("Target")
        for element in rels_root
        if _local_name(element.tag) == "Relationship"
        and element.attrib.get("Id")
        and element.attrib.get("Target")
    }
    result: dict[str, str] = {}
    for element in workbook_root.iter():
        if _local_name(element.tag) != "sheet":
            continue
        name = element.attrib.get("name")
        relationship_id = next(
            (
                value
                for key, value in element.attrib.items()
                if key.rsplit("}", 1)[-1] == "id"
            ),
            None,
        )
        target = relationships.get(relationship_id)
        if not name or not target:
            raise WorkbookValidationError("INVALID_XLSX", "workbook sheet relationship is incomplete")
        if target.startswith("/"):
            part = target.lstrip("/")
        else:
            part = posixpath.normpath(posixpath.join("xl", target))
        if part not in archive.namelist() or not part.startswith("xl/"):
            raise WorkbookValidationError("INVALID_XLSX", f"worksheet part {part!r} is missing or unsafe")
        result[name] = part
    return result


def _physicalize_loaded_workbook(workbook: Any, raw: bytes) -> PhysicalWorkbook:
    """Build the public physical-cell index from raw worksheet XML.

    Coordinates are enumerated from ``<c>`` elements in the XLSX package,
    which is linear in physically represented cells.  ``worksheet[coordinate]``
    is the public openpyxl access used only to retrieve the decoded Cell value
    and style for an already-proven physical coordinate.
    """

    try:
        with zipfile.ZipFile(BytesIO(raw), "r") as archive:
            parts = _workbook_sheet_parts(archive)
            physical_sheets: list[PhysicalWorksheet] = []
            for worksheet in getattr(workbook, "worksheets", ()):
                title = str(getattr(worksheet, "title", ""))
                part = parts.get(title)
                if part is None:
                    raise WorkbookValidationError(
                        "INVALID_XLSX",
                        f"worksheet {title!r} has no workbook XML relationship",
                    )
                cells: dict[tuple[int, int], Any] = {}
                try:
                    with archive.open(part, "r") as stream:
                        for _event, element in ET.iterparse(stream, events=("end",)):
                            local_name = _local_name(element.tag)
                            if local_name == "row":
                                element.clear()
                                continue
                            if local_name != "c":
                                continue
                            reference = element.attrib.get("r")
                            key = _cell_reference(reference)
                            if key in cells:
                                raise WorkbookValidationError(
                                    "DUPLICATE_PHYSICAL_CELL",
                                    f"worksheet {title!r} contains duplicate cell {reference!r}",
                                )
                            try:
                                cells[key] = worksheet[reference]
                            except Exception as exc:
                                raise WorkbookValidationError(
                                    "INVALID_CELL_REFERENCE",
                                    f"worksheet {title!r} cell {reference!r} cannot be decoded",
                                ) from exc
                            element.clear()
                except WorkbookValidationError:
                    raise
                except (ET.ParseError, KeyError, OSError) as exc:
                    raise WorkbookValidationError(
                        "INVALID_XLSX",
                        f"worksheet {title!r} XML is not readable",
                    ) from exc
                physical_sheets.append(PhysicalWorksheet(title, cells))
            return PhysicalWorkbook(tuple(physical_sheets))
    except WorkbookValidationError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise WorkbookValidationError("INVALID_XLSX", "cannot build physical workbook index") from exc


def physicalize_workbook(workbook: Any, *, raw: bytes | bytearray | None = None) -> PhysicalWorkbook:
    """Return the project-owned physical-cell view for a workbook.

    The normal XLSX adapter path supplies ``raw`` and never reads an
    openpyxl private map.  Direct openpyxl integrations are supported as a
    compatibility boundary: their private map is copied once into the public
    index, after which all consumers use :class:`PhysicalWorkbook`.
    """

    if isinstance(workbook, PhysicalWorkbook):
        return workbook
    if raw is not None:
        return _physicalize_loaded_workbook(workbook, bytes(raw))
    worksheets = getattr(workbook, "worksheets", None)
    if worksheets is None:
        raise WorkbookValidationError(
            "UNSUPPORTED_WORKBOOK_STORAGE",
            "workbook has no worksheet collection or physical-cell protocol",
        )
    return PhysicalWorkbook(tuple(physicalize_worksheet(item, index=index) for index, item in enumerate(worksheets)))


def physicalize_worksheet(worksheet: Any, *, index: int = 0) -> PhysicalWorksheet:
    """Copy one direct openpyxl worksheet into the public physical contract."""

    if isinstance(worksheet, PhysicalWorksheet):
        return worksheet
    cells = getattr(worksheet, "_cells", None)
    if not isinstance(cells, Mapping):
        raise WorkbookValidationError(
            "UNSUPPORTED_WORKBOOK_STORAGE",
            "direct worksheet is neither a PhysicalWorksheet nor an openpyxl worksheet with a physical map",
        )
    return PhysicalWorksheet(str(getattr(worksheet, "title", index)), dict(cells))


@dataclass(frozen=True)
class DirectionToken:
    """Parsed Values direction/type cell."""

    direction: str
    channel: str
    raw: str
    raw_type_hint: str | None


@dataclass(frozen=True)
class _PropertyRecord:
    case_number: int
    test_step: int
    case_name: str | None
    description_raw: str
    external_test_case_id: str | None
    description_type: str | None
    overview: str | None
    source_row: int


def _error(code: str, message: str, *, sheet: str | None = None, coordinate: str | None = None) -> None:
    raise WorkbookValidationError(code, message, sheet=sheet, coordinate=coordinate)


def _normalize_label(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def _module_basename(value: str) -> str:
    normalized = value.replace("\\", "/")
    return normalized.rsplit("/", 1)[-1]


def _cell_map(ws: Any) -> Mapping[tuple[int, int], Any]:
    # Parser code consumes only the project-owned physical-cell protocol.  The
    # normal XLSX loading boundary creates this view before parsing; callers
    # embedding an alternative worksheet can provide the same public mapping
    # or iterator without depending on openpyxl internals.
    cells = getattr(ws, "physical_cells", None)
    if isinstance(cells, Mapping):
        return cells
    iterator = getattr(ws, "iter_used_cells", None)
    if callable(iterator):
        result: dict[tuple[int, int], Any] = {}
        for cell in iterator():
            row = getattr(cell, "row", None)
            column = getattr(cell, "column", None)
            if type(row) is not int or type(column) is not int or row < 1 or column < 1:
                raise WorkbookValidationError(
                    "UNSUPPORTED_WORKBOOK_STORAGE",
                    "physical-cell iterator yielded a cell without a valid row/column",
                )
            key = (row, column)
            if key in result:
                raise WorkbookValidationError(
                    "DUPLICATE_PHYSICAL_CELL",
                    f"physical-cell iterator yielded duplicate coordinate {key!r}",
                )
            result[key] = cell
        return result
    # Compatibility boundary for callers that pass one ordinary openpyxl
    # worksheet directly to this low-level helper.  Normal XLSX parsing has
    # already converted the workbook through ``physicalize_workbook``; this
    # fallback copies the private map once and immediately returns the public
    # project-owned mapping, never exposing it to parser logic.
    if isinstance(getattr(ws, "_cells", None), Mapping):
        return physicalize_worksheet(ws).physical_cells
    raise WorkbookValidationError(
        "UNSUPPORTED_WORKBOOK_STORAGE",
        "worksheet does not expose the project physical used-cell map protocol",
    )


def _cell_at(ws: Any, row: int, column: int) -> Any | None:
    # Resolve through the same physical-cell map used by the complete-export
    # scanner.  Falling back to ``iter_rows`` (or ``Worksheet.cell``) would
    # materialize absent cells on sparse worksheets and make a workbook with
    # no physical storage indistinguishable from a valid empty cell.  The
    # adapter therefore fails closed when ``_cells`` is unavailable.
    return _cell_map(ws).get((row, column))


def _is_formula(cell: Any | None) -> bool:
    if cell is None:
        return False
    if getattr(cell, "data_type", None) == "f":
        return True
    value = getattr(cell, "value", None)
    return isinstance(value, str) and value.startswith("=") and getattr(cell, "data_type", None) != "s"


def _cell_value(cell: Any | None) -> CellValue:
    """Decode one cell without evaluating formulas or coercing text."""

    if cell is None:
        return CellValue("missing", None, None)
    value = getattr(cell, "value", None)
    if value is None or value == "":
        # openpyxl resolves a shared-string cell containing ``<t/>`` to the
        # empty string.  It also gives us an empty value for an inline empty
        # cell.  Both are physically present values, whereas ``cell is None``
        # above is absent from the worksheet XML.
        return CellValue("explicit_empty", "", None)
    if isinstance(value, datetime | date | time):
        raw = value.isoformat()
        return CellValue("value", raw, raw)
    if isinstance(value, bool):
        return CellValue("value", "TRUE" if value else "FALSE", value)
    if isinstance(value, str):
        return CellValue("value", value, value)
    if isinstance(value, int | float):
        return CellValue("value", str(value), value)
    raw = str(value)
    return CellValue("value", raw, raw)


def _text_value(cell: Any | None, *, empty: str | None = None) -> str | None:
    decoded = _cell_value(cell)
    if decoded.kind == "missing":
        return empty
    if decoded.kind == "explicit_empty":
        return ""
    return decoded.raw_text


def _require_text(
    ws: Any,
    cell: Any | None,
    *,
    sheet: str,
    field: str,
    coordinate: str | None = None,
) -> str:
    text = _text_value(cell)
    if text is None or not text.strip():
        _error("MISSING_REQUIRED_VALUE", f"{field} must be non-empty", sheet=sheet, coordinate=coordinate)
    return text


def _parse_integer(
    cell: Any | None,
    *,
    sheet: str,
    field: str,
    row: int,
    allow_zero: bool = True,
) -> int:
    text = _text_value(cell)
    if text is None or not text.strip():
        _error("INVALID_INTEGER_CELL", f"{field} must be an integer", sheet=sheet, coordinate=f"{field}{row}")
    stripped = text.strip()
    if not re.fullmatch(r"[0-9]+", stripped):
        _error("INVALID_INTEGER_CELL", f"{field} must be an unsigned integer, got {text!r}", sheet=sheet)
    number = int(stripped, 10)
    if not allow_zero and number == 0:
        _error("INVALID_INTEGER_CELL", f"{field} must be greater than zero", sheet=sheet)
    return number


def _description_parts(description: str) -> tuple[str | None, str | None, str | None]:
    """Extract optional well-known fields while retaining raw Description."""

    external_id: str | None = None
    description_type: str | None = None
    overview: str | None = None
    for line in description.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = _normalize_label(key)
        value = value.strip()
        if normalized == "test case id":
            external_id = value or None
        elif normalized == "test case type":
            description_type = value or None
        elif normalized == "overview":
            overview = value or None
    return external_id, description_type, overview


def parse_direction_token(value: Any) -> DirectionToken:
    """Parse exactly the TESSY ``i``, ``o``, ``i(type)``, ``o(type)`` grammar."""

    if not isinstance(value, str):
        raise WorkbookValidationError("INVALID_DIRECTION", f"direction token must be text, got {type(value).__name__}")
    match = _DIRECTION_RE.fullmatch(value)
    if match is None:
        raise WorkbookValidationError(
            "INVALID_DIRECTION",
            f"invalid Values direction/type token {value!r}; expected i, o, i(type), or o(type)",
        )
    direction = match.group(1)
    return DirectionToken(
        direction="in" if direction == "i" else "out",
        channel="input" if direction == "i" else "output",
        raw=value,
        raw_type_hint=match.group(2),
    )


# Short alias used by a few callers and useful in contract tests.
parse_direction = parse_direction_token


def split_array_column_name(name: str) -> tuple[str, tuple[int, ...]]:
    """Return the unindexed base name and indices in declaration order."""

    if not isinstance(name, str) or not name.strip():
        raise WorkbookValidationError("MISSING_COLUMN_NAME", "Values variable name must be non-empty text")
    paths = tuple(int(item) for item in _ARRAY_INDEX_RE.findall(name))
    base = _ARRAY_INDEX_RE.sub("", name)
    if not base:
        raise WorkbookValidationError("INVALID_COLUMN_NAME", f"Values variable name {name!r} has no base")
    return base, paths


def _active_formula_check(ws: Any, *, sheet: str, min_row: int) -> None:
    for (row, column), cell in _cell_map(ws).items():
        if row >= min_row and _is_formula(cell):
            raise UnsupportedFormulaError(sheet, cell.coordinate)


def _header_map(ws: Any, *, sheet: str, row: int, required: Iterable[str]) -> dict[str, int]:
    cells = _cell_map(ws)
    seen: dict[str, int] = {}
    header_columns = sorted({column for (cell_row, column) in cells if cell_row == row})
    for column in header_columns:
        cell = cells.get((row, column))
        text = _text_value(cell)
        if text is None or not text.strip():
            continue
        label = _normalize_label(text)
        if label in seen:
            _error("DUPLICATE_HEADER", f"duplicate header {text!r}", sheet=sheet, coordinate=cell.coordinate)
        seen[label] = column
    for name in required:
        if _normalize_label(name) not in seen:
            _error("MISSING_REQUIRED_HEADER", f"header {name!r} is missing", sheet=sheet)
    return seen


def _row_has_payload(cells: Mapping[tuple[int, int], Any], row: int, columns: Iterable[int]) -> bool:
    for column in columns:
        cell = cells.get((row, column))
        if cell is None:
            continue
        value = getattr(cell, "value", None)
        if value is not None:
            # Empty shared strings are still physically present payload.
            return True
        if getattr(cell, "data_type", None) == "f":
            return True
    return False


class TessyWorkbookAdapter:
    """Reader for complete TESSY 5.1.x workbooks."""

    dialect = "tessy_5_1"
    mandatory_sheets = ("General", "Properties", "Values", "Stubs")

    def __init__(
        self,
        *,
        expected_module: str | os.PathLike[str] | None = None,
        expected_testobject: str | None = None,
        expected_test_object: str | None = None,
        expected_project: str | None = None,
    ) -> None:
        if expected_testobject is not None and expected_test_object is not None:
            if expected_testobject != expected_test_object:
                raise ValueError("expected_testobject and expected_test_object disagree")
        self.expected_module = expected_module
        self.expected_testobject = expected_testobject if expected_testobject is not None else expected_test_object
        self.expected_project = expected_project

    def read_complete_export(
        self,
        path: str | os.PathLike[str] | Path | bytes | bytearray,
        *,
        expected_module: str | os.PathLike[str] | None = None,
        expected_testobject: str | None = None,
        expected_test_object: str | None = None,
        expected_project: str | None = None,
    ) -> WorkbookSnapshot:
        """Read and validate a complete TESSY export.

        ``expected_module`` and ``expected_testobject`` are optional because
        bootstrap can inspect a workbook before it knows the target.  When
        supplied, they are an explicit identity guard; the adapter never
        invents a project/configuration value.
        """

        if expected_testobject is not None and expected_test_object is not None:
            if expected_testobject != expected_test_object:
                raise ValueError("expected_testobject and expected_test_object disagree")
        if expected_testobject is None:
            expected_testobject = expected_test_object
        if expected_module is None:
            expected_module = self.expected_module
        if expected_testobject is None:
            expected_testobject = self.expected_testobject
        if expected_project is None:
            expected_project = self.expected_project

        raw = _read_xlsx_bytes(path)
        _validate_xlsx_container(raw)
        stream = BytesIO(raw)
        try:
            try:
                workbook = load_workbook(
                    stream,
                    read_only=False,
                    data_only=False,
                    keep_links=False,
                )
            except Exception as exc:
                raise WorkbookValidationError("INVALID_XLSX", "cannot decode XLSX workbook") from exc
            try:
                physical_workbook = physicalize_workbook(workbook, raw=raw)
                self._require_sheets(physical_workbook)
                meta = self._parse_general(
                    physical_workbook["General"],
                    expected_module=expected_module,
                    expected_testobject=expected_testobject,
                    expected_project=expected_project,
                )
                properties = self._parse_properties(physical_workbook["Properties"])
                columns, value_rows = self._parse_values(physical_workbook["Values"])
                stubs = self._parse_stubs(physical_workbook["Stubs"])
                self._validate_cross_sheet(properties, value_rows, columns, stubs)
                cases = self._join_cases(properties, value_rows)
            finally:
                workbook.close()
        finally:
            stream.close()
        return WorkbookSnapshot(
            meta=meta,
            cases=cases,
            columns=columns,
            stubs=stubs,
            completeness="complete",
            workbook_hash=sha256(raw).hexdigest(),
        )

    # Common adapter spellings.  They all retain complete-export semantics.
    def read(self, path: str | os.PathLike[str] | Path | bytes | bytearray, **kwargs: Any) -> WorkbookSnapshot:
        return self.read_complete_export(path, **kwargs)

    def parse(self, path: str | os.PathLike[str] | Path | bytes | bytearray, **kwargs: Any) -> WorkbookSnapshot:
        return self.read_complete_export(path, **kwargs)

    def _require_sheets(self, workbook: Any) -> None:
        available = set(workbook.sheetnames)
        for sheet_name in self.mandatory_sheets:
            if sheet_name not in available:
                raise MissingSheetError(sheet_name)

    def _parse_general(
        self,
        ws: Any,
        *,
        expected_module: str | os.PathLike[str] | None,
        expected_testobject: str | None,
        expected_project: str | None,
    ) -> WorkbookMeta:
        cells = _cell_map(ws)
        labels: dict[str, Any] = {}
        for (row, column), cell in cells.items():
            # General is a label/value sheet.  TESSY uses column A/B, but
            # scanning all physical cells keeps the adapter tolerant of a
            # harmless column insertion while remaining linear.
            text = _text_value(cell)
            if text is None or not text.strip():
                continue
            label = _normalize_label(text)
            if column != 1:
                continue
            if label in labels:
                _error("DUPLICATE_GENERAL_LABEL", f"duplicate General label {text!r}", sheet="General")
            labels[label] = (row, column)

        required = {
            "project": "project",
            "module": "module",
            "testobject": "testobject",
            "tessy version": "tessy_version",
        }
        values: dict[str, str] = {}
        for label, field_name in required.items():
            location = labels.get(label)
            if location is None:
                _error("MISSING_GENERAL_FIELD", f"General field {label!r} is missing", sheet="General")
            row, label_column = location
            value_cell = _cell_at(ws, row, 2)
            values[field_name] = _require_text(
                ws,
                value_cell,
                sheet="General",
                field=label,
                coordinate=f"B{row}",
            )

        module = _module_basename(values["module"])
        if not module:
            _error("MISSING_GENERAL_FIELD", "General Module must identify a source file", sheet="General")
        version = values["tessy_version"]
        if not _TESSY_51_RE.fullmatch(version):
            _error("UNSUPPORTED_TESSY_DIALECT", f"unsupported TESSY version {version!r}", sheet="General")

        export_date = None
        export_label = labels.get("export date")
        if export_label is not None:
            export_date = _text_value(_cell_at(ws, export_label[0], 2))
            if export_date == "":
                export_date = None
            if export_date is not None and not _DATE_RE.fullmatch(export_date):
                _error("INVALID_EXPORT_DATE", f"Export Date must be YYYY-MM-DD, got {export_date!r}", sheet="General")

        pdb_file = None
        pdb_label = labels.get("pdb file")
        if pdb_label is not None:
            pdb_file = _text_value(_cell_at(ws, pdb_label[0], 2))
            if pdb_file == "":
                pdb_file = None

        meta = WorkbookMeta(
            project=values["project"],
            module=module,
            testobject=values["testobject"],
            tessy_version=version,
            export_date=export_date,
            pdb_file=pdb_file,
            dialect=self.dialect,
        )

        if expected_module is not None:
            expected_module_text = _module_basename(os.fspath(expected_module))
            if meta.module != expected_module_text:
                raise GeneralIdentityError("Module", expected_module_text, meta.module)
        if expected_testobject is not None and meta.testobject != expected_testobject:
            raise GeneralIdentityError("Testobject", expected_testobject, meta.testobject)
        if expected_project is not None and meta.project != expected_project:
            raise GeneralIdentityError("Project", expected_project, meta.project)
        return meta

    def _parse_properties(self, ws: Any) -> dict[int, _PropertyRecord]:
        _active_formula_check(ws, sheet="Properties", min_row=4)
        headers = _header_map(
            ws,
            sheet="Properties",
            row=4,
            required=("Test Case", "Test Step", "Name", "Description"),
        )
        cells = _cell_map(ws)
        data_columns = tuple(headers.values())
        candidate_rows = sorted(
            {
                row
                for (row, _column), cell in cells.items()
                if row >= 5 and getattr(cell, "value", None) is not None
            }
        )
        records: dict[int, _PropertyRecord] = {}
        for row in candidate_rows:
            if not _row_has_payload(cells, row, data_columns):
                continue
            case_number = _parse_integer(
                cells.get((row, headers[_normalize_label("Test Case")])),
                sheet="Properties",
                field="Test Case",
                row=row,
                allow_zero=False,
            )
            test_step = _parse_integer(
                cells.get((row, headers[_normalize_label("Test Step")])),
                sheet="Properties",
                field="Test Step",
                row=row,
            )
            if case_number in records:
                _error("DUPLICATE_PROPERTY_CASE", f"duplicate Properties Test Case {case_number}", sheet="Properties")

            name_text = _text_value(cells.get((row, headers[_normalize_label("Name")])))
            case_name = name_text if name_text and name_text.strip() else None

            description = _text_value(cells.get((row, headers[_normalize_label("Description")])))
            if description is None:
                description = ""
            external_id, description_type, overview = _description_parts(description)
            records[case_number] = _PropertyRecord(
                case_number=case_number,
                test_step=test_step,
                case_name=case_name,
                description_raw=description,
                external_test_case_id=external_id,
                description_type=description_type,
                overview=overview,
                source_row=row,
            )
        return records

    def _parse_values(
        self,
        ws: Any,
    ) -> tuple[
        list[ExportColumn],
        list[tuple[int, int, str, dict[ExcelColumnKey, CellValue], dict[ExcelColumnKey, CellValue], int]],
    ]:
        _active_formula_check(ws, sheet="Values", min_row=1)
        cells = _cell_map(ws)
        columns: list[ExportColumn] = []
        seen_keys: set[tuple[str, str, tuple[int, ...]]] = set()
        active_column_numbers = sorted(
            {
                column
                for (row, column), cell in cells.items()
                if column >= 2
                and getattr(cell, "value", None) is not None
            }
        )

        for column in active_column_numbers:
            name_cell = cells.get((1, column))
            direction_cell = cells.get((2, column))
            name = _text_value(name_cell)
            if name is None or not name.strip():
                _error("MISSING_COLUMN_NAME", f"active Values column {column} has no variable name", sheet="Values")
            raw_direction = _text_value(direction_cell)
            if raw_direction is None or not raw_direction.strip():
                _error("INVALID_DIRECTION", f"Values column {name!r} has no direction token", sheet="Values")
            try:
                token = parse_direction_token(raw_direction)
            except WorkbookAdapterError as exc:
                raise WorkbookValidationError(exc.code, str(exc), sheet="Values", coordinate=direction_cell.coordinate if direction_cell else None) from exc
            base_name, element_path = split_array_column_name(name)
            key_tuple = (base_name, token.channel, element_path)
            if key_tuple in seen_keys:
                _error(
                    "DUPLICATE_ARGUMENT_COLUMN",
                    f"same-name same-channel Values column {name!r} occurs more than once",
                    sheet="Values",
                    coordinate=name_cell.coordinate if name_cell else None,
                )
            seen_keys.add(key_tuple)
            key = ExcelColumnKey(base_name, token.channel, element_path, 0)
            columns.append(
                ExportColumn(
                    key=key,
                    name=name,
                    direction=token.direction,
                    channel=token.channel,
                    raw_direction=token.raw,
                    raw_type_hint=token.raw_type_hint,
                    element_path=element_path,
                    base_name=base_name,
                    source_column=column,
                )
            )

        self._validate_array_structure(columns)

        rows: list[
            tuple[int, int, str, dict[ExcelColumnKey, CellValue], dict[ExcelColumnKey, CellValue], int]
        ] = []
        seen_row_labels: set[str] = set()
        candidate_rows = sorted(
            {
                row
                for (row, _column), cell in cells.items()
                if row >= 3 and getattr(cell, "value", None) is not None
            }
        )
        active_cols = tuple(active_column_numbers)
        for row in candidate_rows:
            label_cell = cells.get((row, 1))
            label = _text_value(label_cell)
            has_payload = _row_has_payload(cells, row, active_cols) or label is not None
            if not has_payload:
                continue
            if label is None or not label.strip():
                _error("INVALID_VALUES_ROW_LABEL", "Values testcase row has no tcN.step label", sheet="Values")
            match = _CASE_LABEL_RE.fullmatch(label)
            if match is None:
                _error("INVALID_VALUES_ROW_LABEL", f"invalid Values row label {label!r}", sheet="Values", coordinate=label_cell.coordinate if label_cell else None)
            case_number = int(match.group(1), 10)
            export_step = int(match.group(2), 10)
            if label in seen_row_labels:
                _error("DUPLICATE_VALUES_ROW", f"duplicate Values row label {label!r}", sheet="Values")
            seen_row_labels.add(label)
            inputs: dict[ExcelColumnKey, CellValue] = {}
            outputs: dict[ExcelColumnKey, CellValue] = {}
            for column in columns:
                value = _cell_value(cells.get((row, column.source_column)))
                target = inputs if column.channel == "input" else outputs
                target[column.key] = value
            rows.append((case_number, export_step, label, inputs, outputs, row))
        return columns, rows

    def _validate_array_structure(self, columns: list[ExportColumn]) -> None:
        groups: dict[tuple[str, str], list[tuple[int, ...]]] = {}
        for column in columns:
            groups.setdefault((column.base_name, column.channel), []).append(column.element_path)
        for (base_name, channel), paths in groups.items():
            has_indexed = any(path for path in paths)
            if not has_indexed:
                continue
            if any(not path for path in paths):
                _error(
                    "INCOMPLETE_ARRAY",
                    f"array column group {base_name!r}/{channel} mixes scalar and indexed columns",
                    sheet="Values",
                )
            depth = len(paths[0])
            if any(len(path) != depth for path in paths):
                _error("INCOMPLETE_ARRAY", f"array column group {base_name!r}/{channel} has inconsistent dimensions", sheet="Values")
            expected_sets = [set(range(max(path[index] for path in paths) + 1)) for index in range(depth)]
            observed = set(paths)
            for path_index, expected in enumerate(expected_sets):
                if {path[path_index] for path in paths} != expected:
                    _error(
                        "INCOMPLETE_ARRAY",
                        f"array column group {base_name!r}/{channel} is missing index {path_index}",
                        sheet="Values",
                    )
            expected_paths = {
                tuple(indices)
                for indices in _product_ranges(expected_sets)
            }
            if observed != expected_paths:
                _error(
                    "INCOMPLETE_ARRAY",
                    f"array column group {base_name!r}/{channel} is not structurally complete",
                    sheet="Values",
                )

    def _parse_stubs(self, ws: Any) -> list[ExportStub]:
        _active_formula_check(ws, sheet="Stubs", min_row=4)
        headers = _header_map(
            ws,
            sheet="Stubs",
            row=4,
            required=("Test Case", "Test Step", "Stub", "Stub Body"),
        )
        cells = _cell_map(ws)
        data_columns = tuple(headers.values())
        candidate_rows = sorted(
            {
                row
                for (row, _column), cell in cells.items()
                if row >= 5 and getattr(cell, "value", None) is not None
            }
        )
        result: list[ExportStub] = []
        seen_names: set[str] = set()
        for row in candidate_rows:
            if not _row_has_payload(cells, row, data_columns):
                continue
            name = _text_value(cells.get((row, headers[_normalize_label("Stub")])))
            if name is None or not name.strip():
                _error("MISSING_STUB_NAME", "active Stubs row has no Stub name", sheet="Stubs")
            if name in seen_names:
                _error("DUPLICATE_STUB_NAME", f"duplicate module-level Stub name {name!r}", sheet="Stubs")
            seen_names.add(name)
            test_case = _parse_integer(cells.get((row, headers[_normalize_label("Test Case")])), sheet="Stubs", field="Test Case", row=row)
            test_step = _parse_integer(cells.get((row, headers[_normalize_label("Test Step")])), sheet="Stubs", field="Test Step", row=row)
            body = _text_value(cells.get((row, headers[_normalize_label("Stub Body")])))
            if body is None:
                body = ""
            result.append(ExportStub(name=name, test_case=test_case, test_step=test_step, body_raw=body, source_row=row))
        return result

    def _validate_cross_sheet(
        self,
        properties: Mapping[int, _PropertyRecord],
        value_rows: list[
            tuple[int, int, str, dict[ExcelColumnKey, CellValue], dict[ExcelColumnKey, CellValue], int]
        ],
        columns: list[ExportColumn],
        stubs: list[ExportStub],
    ) -> None:
        value_numbers = [row[0] for row in value_rows]
        missing_properties = sorted(set(value_numbers) - set(properties))
        if missing_properties:
            _error("MISSING_PROPERTY_RECORD", f"Values testcase(s) lack Properties records: {missing_properties}")
        missing_values = sorted(set(properties) - set(value_numbers))
        if missing_values:
            _error("MISSING_VALUES_RECORD", f"Properties testcase(s) lack Values rows: {missing_values}")

        # Same visible name in input and output is the normal INOUT encoding.
        # The duplicate check was performed per (base, channel, element_path),
        # so this validation only documents the intended two-channel pairing.
        if len({stub.name for stub in stubs}) != len(stubs):
            _error("DUPLICATE_STUB_NAME", "Stub names must be unique within the module")

    def _join_cases(
        self,
        properties: Mapping[int, _PropertyRecord],
        value_rows: list[
            tuple[int, int, str, dict[ExcelColumnKey, CellValue], dict[ExcelColumnKey, CellValue], int]
        ],
    ) -> list[ExportCase]:
        result: list[ExportCase] = []
        for case_number, export_step, label, inputs, outputs, values_row in value_rows:
            property_record = properties[case_number]
            result.append(
                ExportCase(
                    export_case_number=case_number,
                    export_step=export_step,
                    row_label=label,
                    case_name=property_record.case_name,
                    external_test_case_id=property_record.external_test_case_id,
                    description_raw=property_record.description_raw,
                    input_cells=inputs,
                    output_cells=outputs,
                    properties_test_step=property_record.test_step,
                    properties_row=property_record.source_row,
                    description_type=property_record.description_type,
                    overview=property_record.overview,
                    values_row=values_row,
                )
            )
        return result


def _product_ranges(ranges: list[set[int]]) -> Iterable[tuple[int, ...]]:
    if not ranges:
        yield ()
        return
    head, *tail = ranges
    for value in sorted(head):
        for suffix in _product_ranges(tail):
            yield (value, *suffix)


def _read_xlsx_bytes(value: str | os.PathLike[str] | Path | bytes | bytearray) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        try:
            path = Path(os.fspath(value))
            data = path.read_bytes()
        except (TypeError, OSError) as exc:
            raise WorkbookSecurityError("cannot read XLSX input") from exc
    if not data:
        raise WorkbookSecurityError("XLSX input is empty")
    return data


def _validate_xlsx_container(data: bytes) -> None:
    if not zipfile.is_zipfile(BytesIO(data)):
        raise WorkbookSecurityError("input is not a ZIP-based XLSX workbook")
    try:
        with zipfile.ZipFile(BytesIO(data), "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise WorkbookSecurityError("XLSX contains too many ZIP members")
            total = 0
            names: set[str] = set()
            for info in infos:
                name = info.filename
                if name in names:
                    raise WorkbookSecurityError(f"XLSX contains duplicate ZIP member {name!r}")
                names.add(name)
                if "\x00" in name or "\\" in name:
                    raise WorkbookSecurityError(f"XLSX ZIP member has unsafe name {name!r}")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise WorkbookSecurityError(f"XLSX ZIP member has unsafe path {name!r}")
                if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise WorkbookSecurityError(f"XLSX ZIP member is too large: {name!r}")
                total += info.file_size
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise WorkbookSecurityError("XLSX uncompressed content is too large")
                if info.compress_size == 0:
                    if info.file_size > 0:
                        raise WorkbookSecurityError(f"XLSX ZIP member has invalid compression size: {name!r}")
                elif info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    raise WorkbookSecurityError(f"XLSX ZIP member has an unsafe compression ratio: {name!r}")
            required = {"[Content_Types].xml", "xl/workbook.xml"}
            missing = sorted(required - names)
            if missing:
                raise WorkbookSecurityError(f"XLSX container is missing required member(s): {', '.join(missing)}")
    except WorkbookAdapterError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise WorkbookSecurityError("cannot inspect XLSX ZIP container") from exc


def read_complete_export(
    path: str | os.PathLike[str] | Path | bytes | bytearray,
    **kwargs: Any,
) -> WorkbookSnapshot:
    """Functional facade for :class:`TessyWorkbookAdapter`."""

    return TessyWorkbookAdapter().read_complete_export(path, **kwargs)


def read_workbook(
    path: str | os.PathLike[str] | Path | bytes | bytearray,
    **kwargs: Any,
) -> WorkbookSnapshot:
    return read_complete_export(path, **kwargs)


WorkbookAdapter = TessyWorkbookAdapter
read = read_complete_export
parse_workbook = read_complete_export


__all__ = [
    "CellValue",
    "DirectionToken",
    "ExcelColumnKey",
    "ExportCase",
    "ExportColumn",
    "ExportStub",
    "GeneralIdentityError",
    "MissingSheetError",
    "PhysicalWorkbook",
    "PhysicalWorksheet",
    "TessyWorkbookAdapter",
    "UnsupportedFormulaError",
    "WorkbookAdapter",
    "WorkbookAdapterError",
    "WorkbookMeta",
    "WorkbookSecurityError",
    "WorkbookSnapshot",
    "WorkbookValidationError",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_ARCHIVE_MEMBER_BYTES",
    "MAX_ARCHIVE_TOTAL_BYTES",
    "MAX_COMPRESSION_RATIO",
    "parse_direction",
    "parse_direction_token",
    "parse_workbook",
    "read",
    "read_complete_export",
    "read_workbook",
    "physicalize_workbook",
    "physicalize_worksheet",
    "split_array_column_name",
]
