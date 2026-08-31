"""Pure candidate Merge A synchronization.

PR08 introduced the scalar row-oriented path.  PR09 extends the same pure
candidate operation with logical INOUT arguments, arrays, pointers and
module-scoped stubs.  The operation still deliberately does not acquire a
repository lock, write an artifact, update BASE, or change display version.

The implementation keeps the scalar path intact for compatibility.  Complex
workbooks are normalized before touching rows, and every validation failure
returns ``candidate=None`` so callers can never accidentally publish a
partially-built candidate.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import inspect
import re
import uuid
from typing import Any, Callable, Iterable, Mapping, Sequence

from .identity import (
    IdentityError,
    allocate_case_name,
    allocate_row_id,
    validate_case_name,
    validate_row_id,
)
from .model import (
    ArgumentState,
    CellValue,
    ExcelColumnKey,
    FunctionIterationState,
    LogicalArgumentKey,
    MergeConflict,
    RowMeta,
    StubParameter as ModelStubParameter,
    StubState,
    Tombstone,
    WorkingSnapshot,
)
from .adapters.tessy_workbook import ExportCase, ExportColumn, WorkbookSnapshot
from .adapters.value_xml import (
    FunctionSnapshot,
    ValueArgument,
    ValueXmlError,
    normalize_pointer_binding,
    project_pointer_bindings,
)
from utgen.model import TestRow


def _canonical_physical_name(name: str) -> str:
    """Canonicalize a physical Excel alias without erasing member identity."""

    return re.sub(r"\[[0-9]+\]", "", str(name))


def _canonical_argument_name(name: str) -> str:
    """Canonical logical XML name used for matching array arguments."""

    return _canonical_physical_name(name)


def _is_internal_argument_name(name: str) -> bool:
    text = str(name)
    lowered = text.casefold()
    return (
        "@address" in text
        or lowered.endswith("_control_output")
        or lowered.endswith("_pointer_control")
        or lowered.endswith("_pointer_control_output")
    )


def _indexed_name(name: str, path: tuple[int, ...]) -> str:
    """Append normalized array indices to an argument name."""

    # ``name`` is normally the unindexed workbook base.  If a caller supplies
    # an already-indexed XML name, retain its literal spelling and only append
    # indices for the normalized row representation.
    if not path:
        return name
    return f"{name}{''.join(f'[{index}]' for index in path)}"


def _value_argument_from_state(argument: ArgumentState) -> ValueArgument:
    """Project persisted argument metadata into the value-XML model."""

    return ValueArgument(
        name=argument.name,
        xml_len=argument.xml_len,
        fmt=argument.fmt,
        direction=argument.direction,
        pointer=argument.pointer,
        testmethod=argument.testmethod,
        dimensions=argument.dimensions,
        range_encoding=argument.range_encoding,
        output_encoding=argument.output_encoding,
        original_order=argument.original_order,
    )


def _argument_state_from_value_argument(argument: ValueArgument) -> ArgumentState:
    """Normalize a value-XML argument into the persisted schema record."""

    return ArgumentState(
        key=LogicalArgumentKey(_canonical_argument_name(argument.name), 0),
        name=argument.name,
        direction=argument.direction,
        fmt=argument.fmt,
        xml_len=argument.xml_len,
        pointer=argument.pointer,
        testmethod=argument.testmethod,
        dimensions=tuple(argument.dimensions),
        range_encoding=argument.range_encoding or "scalar",
        output_encoding=argument.output_encoding
        or ("scalar" if argument.direction in {"out", "inout"} else "absent"),
        source="generated",
        metadata_status="complete" if argument.pointer is not None else "unresolved",
        original_order=argument.original_order,
    )


_MISSING = object()


def _cell_raw_text(value: Any) -> str | None:
    if isinstance(value, CellValue):
        if value.kind == "missing":
            return None
        return value.raw_text if value.raw_text is not None else ""
    if value is _MISSING or value is None:
        return None
    return str(value)


def _is_null_text(value: Any) -> bool:
    raw = _cell_raw_text(value)
    return raw is not None and raw.strip().upper() == "NULL"


def _infer_array_shape(paths: Sequence[tuple[int, ...]]) -> tuple[tuple[int, ...], str | None]:
    """Infer a complete Cartesian array shape from physical paths."""

    normalized = [tuple(path) for path in paths]
    if not normalized or all(not path for path in normalized):
        return (), None
    if any(not path for path in normalized):
        return (), "array columns mix scalar and indexed paths"
    depth = len(normalized[0])
    if any(len(path) != depth for path in normalized):
        return (), "array columns have inconsistent nested dimensions"
    dimensions = tuple(max(path[index] for path in normalized) + 1 for index in range(depth))
    expected = {
        tuple(indices)
        for indices in __import__("itertools").product(*(range(size) for size in dimensions))
    }
    observed = set(normalized)
    if observed != expected:
        missing = sorted(expected - observed)
        duplicate = len(normalized) != len(observed)
        detail = "duplicate array indices" if duplicate else f"missing array index {missing[0] if missing else '?'}"
        return (), detail
    return dimensions, None


def _group_declared_name(name: str, group: Mapping[str, Any]) -> str:
    """Build the Inventory declaration spelling from every physical index."""

    shapes: set[tuple[int, ...]] = set()
    by_channel: dict[str, list[tuple[int, ...]]] = {}
    template: str | None = None
    for key, column in group.get("columns", {}).items():
        path = tuple(getattr(column, "element_path", ()) or ())
        if path:
            by_channel.setdefault(str(key[0]), []).append(path)
            if template is None:
                template = str(getattr(column, "name", name))
    for paths in by_channel.values():
        unique = list(dict.fromkeys(paths))
        shape, error = _infer_array_shape(unique)
        if error is not None:
            return name
        if shape:
            shapes.add(shape)
    if len(shapes) != 1:
        return name
    shape = next(iter(shapes))
    if template is None:
        return name
    index = 0

    def replace_extent(_match: re.Match[str]) -> str:
        nonlocal index
        if index >= len(shape):
            return _match.group(0)
        extent = shape[index]
        index += 1
        return f"[{extent}]"

    return re.sub(r"\[[0-9]+\]", replace_extent, template)


def _group_direction(columns: Mapping[Any, Any]) -> str:
    channels = {str(key[0]) for key in columns}
    if channels == {"input", "output"}:
        return "inout"
    if "output" in channels:
        return "out"
    return "in"


def _complex_case_signature(
    case: Any,
    schema: Mapping[str, Any],
) -> tuple[tuple[str, str, tuple[int, ...], tuple[Any, ...]], ...]:
    """Build a logical signature that hides pointer control/storage aliases."""

    entries: list[tuple[str, str, tuple[int, ...], tuple[Any, ...]]] = []
    for group in schema.get("groups", ()):
        argument = group.get("argument")
        if not isinstance(argument, ArgumentState):
            continue
        logical_name = _canonical_argument_name(argument.name)
        if group.get("pointer"):
            for binding in tuple(group.get("bindings", ())):
                control = _find_pointer_column(tuple(group.get("columns", {}).values()), binding, "control", "input")
                storage = _find_pointer_column(tuple(group.get("columns", {}).values()), binding, "storage", "input")
                if control is not None and storage is not None:
                    control_raw = _cell_raw_text(case.input_cells.get(control.key, CellValue("missing", None, None)))
                    storage_raw = _cell_raw_text(case.input_cells.get(storage.key, CellValue("missing", None, None)))
                    try:
                        logical = normalize_pointer_binding(control_raw, storage_raw, expected_storage=binding.expected_storage)
                    except (TypeError, ValueXmlError):
                        logical = "<invalid-pointer>"
                    entries.append((logical_name, "input", tuple(binding.element_path), _cell_token(logical)))
                output = _find_pointer_column(tuple(group.get("columns", {}).values()), binding, "storage", "output")
                if output is not None:
                    entries.append((logical_name, "output", tuple(binding.element_path), _cell_token(case.output_cells.get(output.key, CellValue("missing", None, None)))))
            continue
        for (channel, path, _role, _alias), column in group.get("columns", {}).items():
            cells = case.input_cells if channel == "input" else case.output_cells
            entries.append((logical_name, channel, tuple(path), _cell_token(cells.get(column.key, CellValue("missing", None, None)))))
    return tuple(sorted(entries, key=repr))


def _complex_row_signature(
    row: TestRow,
    schema: Mapping[str, Any],
) -> tuple[tuple[str, str, tuple[int, ...], tuple[Any, ...]], ...]:
    entries: list[tuple[str, str, tuple[int, ...], tuple[Any, ...]]] = []
    for group in schema.get("groups", ()):
        argument = group.get("argument")
        if not isinstance(argument, ArgumentState):
            continue
        logical_name = _canonical_argument_name(argument.name)
        if group.get("pointer"):
            for binding in tuple(group.get("bindings", ())):
                value = _row_value_for_path(row.values, argument.name, tuple(binding.element_path))
                entries.append((logical_name, "input", tuple(binding.element_path), _cell_token(value)))
                if argument.direction in {"out", "inout"}:
                    value = _row_value_for_path(row.expected, argument.name, tuple(binding.element_path))
                    entries.append((logical_name, "output", tuple(binding.element_path), _cell_token(value)))
            continue
        for (channel, path, _role, _alias), _column in group.get("columns", {}).items():
            mapping = row.values if channel == "input" else row.expected
            value = _row_value_for_path(mapping, argument.name, tuple(path))
            entries.append((logical_name, channel, tuple(path), _cell_token(value)))
    return tuple(sorted(entries, key=repr))


def _first_attr(value: Any, names: Iterable[str], *, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _normalize_direction(value: Any) -> str:
    if isinstance(value, str):
        return value.lower()
    raw = getattr(value, "value", None)
    return str(raw if raw is not None else value).lower()


def _normalize_complex_metadata(
    metadata: Any | None,
    name: str,
    direction: str,
    group: Mapping[str, Any],
) -> tuple[ArgumentState | None, list[tuple[str, str]]]:
    """Normalize resolver/Inventory metadata for scalar, array and pointer additions."""

    errors: list[tuple[str, str]] = []
    channel_paths: dict[str, list[tuple[int, ...]]] = {}
    for key, column in group.get("columns", {}).items():
        channel_paths.setdefault(str(key[0]), []).append(
            tuple(getattr(column, "element_path", ()) or ())
        )
    inferred_shapes: dict[str, tuple[int, ...]] = {}
    for channel, paths in channel_paths.items():
        # A pointer's control and storage aliases share an element path.  Do
        # not count those physical roles twice, but still let ordinary mixed
        # scalar/indexed groups be rejected by the final schema validation.
        indexed_paths = list(dict.fromkeys(path for path in paths if path))
        if not indexed_paths:
            inferred_shapes[channel] = ()
            continue
        inferred_shape, shape_error = _infer_array_shape(indexed_paths)
        if shape_error is not None:
            errors.append(("INVALID_ARRAY_SHAPE", f"new argument {name!r}: {shape_error}"))
            continue
        inferred_shapes[channel] = inferred_shape
    nonempty_shapes = {shape for shape in inferred_shapes.values() if shape}
    inferred_shape = next(iter(nonempty_shapes), ())
    if len(nonempty_shapes) > 1:
        errors.append(
            (
                "INVALID_ARRAY_SHAPE",
                f"new argument {name!r} has different input/output array shapes {sorted(nonempty_shapes)!r}",
            )
        )
    if metadata is None:
        errors.extend(
            (
                ("MISSING_ARGUMENT_FORMAT", f"new argument {name!r} has no explicit format metadata"),
                ("MISSING_ARGUMENT_LENGTH", f"new argument {name!r} has no explicit XML length metadata"),
                ("MISSING_POINTER_METADATA", f"new argument {name!r} has no explicit pointer metadata"),
            )
        )
        if inferred_shape:
            errors.append(("MISSING_ARRAY_DIMENSIONS", f"new array argument {name!r} has no explicit dimensions metadata"))
        return None, errors

    if isinstance(metadata, ArgumentState):
        argument = deepcopy(metadata)
    elif isinstance(metadata, ValueArgument):
        argument = _argument_state_from_value_argument(metadata)
    else:
        value = lambda *names, default=None: _first_attr(metadata, names, default=default)
        metadata_name = value("name", default=name)
        raw_dimensions = value("dimensions", "shape", default=None)
        if raw_dimensions is None:
            raw_name_dimensions = tuple(int(item) for item in re.findall(r"\[([0-9]+)\]", str(metadata_name)))
            raw_dimensions = raw_name_dimensions
        dimensions = tuple(int(item) for item in (raw_dimensions or ()))
        raw_direction = value("direction", default=direction)
        normalized_direction = _normalize_direction(raw_direction)
        pointer = value("pointer", "is_pointer", default=None)
        if pointer is None and hasattr(metadata, "address_of"):
            # ``address_of`` proves a pointer only when explicitly non-empty;
            # an empty field remains unresolved rather than becoming false.
            address_of = getattr(metadata, "address_of", "")
            pointer = True if address_of else value("pointer", "is_pointer", default=None)
        range_encoding = value("range_encoding", default="shared" if dimensions else "scalar")
        output_encoding = value(
            "output_encoding",
            default=("shared" if dimensions and normalized_direction in {"out", "inout"} else "scalar" if normalized_direction in {"out", "inout"} else "absent"),
        )
        try:
            argument = ArgumentState(
                LogicalArgumentKey(_canonical_argument_name(str(metadata_name)), int(value("occurrence", default=0) or 0)),
                str(metadata_name),
                normalized_direction,
                value("fmt", "format", default=None),
                None if value("xml_len", "len", "length", default=None) is None else str(value("xml_len", "len", "length", default=None)),
                pointer,
                value("testmethod", "test_method", default=None),
                dimensions,
                range_encoding,
                output_encoding,
                value("source", default="generated"),
                value("metadata_status", default="complete"),
                int(value("original_order", default=0) or 0),
            )
        except (TypeError, ValueError) as exc:
            return None, [("MISSING_ARGUMENT_METADATA", f"invalid metadata for new argument {name!r}: {exc}")]

    if _canonical_argument_name(argument.name) != _canonical_argument_name(name):
        errors.append(("AMBIGUOUS_ARGUMENT_DIRECTION", f"resolver metadata names {argument.name!r}, workbook is {name!r}"))
    if argument.direction != direction and not (argument.direction == "inout" and direction in {"in", "out"}):
        errors.append(("AMBIGUOUS_ARGUMENT_DIRECTION", f"resolver direction {argument.direction!r} conflicts with workbook {direction!r}"))
    channels = set(channel_paths)
    if argument.direction == "inout" and channels != {"input", "output"} and argument.pointer is not True:
        errors.append(
            (
                "AMBIGUOUS_ARGUMENT_DIRECTION",
                f"new INOUT argument {name!r} requires exactly one input and one output channel; workbook has {sorted(channels)!r}",
            )
        )
    if argument.fmt is None or argument.fmt == "":
        errors.append(("MISSING_ARGUMENT_FORMAT", f"new argument {name!r} lacks format metadata"))
    if argument.xml_len is None or argument.xml_len == "":
        errors.append(("MISSING_ARGUMENT_LENGTH", f"new argument {name!r} lacks XML length metadata"))
    if argument.pointer is None:
        errors.append(("MISSING_POINTER_METADATA", f"new argument {name!r} lacks pointer metadata"))
    if inferred_shape:
        if not argument.dimensions:
            errors.append(("MISSING_ARRAY_DIMENSIONS", f"new array argument {name!r} lacks dimensions metadata"))
        elif tuple(argument.dimensions) != inferred_shape:
            errors.append(("INVALID_ARRAY_SHAPE", f"new argument {name!r} dimensions {argument.dimensions} conflict with workbook shape {inferred_shape}"))
    elif argument.dimensions:
        errors.append(("INVALID_ARRAY_SHAPE", f"new argument {name!r} metadata declares dimensions {argument.dimensions} but workbook has no indexed columns"))
    if argument.metadata_status != "complete":
        errors.append(("MISSING_ARGUMENT_METADATA", f"new argument {name!r} metadata is not complete"))
    return (None if errors else argument), errors

def _find_pointer_column(
    columns: Sequence[Any],
    binding: Any,
    role: str,
    channel: str,
) -> Any | None:
    wanted = binding.control_name if role == "control" else binding.storage_name
    wanted_name = _canonical_physical_name(wanted)
    wanted_path = tuple(binding.element_path)
    candidates = [
        column
        for column in columns
        if column.channel == channel
        and _canonical_physical_name(column.base_name) == wanted_name
        and (
            tuple(column.element_path) == wanted_path
            or (role == "control" and not column.element_path)
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def _bindings_for_argument(projection: Any, argument_name: str) -> tuple[Any, ...]:
    direct = tuple(projection.for_argument(argument_name))
    if direct:
        return direct
    canonical = _canonical_argument_name(argument_name)
    return tuple(
        binding
        for binding in projection
        if _canonical_argument_name(binding.argument_name) == canonical
    )


def _row_value_for_path(mapping: Mapping[Any, Any], name: str, path: tuple[int, ...]) -> Any:
    indexed = _indexed_name(_canonical_argument_name(name), path)
    exact = _indexed_name(name, path)
    for key, value in mapping.items():
        if isinstance(key, ExcelColumnKey):
            if key.canonical_name in {_canonical_argument_name(name), _canonical_physical_name(name)} and tuple(key.element_path) == path:
                return value
            continue
        text = str(key)
        key_name, key_path = _row_key_parts(text)
        if key_path == path and key_name in {
            _canonical_argument_name(name),
            _canonical_physical_name(name),
            name,
        }:
            return value
        if text in {indexed, exact}:
            return value
    if not path:
        for key, value in mapping.items():
            if str(key) in {name, _canonical_argument_name(name)}:
                return value
    return _MISSING


def _cell_token(value: Any) -> tuple[Any, ...]:
    if isinstance(value, CellValue):
        # Match on the source kind and raw lexeme only.  A generated primitive
        # has no type proof at this layer, so its ordinary textual spelling
        # must match an imported CellValue with the same raw spelling; do not
        # compare the imported semantic string to the primitive's Python type.
        # Keeping the raw lexeme distinct preserves ``0`` vs ``0x00``, enum
        # symbols vs numeric text, and missing vs explicit-empty.
        return (value.kind, value.raw_text)
    if value is _MISSING:
        return ("missing", None)
    return ("value", str(value))


def _row_key_parts(key: Any) -> tuple[str, tuple[int, ...]]:
    if isinstance(key, ExcelColumnKey):
        return key.canonical_name, key.element_path
    text = str(key)
    matches = tuple(int(item) for item in re.findall(r"\[([0-9]+)\]", text))
    return re.sub(r"\[[0-9]+\]", "", text), matches


def _row_key_name(key: Any) -> str:
    return _row_key_parts(key)[0]


def _encode_xml_groups(
    groups: Sequence[tuple[str, ...]] | None,
    dimensions: tuple[int, ...],
    *,
    output: bool,
) -> tuple[str | None, str]:
    if groups is None:
        return None, "absent" if output else "scalar"
    normalized = tuple(tuple(item) for item in groups)
    dimensions = tuple(int(item) for item in (dimensions or ()))
    if not dimensions:
        if len(normalized) != 1:
            raise ValueXmlError(
                f"scalar {'output' if output else 'range'} requires exactly one stream, got {len(normalized)}"
            )
        return ",".join(normalized[0]), "scalar"
    element_count = 1
    for extent in dimensions:
        if extent <= 0:
            raise ValueXmlError(f"array dimensions must be positive, got {dimensions!r}")
        element_count *= extent
    if len(normalized) not in {1, element_count}:
        raise ValueXmlError(
            f"{'output' if output else 'range'} stream count {len(normalized)} "
            f"does not match array element count {element_count}"
        )
    if not normalized:
        raise ValueXmlError(
            f"array {'output' if output else 'range'} cannot be serialized without a stream"
        )
    row_count = len(normalized[0])
    if any(len(item) != row_count for item in normalized[1:]):
        raise ValueXmlError(
            f"{'output' if output else 'range'} streams have inconsistent testcase counts"
        )
    if len(normalized) == 1:
        return ",".join(normalized[0]), "shared"
    return "|".join(",".join(item) for item in normalized), "expanded"


_SCALAR_DIRECTIONS = frozenset({"in", "out"})
_INOUT_DIRECTIONS = frozenset({"inout"})
class _StubSignatureResolutionError(ValueError):
    """A source-signature provider failed instead of returning metadata."""


@dataclass(frozen=True)
class MergeADelta:
    """One pure, auditable change proposed by PR08."""

    entity_type: str
    action: str
    field: str | None
    row_id: str | None
    case_name: str | None
    logical_argument_key: str | None
    before: Any
    after: Any

    @property
    def entity(self) -> str:
        return self.entity_type

    @property
    def operation(self) -> str:
        return self.action

    @property
    def before_value(self) -> Any:
        return self.before

    @property
    def after_value(self) -> Any:
        return self.after


@dataclass
class MergeAResult:
    """Result of a pure Merge A candidate attempt.

    ``candidate`` is ``None`` whenever a blocking conflict exists.  The
    accepted input is never mutated, including when a candidate is rejected.
    """

    candidate: FunctionIterationState | WorkingSnapshot | None
    delta: tuple[MergeADelta, ...]
    conflicts: tuple[MergeConflict, ...]
    changed: bool
    # PR09 keeps XML/stub projections alongside the state candidate when the
    # caller supplies those accepted projections.  They are optional so the
    # PR08 positional result contract remains source compatible.
    value_snapshot: FunctionSnapshot | None = None
    module_stub_state: tuple[StubState, ...] | None = None

    @property
    def working(self) -> WorkingSnapshot | None:
        if self.candidate is None:
            return None
        return self.candidate.working if isinstance(self.candidate, FunctionIterationState) else self.candidate

    @property
    def candidate_state(self) -> FunctionIterationState | WorkingSnapshot | None:
        return self.candidate

    @property
    def candidate_working(self) -> WorkingSnapshot | None:
        return self.working

    @property
    def deltas(self) -> tuple[MergeADelta, ...]:
        """Plural spelling for callers that treat the audit output as a list."""

        return self.delta

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)

    @property
    def can_emit(self) -> bool:
        return self.candidate is not None and not self.conflicts

    @property
    def conflicts_by_code(self) -> dict[str, tuple[MergeConflict, ...]]:
        grouped: dict[str, list[MergeConflict]] = {}
        for conflict in self.conflicts:
            grouped.setdefault(conflict.code, []).append(conflict)
        return {code: tuple(items) for code, items in grouped.items()}


@dataclass(frozen=True)
class ScalarArgumentMetadata:
    """Minimal explicit metadata accepted for a new scalar workbook column."""

    name: str
    direction: str
    fmt: str | None
    xml_len: str | None
    pointer: bool | None
    testmethod: str | None = None
    source: str = "generated"
    metadata_status: str = "complete"


class MergeAEngine:
    """Apply a normalized complete workbook to a working snapshot."""

    def __init__(
        self,
        argument_resolver: Any | None = None,
        *,
        resolver: Any | None = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        function_key: str | None = None,
        source_file: str | None = None,
        whole_struct_bases: Iterable[str] | Mapping[str, bool] | None = None,
    ) -> None:
        if argument_resolver is not None and resolver is not None and argument_resolver is not resolver:
            raise ValueError("argument_resolver and resolver disagree")
        self.argument_resolver = argument_resolver if argument_resolver is not None else resolver
        self.uuid_factory = uuid_factory
        self.function_key = function_key
        self.source_file = source_file
        self.whole_struct_bases = whole_struct_bases

    def merge(
        self,
        accepted: FunctionIterationState | WorkingSnapshot,
        workbook: WorkbookSnapshot,
        resolver: Any | None = None,
        *,
        argument_resolver: Any | None = None,
        inventory: Any | None = None,
        required_arguments: Iterable[str] | None = None,
        function_key: str | None = None,
        source_file: str | None = None,
        value_snapshot: FunctionSnapshot | None = None,
        module_stub_state: Iterable[StubState] | None = None,
        required_stubs: Iterable[str] | Mapping[str, Any] | None = None,
        stub_signatures: Any | None = None,
        stub_signature_resolver: Any | None = None,
        whole_struct_bases: Iterable[str] | Mapping[str, bool] | None = None,
    ) -> MergeAResult:
        """Return a pure candidate without committing it anywhere.

        A resolver is required only when the workbook introduces a column not
        already present in ``working.arguments``.  It must return complete
        metadata; the engine never fabricates format, length, pointer, array,
        or direction metadata.
        """

        if not isinstance(accepted, (FunctionIterationState, WorkingSnapshot)):
            raise TypeError("accepted must be a FunctionIterationState or WorkingSnapshot")
        if not isinstance(workbook, WorkbookSnapshot):
            raise TypeError("workbook must be a normalized WorkbookSnapshot")
        if resolver is not None and argument_resolver is not None and resolver is not argument_resolver:
            raise ValueError("resolver and argument_resolver disagree")

        if value_snapshot is not None and not isinstance(value_snapshot, FunctionSnapshot):
            raise TypeError("value_snapshot must be a parsed FunctionSnapshot")
        if module_stub_state is not None:
            module_stub_state = tuple(module_stub_state)
            if not all(isinstance(item, StubState) for item in module_stub_state):
                raise TypeError("module_stub_state must contain only StubState records")
        # Inventory is the PR08 source of truth for known stub signatures when
        # the caller does not pass the PR09-specific signature mapping.
        selected_stub_signatures = stub_signatures
        if selected_stub_signatures is None and inventory is not None:
            selected_stub_signatures = getattr(inventory, "stubs", None)
        if whole_struct_bases is None:
            whole_struct_bases = self.whole_struct_bases

        # The presence of any logical array, pointer, INOUT, XML projection or
        # stub state selects the PR09 path.  Scalar-only PR08 calls retain
        # their established matching/allocation behavior byte-for-byte.
        working_for_dispatch = accepted.working if isinstance(accepted, FunctionIterationState) else accepted
        if self._needs_complex_merge(workbook, working_for_dispatch, value_snapshot, module_stub_state):
            return self._merge_complex(
                accepted,
                workbook,
                resolver=resolver if resolver is not None else argument_resolver,
                inventory=inventory,
                required_arguments=required_arguments,
                function_key=function_key,
                source_file=source_file,
                value_snapshot=value_snapshot,
                module_stub_state=module_stub_state,
                required_stubs=required_stubs,
                stub_signatures=selected_stub_signatures,
                stub_signature_resolver=stub_signature_resolver,
                whole_struct_bases=whole_struct_bases,
            )
        selected_resolver = resolver if resolver is not None else argument_resolver
        if selected_resolver is None:
            selected_resolver = inventory
        if selected_resolver is None:
            selected_resolver = self.argument_resolver

        state = accepted if isinstance(accepted, FunctionIterationState) else None
        working = accepted.working if state is not None else accepted
        actual_function_key = (
            function_key
            or (state.function_key if state is not None else None)
            or self.function_key
            or getattr(workbook.meta, "testobject", None)
            or "unknown_function"
        )
        if not actual_function_key:
            actual_function_key = "unknown_function"

        # Complete-export absence is the only safe evidence of a user
        # deletion.  A partial/preview snapshot must never be allowed to
        # remove rows or arguments merely because they are not represented.
        if workbook.completeness != "complete":
            conflict = self._conflict(
                "INCOMPLETE_WORKBOOK",
                actual_function_key,
                entity_type="workbook",
                message="Merge A deletion semantics require a complete workbook export",
            )
            return MergeAResult(None, tuple(), (conflict,), False)

        deltas: list[MergeADelta] = []
        conflicts: list[MergeConflict] = []
        working_copy = deepcopy(working)

        identity_conflicts = self._validate_working_identity(
            accepted,
            working_copy,
            actual_function_key,
        )
        if identity_conflicts:
            return MergeAResult(None, tuple(), tuple(identity_conflicts), False)

        scalar_columns, unsupported = self._scalar_columns(workbook.columns)
        for code, column in unsupported:
            conflicts.append(
                self._conflict(
                    code,
                    actual_function_key,
                    entity_type="argument",
                    logical_argument_key=column.name,
                    message=f"PR08 handles scalar columns only; {column.name!r} requires a later adapter",
                    user=column.name,
                )
            )

        active_rows = self._active_rows(working_copy)
        row_matches, row_conflicts = self._match_rows(
            active_rows,
            workbook.cases,
            actual_function_key,
        )
        conflicts.extend(row_conflicts)

        argument_plan, argument_conflicts = self._plan_arguments(
            working_copy,
            scalar_columns,
            selected_resolver,
            actual_function_key,
            required_arguments=required_arguments,
        )
        conflicts.extend(argument_conflicts)

        if conflicts:
            return MergeAResult(None, tuple(deltas), tuple(conflicts), False)

        # Apply argument deletions/additions before rows so every row sees the
        # same scalar schema.  The original argument order remains authoritative.
        working_copy, argument_deltas = self._apply_argument_plan(
            working_copy,
            argument_plan,
            actual_function_key,
        )
        deltas.extend(argument_deltas)

        # Existing matched rows retain their identity and name.  New rows are
        # allocated only after every ambiguity/metadata blocker has passed.
        existing_row_ids = {
            row.meta.row_id
            for row in working_copy.rows
            if row.meta is not None
        }
        existing_case_names = {
            row.meta.case_name
            for row in working_copy.rows
            if row.meta is not None
        }
        next_case_number = self._next_case_number(accepted, existing_case_names)
        source_file = (
            state.source_file
            if state is not None
            else source_file
            or self.source_file
            or getattr(working_copy, "source_file", None)
            or getattr(workbook.meta, "module", None)
        )
        new_rows: list[TestRow] = []
        output_order: list[str] = []
        matched_row_indexes = {working_index for working_index in row_matches.values()}
        row_by_index = {
            position: row
            for position, (_, row) in enumerate(active_rows)
        }

        for workbook_index, export_case in enumerate(workbook.cases):
            working_index = row_matches.get(workbook_index)
            if working_index is None:
                if not source_file:
                    conflicts.append(
                        self._conflict(
                            "MISSING_SOURCE_IDENTITY",
                            actual_function_key,
                            entity_type="testcase",
                            case_name=export_case.case_name,
                            message="cannot allocate a case name without explicit source filename",
                            user=export_case.row_label,
                        )
                    )
                    continue
                try:
                    row, row_id, case_name, next_case_number = self._new_manual_row(
                        export_case,
                        actual_function_key,
                        working_copy.generation,
                        working_copy.snapshot_id,
                        existing_row_ids,
                        existing_case_names,
                        next_case_number,
                        source_file,
                    )
                except (IdentityError, TypeError) as exc:
                    conflicts.append(
                        self._conflict(
                            "IDENTITY_ALLOCATION_CONFLICT",
                            actual_function_key,
                            entity_type="testcase",
                            case_name=export_case.case_name,
                            message=f"cannot allocate a persistent testcase identity: {exc}",
                            user=export_case.row_label,
                        )
                    )
                    continue
                existing_row_ids.add(row_id)
                existing_case_names.add(case_name)
                new_rows.append(row)
                output_order.append(row_id)
                deltas.append(
                    MergeADelta(
                        "testcase",
                        "add",
                        None,
                        row_id,
                        case_name,
                        None,
                        None,
                        row,
                    )
                )
                continue

            row = row_by_index[working_index]
            if row.meta is None:
                # A row without persisted identity cannot be safely carried
                # through a complete-export synchronization.
                conflicts.append(
                    self._conflict(
                        "AMBIGUOUS_ROW_MATCH",
                        actual_function_key,
                        entity_type="testcase",
                        message="matched working row has no persistent row identity",
                    )
                )
                continue
            updated, row_deltas = self._update_existing_row(
                row,
                export_case,
                scalar_columns,
                argument_plan,
            )
            new_rows.append(updated)
            output_order.append(row.meta.row_id)
            deltas.extend(row_deltas)

        # Late allocation/identity checks above are still blocking.  Do not
        # return a partially built candidate when a WorkingSnapshot lacks the
        # source identity needed for a new case (or any other late guard fails).
        if conflicts:
            # The deltas below may include speculative allocations made while
            # checking the complete export.  They are not an accepted patch:
            # expose no partial delta alongside a rejected candidate.
            return MergeAResult(None, tuple(), tuple(conflicts), False)

        # Preserve inactive historical/orphan rows and remove unmatched active
        # rows from the new active order.  Complete export absence is therefore
        # represented by tombstones, never by a guessed zero row.
        deleted_rows = [
            row
            for position, (_original_index, row) in enumerate(active_rows)
            if position not in matched_row_indexes
            and row.meta is not None
        ]
        tombstones = list(working_copy.tombstones)
        existing_tombstones = {
            (t.entity_type, t.entity_key)
            for t in tombstones
        }
        active_row_indices = {idx for idx, _ in active_rows}
        retained_rows: list[TestRow] = []
        for index, row in enumerate(working_copy.rows):
            if index in active_row_indices and row.meta is not None:
                continue
            retained_rows.append(row)
        for row in deleted_rows:
            # Tombstones carry the durable deletion fact, while the inactive
            # row record retains its prior values/description for audit and
            # future conflict inspection.  It is intentionally excluded from
            # row_order and therefore cannot be matched again as active.
            historical = replace(row, meta=replace(row.meta, status="orphaned"))
            retained_rows.append(historical)
            key = ("testcase", row.meta.row_id)
            if key not in existing_tombstones:
                tombstones.append(Tombstone("testcase", row.meta.row_id, working_copy.generation))
                deltas.append(
                    MergeADelta(
                        "testcase",
                        "delete",
                        None,
                        row.meta.row_id,
                        row.meta.case_name,
                        None,
                        row,
                        None,
                    )
                )

        if working_copy.row_order != tuple(output_order):
            deltas.append(
                MergeADelta(
                    "order",
                    "reorder",
                    "row_order",
                    None,
                    None,
                    None,
                    working_copy.row_order,
                    tuple(output_order),
                )
            )

        # ``new_rows`` follows complete workbook order; historical inactive
        # rows stay in state after the active rows for deterministic persistence.
        candidate_rows = tuple(new_rows + retained_rows)
        candidate_working = WorkingSnapshot(
            snapshot_id=working_copy.snapshot_id,
            generation=working_copy.generation,
            analysis_id=working_copy.analysis_id,
            row_order=tuple(output_order),
            rows=candidate_rows,
            arguments=working_copy.arguments,
            tombstones=tuple(tombstones),
        )

        if state is not None:
            candidate: FunctionIterationState | WorkingSnapshot = replace(
                state,
                working=candidate_working,
                next_case_number=next_case_number,
            )
        else:
            candidate = candidate_working
        changed = bool(deltas)
        return MergeAResult(candidate, tuple(deltas), tuple(), changed)

    @staticmethod
    def _needs_complex_merge(
        workbook: WorkbookSnapshot,
        working: WorkingSnapshot,
        value_snapshot: FunctionSnapshot | None,
        module_stub_state: Iterable[StubState] | None,
    ) -> bool:
        if value_snapshot is not None or module_stub_state is not None:
            return True
        if getattr(workbook, "stubs", None):
            return True
        columns = tuple(getattr(workbook, "columns", ()) or ())
        if any(getattr(column, "element_path", ()) for column in columns):
            return True
        by_name: dict[str, set[str]] = {}
        for column in columns:
            by_name.setdefault(str(getattr(column, "base_name", getattr(column, "name", ""))), set()).add(
                str(getattr(column, "channel", ""))
            )
        if any(channels == {"input", "output"} for channels in by_name.values()):
            return True
        return any(
            argument.pointer is not False
            or bool(argument.dimensions)
            or argument.direction == "inout"
            for argument in working.arguments
        )

    def _merge_complex(
        self,
        accepted: FunctionIterationState | WorkingSnapshot,
        workbook: WorkbookSnapshot,
        *,
        resolver: Any | None,
        inventory: Any | None,
        required_arguments: Iterable[str] | None,
        function_key: str | None,
        source_file: str | None,
        value_snapshot: FunctionSnapshot | None,
        module_stub_state: tuple[StubState, ...] | None,
        required_stubs: Iterable[str] | Mapping[str, Any] | None,
        stub_signatures: Any | None,
        stub_signature_resolver: Any | None,
        whole_struct_bases: Iterable[str] | Mapping[str, bool] | None,
    ) -> MergeAResult:
        """Synchronize logical arrays/pointers/INOUT and module stubs.

        All intermediate structures are ordinary detached values.  A caller
        receives a candidate only after schema, pointer, row and stub checks
        have all passed, which is the key atomicity invariant for this pure
        stage.
        """

        state = accepted if isinstance(accepted, FunctionIterationState) else None
        working = accepted.working if state is not None else accepted
        actual_function_key = (
            function_key
            or (state.function_key if state is not None else None)
            or self.function_key
            or getattr(workbook.meta, "testobject", None)
            or "unknown_function"
        )
        if not actual_function_key:
            actual_function_key = "unknown_function"

        if value_snapshot is not None:
            accepted_function_names = (
                {state.function_key, state.function}
                if state is not None
                else {actual_function_key}
            )
            if function_key is not None and function_key not in accepted_function_names:
                conflict = self._conflict(
                    "IDENTITY_FUNCTION_MISMATCH",
                    actual_function_key,
                    entity_type="function",
                    message=(
                        "merge target function key does not match accepted state "
                        f"identity: target={function_key!r}, "
                        f"accepted={sorted(accepted_function_names)!r}"
                    ),
                    base=tuple(sorted(accepted_function_names)),
                    user=function_key,
                )
                return MergeAResult(None, tuple(), (conflict,), False)
            if value_snapshot.name not in accepted_function_names:
                conflict = self._conflict(
                    "IDENTITY_FUNCTION_MISMATCH",
                    actual_function_key,
                    entity_type="function",
                    message=(
                        "Value XML function name does not match accepted state "
                        f"identity: XML={value_snapshot.name!r}, "
                        f"accepted={sorted(accepted_function_names)!r}"
                    ),
                    base=tuple(sorted(accepted_function_names)),
                    user=value_snapshot.name,
                )
                return MergeAResult(None, tuple(), (conflict,), False)

        if workbook.completeness != "complete":
            conflict = self._conflict(
                "INCOMPLETE_WORKBOOK",
                actual_function_key,
                entity_type="workbook",
                message="Merge A deletion semantics require a complete workbook export",
            )
            return MergeAResult(None, tuple(), (conflict,), False)

        identity_conflicts = self._validate_working_identity(accepted, deepcopy(working), actual_function_key)
        if identity_conflicts:
            return MergeAResult(None, tuple(), tuple(identity_conflicts), False)

        working_copy = deepcopy(working)
        conflicts: list[MergeConflict] = []
        deltas: list[MergeADelta] = []

        if value_snapshot is not None:
            conflicts.extend(
                self._validate_value_snapshot_compatibility(
                    working_copy,
                    value_snapshot,
                    actual_function_key,
                )
            )
            if conflicts:
                return MergeAResult(None, tuple(), tuple(conflicts), False)

        schema = self._complex_schema(
            working_copy,
            workbook,
            resolver if resolver is not None else inventory,
            actual_function_key,
            required_arguments=required_arguments,
            value_snapshot=value_snapshot,
            whole_struct_bases=whole_struct_bases,
        )
        conflicts.extend(schema["conflicts"])
        if conflicts:
            return MergeAResult(None, tuple(), tuple(conflicts), False)
        old_argument_names = {
            _canonical_argument_name(argument.name)
            for argument in working_copy.arguments
        }
        for argument in schema.get("arguments", ()):
            if _canonical_argument_name(argument.name) not in old_argument_names:
                deltas.append(
                    MergeADelta(
                        "argument",
                        "add",
                        None,
                        None,
                        None,
                        argument.name,
                        None,
                        argument,
                    )
                )

        # Row matching still uses the proven PR08 identity ladder.  Its cell
        # signature is extended below for aliases, but explicit case names and
        # Properties IDs remain authoritative in either path.
        active_rows = self._active_rows(working_copy)
        row_matches, row_conflicts = self._match_rows(
            active_rows,
            workbook.cases,
            actual_function_key,
            case_signature=lambda case: _complex_case_signature(case, schema),
            row_signature=lambda row: _complex_row_signature(row, schema),
        )
        conflicts.extend(row_conflicts)
        if conflicts:
            return MergeAResult(None, tuple(), tuple(conflicts), False)

        row_by_index = {position: row for position, (_, row) in enumerate(active_rows)}
        existing_row_ids = {
            row.meta.row_id for row in working_copy.rows if row.meta is not None
        }
        existing_case_names = {
            row.meta.case_name for row in working_copy.rows if row.meta is not None
        }
        next_case_number = self._next_case_number(accepted, existing_case_names)
        source_file_value = (
            state.source_file
            if state is not None
            else source_file
            or self.source_file
            or getattr(workbook.meta, "module", None)
        )

        new_rows: list[TestRow] = []
        output_order: list[str] = []
        matched_row_indexes = set(row_matches.values())
        row_errors: list[MergeConflict] = []
        for workbook_index, export_case in enumerate(workbook.cases):
            working_index = row_matches.get(workbook_index)
            if working_index is None:
                if not source_file_value:
                    row_errors.append(
                        self._conflict(
                            "MISSING_SOURCE_IDENTITY",
                            actual_function_key,
                            entity_type="testcase",
                            case_name=export_case.case_name,
                            message="cannot allocate a case name without explicit source filename",
                            user=export_case.row_label,
                        )
                    )
                    continue
                try:
                    row_id = self._allocate_row_id(existing_row_ids)
                    case_name, next_case_number = self._allocate_case_name(
                        source_file_value,
                        next_case_number,
                        existing_case_names,
                    )
                except (IdentityError, TypeError) as exc:
                    row_errors.append(
                        self._conflict(
                            "IDENTITY_ALLOCATION_CONFLICT",
                            actual_function_key,
                            entity_type="testcase",
                            case_name=export_case.case_name,
                            message=f"cannot allocate a persistent testcase identity: {exc}",
                            user=export_case.row_label,
                        )
                    )
                    continue
                row = self._new_complex_row(
                    export_case,
                    schema,
                    row_id=row_id,
                    case_name=case_name,
                    function_key=actual_function_key,
                    generation=working_copy.generation,
                    row_errors=row_errors,
                )
                if row is None:
                    continue
                existing_row_ids.add(row_id)
                existing_case_names.add(case_name)
                new_rows.append(row)
                output_order.append(row_id)
                deltas.append(
                    MergeADelta("testcase", "add", None, row_id, case_name, None, None, row)
                )
                continue

            row = row_by_index.get(working_index)
            if row is None or row.meta is None:
                row_errors.append(
                    self._conflict(
                        "AMBIGUOUS_ROW_MATCH",
                        actual_function_key,
                        entity_type="testcase",
                        message="matched working row has no persistent row identity",
                    )
                )
                continue
            updated, row_deltas = self._update_complex_row(
                row,
                export_case,
                schema,
                actual_function_key,
                row_errors,
            )
            new_rows.append(updated)
            output_order.append(row.meta.row_id)
            deltas.extend(row_deltas)

        conflicts.extend(row_errors)
        if conflicts:
            return MergeAResult(None, tuple(), tuple(conflicts), False)

        # Complete-export absence means deletion for every logical argument.
        # Pointer storage aliases are not logical arguments and are therefore
        # never independently tombstoned.
        represented = set(schema["represented_names"])
        tombstones = list(working_copy.tombstones)
        tombstone_keys = {(item.entity_type, item.entity_key) for item in tombstones}
        arguments = list(schema["arguments"])
        explicit_required = set(required_arguments or ())
        for argument in tuple(arguments):
            canonical = _canonical_argument_name(argument.name)
            if canonical in represented:
                continue
            # A synthetic/address argument is implementation state, not a
            # deletable XML argument.
            if _is_internal_argument_name(argument.name):
                continue
            is_required = argument.source == "generated" or argument.name in explicit_required or canonical in explicit_required
            if is_required:
                conflicts.append(
                    self._conflict(
                        "REQUIRED_ARGUMENT_DELETED",
                        actual_function_key,
                        entity_type="argument",
                        logical_argument_key=argument.name,
                        message=f"required argument {argument.name!r} is absent from complete workbook",
                    )
                )
                continue
            if ("argument", argument.name) not in tombstone_keys:
                tombstones.append(Tombstone("argument", argument.name, working_copy.generation))
                tombstone_keys.add(("argument", argument.name))
                deltas.append(MergeADelta("argument", "delete", None, None, None, argument.name, argument, None))
            arguments = [item for item in arguments if item is not argument]

        if conflicts:
            return MergeAResult(None, tuple(), tuple(conflicts), False)

        # Apply encoding metadata only after row validation.  Shapes come from
        # complete physical index sets; XML len is never consulted for extent.
        previous_arguments = {argument.name: argument for argument in arguments}
        arguments = self._finalize_complex_argument_encodings(
            arguments,
            schema,
            new_rows,
        )
        for argument in arguments:
            previous = previous_arguments.get(argument.name)
            if previous is None:
                continue
            if (
                previous.range_encoding != argument.range_encoding
                or previous.output_encoding != argument.output_encoding
            ):
                deltas.append(
                    MergeADelta(
                        "argument",
                        "update",
                        "encoding",
                        None,
                        None,
                        argument.name,
                        (previous.range_encoding, previous.output_encoding),
                        (argument.range_encoding, argument.output_encoding),
                    )
                )

        deleted_rows = [
            row
            for position, (_original_index, row) in enumerate(active_rows)
            if position not in matched_row_indexes and row.meta is not None
        ]
        active_row_indices = {idx for idx, _ in active_rows}
        retained_rows: list[TestRow] = []
        for index, row in enumerate(working_copy.rows):
            if index in active_row_indices and row.meta is not None:
                continue
            retained_rows.append(row)
        for row in deleted_rows:
            historical = replace(row, meta=replace(row.meta, status="orphaned"))
            retained_rows.append(historical)
            key = ("testcase", row.meta.row_id)
            if key not in tombstone_keys:
                tombstones.append(Tombstone("testcase", row.meta.row_id, working_copy.generation))
                tombstone_keys.add(key)
                deltas.append(MergeADelta("testcase", "delete", None, row.meta.row_id, row.meta.case_name, None, row, None))

        if working_copy.row_order != tuple(output_order):
            deltas.append(MergeADelta("order", "reorder", "row_order", None, None, None, working_copy.row_order, tuple(output_order)))

        candidate_working = WorkingSnapshot(
            snapshot_id=working_copy.snapshot_id,
            generation=working_copy.generation,
            analysis_id=working_copy.analysis_id,
            row_order=tuple(output_order),
            rows=tuple(new_rows + retained_rows),
            arguments=tuple(arguments),
            tombstones=tuple(tombstones),
        )
        candidate: FunctionIterationState | WorkingSnapshot
        if state is not None:
            candidate = replace(state, working=candidate_working, next_case_number=next_case_number)
        else:
            candidate = candidate_working

        candidate_value = None
        if value_snapshot is not None:
            candidate_value, xml_conflicts = self._update_value_snapshot(
                value_snapshot,
                workbook,
                schema,
                actual_function_key,
            )
            if xml_conflicts:
                return MergeAResult(None, tuple(), tuple(xml_conflicts), False)

        candidate_stubs: tuple[StubState, ...] | None = None
        if module_stub_state is not None or workbook.stubs:
            candidate_stubs, stub_conflicts, stub_deltas = self._synchronize_stubs(
                module_stub_state or (),
                tuple(workbook.stubs or ()),
                actual_function_key,
                required_stubs=required_stubs,
                signatures=stub_signatures,
                signature_resolver=stub_signature_resolver,
            )
            if stub_conflicts:
                return MergeAResult(None, tuple(), tuple(stub_conflicts), False)
            deltas.extend(stub_deltas)

        changed = bool(deltas) or candidate_value != value_snapshot or candidate_stubs != module_stub_state
        return MergeAResult(candidate, tuple(deltas), tuple(), changed, candidate_value, candidate_stubs)

    def _validate_value_snapshot_compatibility(
        self,
        working: WorkingSnapshot,
        value_snapshot: FunctionSnapshot,
        function_key: str,
    ) -> list[MergeConflict]:
        """Require accepted state/XML schema metadata to agree before edits.

        The workbook is allowed to add a new logical argument, but the
        accepted state and accepted Value XML are two views of the same
        existing schema.  Updating one from the other when they disagree
        would produce a candidate that cannot be round-tripped safely.
        """

        def logical_arguments(items: Iterable[Any]) -> tuple[dict[str, Any], list[MergeConflict]]:
            records: dict[str, Any] = {}
            errors: list[MergeConflict] = []
            for item in items:
                name = str(getattr(item, "name", ""))
                if not name or _is_internal_argument_name(name):
                    continue
                canonical = _canonical_argument_name(name)
                if canonical in records:
                    errors.append(
                        self._conflict(
                            "VALUE_XML_STATE_MISMATCH",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=canonical,
                            message=f"accepted schema contains duplicate logical argument {canonical!r}",
                            base=records[canonical],
                            user=item,
                        )
                    )
                else:
                    records[canonical] = item
            return records, errors

        state_args, conflicts = logical_arguments(working.arguments)
        xml_args, xml_conflicts = logical_arguments(
            argument
            for argument in value_snapshot.arguments
            if not getattr(argument, "internal", False)
            and not getattr(argument, "synthetic_pointer_control", False)
        )
        conflicts.extend(xml_conflicts)
        if set(state_args) != set(xml_args):
            missing_xml = sorted(set(state_args) - set(xml_args))
            missing_state = sorted(set(xml_args) - set(state_args))
            conflicts.append(
                self._conflict(
                    "VALUE_XML_STATE_MISMATCH",
                    function_key,
                    entity_type="schema",
                    message=(
                        "accepted state/XML logical argument sets differ; "
                        f"missing XML={missing_xml!r}, missing state={missing_state!r}"
                    ),
                    base=tuple(sorted(state_args)),
                    user=tuple(sorted(xml_args)),
                )
            )

        state_order = tuple(
            canonical
            for canonical, _argument in sorted(
                state_args.items(),
                key=lambda item: getattr(item[1], "original_order", 0),
            )
        )
        xml_order = tuple(
            canonical
            for canonical, _argument in sorted(
                xml_args.items(),
                key=lambda item: getattr(item[1], "original_order", 0),
            )
        )
        if state_order != xml_order:
            conflicts.append(
                self._conflict(
                    "VALUE_XML_STATE_MISMATCH",
                    function_key,
                    entity_type="schema",
                    message=(
                        "accepted state/XML argument order differs: "
                        f"state={state_order!r}, XML={xml_order!r}"
                    ),
                    base=state_order,
                    user=xml_order,
                )
            )

        fields = (
            "name",
            "direction",
            "fmt",
            "xml_len",
            "pointer",
            "dimensions",
            "testmethod",
        )
        for canonical in sorted(set(state_args) & set(xml_args)):
            state_argument = state_args[canonical]
            xml_argument = xml_args[canonical]
            for field_name in fields:
                state_value = getattr(state_argument, field_name)
                xml_value = getattr(xml_argument, field_name)
                if field_name == "dimensions":
                    state_value = tuple(state_value or ())
                    xml_value = tuple(xml_value or ())
                if state_value == xml_value:
                    continue
                conflicts.append(
                    self._conflict(
                        "VALUE_XML_STATE_MISMATCH",
                        function_key,
                        entity_type="argument",
                        logical_argument_key=canonical,
                        message=(
                            f"accepted state/XML {field_name} differs for {canonical!r}: "
                            f"state={state_value!r}, XML={xml_value!r}"
                        ),
                        base=state_value,
                        user=xml_value,
                    )
                )
        return conflicts

    def _complex_schema(
        self,
        working: WorkingSnapshot,
        workbook: WorkbookSnapshot,
        resolver: Any | None,
        function_key: str,
        *,
        required_arguments: Iterable[str] | None,
        value_snapshot: FunctionSnapshot | None,
        whole_struct_bases: Iterable[str] | Mapping[str, bool] | None,
    ) -> dict[str, Any]:
        """Build the logical argument/physical-column projection for PR09."""

        conflicts: list[MergeConflict] = []
        arguments = list(working.arguments)
        state_by_name: dict[str, list[ArgumentState]] = {}
        for argument in arguments:
            state_by_name.setdefault(_canonical_argument_name(argument.name), []).append(argument)

        # State/XML metadata is authoritative.  The XML projection is useful
        # for pointer grouping variants and for callers that are importing an
        # existing XML artifact whose state was bootstrapped separately.
        projection_arguments: list[ValueArgument] = []
        xml_by_name = (
            {
                _canonical_argument_name(argument.name): argument
                for argument in value_snapshot.arguments
                if argument.pointer is True
            }
            if value_snapshot is not None
            else {}
        )
        xml_metadata_by_name = (
            {
                _canonical_argument_name(argument.name): argument
                for argument in value_snapshot.arguments
                if not argument.internal
                and not argument.synthetic_pointer_control
                and "@address" not in argument.name
            }
            if value_snapshot is not None
            else {}
        )
        for argument in arguments:
            # Keep state pointer truth authoritative, but retain an accepted
            # XML argument's explicit grouping hint (pointer_grouping/group)
            # when one is available.
            projection_arguments.append(
                xml_by_name.get(_canonical_argument_name(argument.name), _value_argument_from_state(argument))
                if argument.pointer is True
                else _value_argument_from_state(argument)
            )
        if value_snapshot is not None:
            known = {_canonical_argument_name(argument.name) for argument in projection_arguments}
            projection_arguments.extend(
                argument
                for argument in value_snapshot.arguments
                if _canonical_argument_name(argument.name) not in known
            )
        try:
            projection = project_pointer_bindings(
                projection_arguments,
                whole_struct_bases=whole_struct_bases,
            )
        except (TypeError, ValueXmlError) as exc:
            conflict = self._conflict(
                "INVALID_POINTER_BINDING",
                function_key,
                entity_type="argument",
                message=f"cannot project pointer bindings: {exc}",
            )
            return {
                "arguments": arguments,
                "groups": (),
                "columns": (),
                "represented_names": set(),
                "conflicts": [conflict],
                "projection": None,
            }

        pointer_groups: dict[str, dict[str, Any]] = {}
        binding_to_groups: dict[tuple[str, tuple[int, ...]], list[dict[str, Any]]] = {}
        for argument in arguments:
            if argument.pointer is not True:
                continue
            name = _canonical_argument_name(argument.name)
            group = {
                "name": name,
                "argument": argument,
                "columns": {},
                "pointer": True,
                "bindings": list(_bindings_for_argument(projection, argument.name)),
                "new": False,
                "shape": {},
            }
            pointer_groups[name] = group
            for binding in group["bindings"]:
                binding_to_groups.setdefault(
                    (_canonical_physical_name(binding.control_name), tuple(binding.element_path)), []
                ).append(group)
                # Whole-struct pointer arrays reuse one scalar base control
                # column for every element.  Let that physical ``base``
                # column attach to all element bindings as well as the exact
                # indexed key used by member controls.
                if binding.element_path and not re.search(r"\[[0-9]+\]", str(binding.control_name)):
                    binding_to_groups.setdefault(
                        (_canonical_physical_name(binding.control_name), ()), []
                    ).append(group)
                binding_to_groups.setdefault(
                    (_canonical_physical_name(binding.storage_name), tuple(binding.element_path)), []
                ).append(group)

        # Include XML pointer arguments that are absent in state as unresolved
        # logical records.  They will still require complete metadata before
        # a candidate can be emitted, but this lets the adapter report the
        # precise pointer conflict instead of treating storage aliases as new
        # scalar variables.
        for xml_argument in (value_snapshot.arguments if value_snapshot is not None else ()):
            if xml_argument.pointer is not True:
                continue
            name = _canonical_argument_name(xml_argument.name)
            if name in pointer_groups:
                continue
            pointer_state = _argument_state_from_value_argument(xml_argument)
            pointer_state = replace(
                pointer_state,
                original_order=max(
                    (item.original_order for item in arguments),
                    default=-1,
                )
                + 1,
            )
            arguments.append(pointer_state)
            pointer_groups[name] = {
                "name": name,
                "argument": pointer_state,
                "columns": {},
                "pointer": True,
                "bindings": list(_bindings_for_argument(projection, xml_argument.name)),
                "new": True,
                "shape": {},
            }
            for binding in pointer_groups[name]["bindings"]:
                binding_to_groups.setdefault(
                    (_canonical_physical_name(binding.control_name), tuple(binding.element_path)), []
                ).append(pointer_groups[name])
                if binding.element_path and not re.search(r"\[[0-9]+\]", str(binding.control_name)):
                    binding_to_groups.setdefault(
                        (_canonical_physical_name(binding.control_name), ()), []
                    ).append(pointer_groups[name])
                binding_to_groups.setdefault(
                    (_canonical_physical_name(binding.storage_name), tuple(binding.element_path)), []
                ).append(pointer_groups[name])

        generic: dict[str, dict[str, Any]] = {}
        seen_physical: set[tuple[str, str, tuple[int, ...]]] = set()
        columns: list[ExportColumn] = []
        for column in tuple(workbook.columns or ()):
            name = str(getattr(column, "name", ""))
            base = str(getattr(column, "base_name", name))
            path = tuple(getattr(column, "element_path", ()) or ())
            channel = str(getattr(column, "channel", ""))
            if _is_internal_argument_name(name) or _is_internal_argument_name(base):
                # Internal @address leaves and synthetic controls may be
                # present in legacy workbook exports; they are deliberately
                # not logical XML arguments.
                continue
            physical_key = (_canonical_physical_name(base), channel, path)
            if physical_key in seen_physical:
                conflicts.append(
                    self._conflict(
                        "DUPLICATE_CHANNEL_COLUMN",
                        function_key,
                        entity_type="argument",
                        logical_argument_key=base,
                        message=f"same-name same-channel Values column {name!r} occurs more than once",
                        user=name,
                    )
                )
                continue
            seen_physical.add(physical_key)

            pointer_targets = binding_to_groups.get(
                (_canonical_physical_name(base), path),
                (),
            )
            if pointer_targets:
                # A whole-struct control may fan out to multiple member
                # groups.  Retain the physical column on every target; the
                # storage alias remains an implementation role, not a new
                # logical XML argument.
                for target in pointer_targets:
                    target["columns"][(channel, path, "physical", _canonical_physical_name(base))] = column
                columns.append(column)
                continue

            key = _canonical_physical_name(base)
            target = generic.setdefault(
                key,
                {
                    "name": key,
                    "argument": None,
                    "columns": {},
                    "pointer": False,
                    "bindings": [],
                    "new": False,
                    "shape": {},
                },
            )
            target["columns"][(channel, path, "physical", _canonical_physical_name(base))] = column
            columns.append(column)

        # Resolve the existing argument for each non-pointer physical group,
        # then resolve metadata for genuinely new groups.  A same-name pair
        # is one logical INOUT argument only when each channel/path is unique.
        groups: list[dict[str, Any]] = list(pointer_groups.values()) + list(generic.values())
        groups.sort(key=lambda item: min(
            (getattr(column, "source_column", 0) for column in item["columns"].values()),
            default=10**9,
        ))

        # Resolve metadata for new groups once before the main pass.  This
        # lets a newly discovered pointer group consume its hidden
        # ``tessy_*`` storage group even when the storage column happens to
        # precede the control column in the workbook.  Without this small
        # pre-pass, the storage alias could be mistaken for a second unknown
        # logical argument.
        for group in groups:
            if group["pointer"] or state_by_name.get(group["name"]):
                continue
            first_column = next(iter(group["columns"].values()), None)
            if first_column is None:
                continue
            direction = _group_direction(group["columns"])
            metadata = xml_metadata_by_name.get(group["name"])
            if metadata is None:
                metadata = _resolve_argument_metadata(
                    resolver,
                    group["name"],
                    first_column,
                    declared_name=_group_declared_name(group["name"], group),
                )
            group["resolved_metadata"] = _normalize_complex_metadata(
                metadata,
                group["name"],
                direction,
                group,
            )

        consumed_aliases: set[int] = set()
        for group in groups:
            resolved = group.get("resolved_metadata")
            normalized = resolved[0] if isinstance(resolved, tuple) and len(resolved) == 2 else None
            if not isinstance(normalized, ArgumentState) or normalized.pointer is not True:
                continue
            try:
                projected_new = project_pointer_bindings(
                    (_value_argument_from_state(normalized),),
                    whole_struct_bases=whole_struct_bases,
                )
            except (TypeError, ValueXmlError):
                continue
            bindings = tuple(projected_new.for_argument(normalized.name))
            if not bindings:
                continue
            group["pointer"] = True
            # Mark a valid newly-resolved logical argument now because the
            # main pass will intentionally visit it through the pointer
            # branch after this pre-pass.
            normalized = replace(
                normalized,
                original_order=max(
                    (item.original_order for item in arguments),
                    default=-1,
                )
                + 1,
            )
            group["argument"] = normalized
            group["new"] = True
            arguments.append(normalized)
            group["bindings"] = list(bindings)
            aliases = {
                _canonical_physical_name(binding.control_name)
                for binding in bindings
            } | {
                _canonical_physical_name(binding.storage_name)
                for binding in bindings
            }
            for alias_group in groups:
                if alias_group is group or alias_group["pointer"]:
                    continue
                if alias_group["name"] not in aliases:
                    continue
                # A real state argument with this name is not an internal
                # pointer alias; leave it visible so the ambiguity is
                # reported by normal schema resolution.
                if state_by_name.get(alias_group["name"]):
                    continue
                group["columns"].update(alias_group["columns"])
                consumed_aliases.add(id(alias_group))
        if consumed_aliases:
            groups = [group for group in groups if id(group) not in consumed_aliases]

        represented_names: set[str] = set()
        for group in groups:
            name = group["name"]
            if group["pointer"]:
                argument = group.get("argument")
                if argument is None:
                    continue
                group["argument"] = argument
                group["name"] = _canonical_argument_name(argument.name)
                if group["columns"]:
                    represented_names.add(group["name"])
                # Pointer groups need an explicit control and storage alias
                # for every XML element.  Missing aliases are rejected during
                # row validation with INVALID_POINTER_BINDING.
                conflicts.extend(
                    self._validate_complex_argument(
                        argument,
                        group,
                        function_key,
                    )
                )
                continue

            candidates = state_by_name.get(name, [])
            if len(candidates) > 1:
                # Occurrence is an explicit discriminator when supplied by
                # either the XML key or physical column key.
                occurrences = {
                    int(getattr(getattr(column, "key", None), "occurrence", 0) or 0)
                    for column in group["columns"].values()
                }
                selected = [item for item in candidates if item.key.occurrence in occurrences]
                if len(selected) == 1:
                    candidates = selected
                else:
                    conflicts.append(
                        self._conflict(
                            "AMBIGUOUS_ARGUMENT_DIRECTION",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=name,
                            message=f"more than one working argument matches {name!r}",
                            user=name,
                        )
                    )
                    continue
            if candidates:
                argument = candidates[0]
                group["argument"] = argument
                represented_names.add(name)
                conflicts.extend(self._validate_complex_argument(argument, group, function_key))
            else:
                resolved = group.pop("resolved_metadata", None)
                if resolved is None:
                    first_column = next(iter(group["columns"].values()), None)
                    if first_column is None:
                        continue
                    direction = _group_direction(group["columns"])
                    metadata = xml_metadata_by_name.get(name)
                    if metadata is None:
                        metadata = _resolve_argument_metadata(
                            resolver,
                            name,
                            first_column,
                            declared_name=_group_declared_name(name, group),
                        )
                    normalized, metadata_errors = _normalize_complex_metadata(
                        metadata,
                        name,
                        direction,
                        group,
                    )
                else:
                    normalized, metadata_errors = resolved
                if metadata_errors:
                    for code, message in metadata_errors:
                        conflicts.append(
                            self._conflict(
                                code,
                                function_key,
                                entity_type="argument",
                                logical_argument_key=name,
                                message=message,
                                user=name,
                            )
                        )
                    continue
                normalized = replace(
                    normalized,
                    original_order=max(
                        (item.original_order for item in arguments),
                        default=-1,
                    )
                    + 1,
                )
                group["argument"] = normalized
                group["new"] = True
                represented_names.add(name)
                arguments.append(normalized)

                # A newly-resolved pointer may use one of the established
                # aliases.  Build bindings now and attach matching columns;
                # aliases remain hidden from the logical argument list.
                if normalized.pointer is True:
                    group["pointer"] = True
                    try:
                        projection_new = project_pointer_bindings(
                            (_value_argument_from_state(normalized),),
                            whole_struct_bases=whole_struct_bases,
                        )
                    except (TypeError, ValueXmlError) as exc:
                        conflicts.append(
                            self._conflict(
                                "INVALID_POINTER_BINDING",
                                function_key,
                                entity_type="argument",
                                logical_argument_key=name,
                                message=str(exc),
                            )
                        )
                    else:
                        group["bindings"] = list(projection_new.for_argument(normalized.name))
                        for binding in group["bindings"]:
                            for column in columns:
                                if (
                                    _canonical_physical_name(column.base_name)
                                    in {
                                        _canonical_physical_name(binding.control_name),
                                        _canonical_physical_name(binding.storage_name),
                                    }
                                    and tuple(column.element_path) == tuple(binding.element_path)
                                ):
                                    group["columns"][(column.channel, tuple(column.element_path), "physical", _canonical_physical_name(column.base_name))] = column

                conflicts.extend(self._validate_complex_group_shape(group, function_key, existing=normalized))

        # Pointer state groups supplied by state but absent from workbook are
        # intentionally left unrepresented; deletion/tombstone policy is
        # applied after row construction once all schema blockers are known.
        return {
            "arguments": arguments,
            "groups": tuple(groups),
            "columns": tuple(columns),
            "represented_names": represented_names,
            "conflicts": conflicts,
            "projection": projection,
            "state_by_name": state_by_name,
        }

    def _validate_complex_argument(
        self,
        argument: ArgumentState,
        group: Mapping[str, Any],
        function_key: str,
    ) -> list[MergeConflict]:
        conflicts: list[MergeConflict] = []
        name = argument.name
        if argument.fmt is None or argument.fmt == "":
            conflicts.append(self._conflict("MISSING_ARGUMENT_FORMAT", function_key, entity_type="argument", logical_argument_key=name, message=f"existing argument {name!r} lacks format metadata", user=name))
        if argument.xml_len is None or argument.xml_len == "":
            conflicts.append(self._conflict("MISSING_ARGUMENT_LENGTH", function_key, entity_type="argument", logical_argument_key=name, message=f"existing argument {name!r} lacks XML length metadata", user=name))
        if argument.pointer is None:
            conflicts.append(self._conflict("MISSING_POINTER_METADATA", function_key, entity_type="argument", logical_argument_key=name, message=f"existing argument {name!r} lacks pointer metadata", user=name))
        if argument.metadata_status != "complete":
            conflicts.append(self._conflict("MISSING_ARGUMENT_METADATA", function_key, entity_type="argument", logical_argument_key=name, message=f"existing argument {name!r} metadata is not complete", user=name))
        workbook_direction = _group_direction(group.get("columns", {}))
        channels = {key[0] for key in group.get("columns", {})}
        if argument.direction == "inout" and channels != {"input", "output"}:
            conflicts.append(
                self._conflict(
                    "AMBIGUOUS_ARGUMENT_DIRECTION",
                    function_key,
                    entity_type="argument",
                    logical_argument_key=name,
                    message=(
                        f"existing INOUT argument {name!r} requires exactly one "
                        f"input and one output channel; workbook has {sorted(channels)!r}"
                    ),
                    user=name,
                )
            )
        elif not group.get("pointer") and argument.direction != workbook_direction:
            conflicts.append(self._conflict("AMBIGUOUS_ARGUMENT_DIRECTION", function_key, entity_type="argument", logical_argument_key=name, message=f"workbook channels imply {workbook_direction!r}, existing argument is {argument.direction!r}", user=name))

        shape_conflicts = self._validate_complex_group_shape(group, function_key, existing=argument)
        conflicts.extend(shape_conflicts)
        return conflicts

    def _validate_complex_group_shape(
        self,
        group: Mapping[str, Any],
        function_key: str,
        *,
        existing: ArgumentState,
    ) -> list[MergeConflict]:
        conflicts: list[MergeConflict] = []
        columns = group.get("columns", {})
        by_channel: dict[str, list[tuple[int, ...]]] = {}
        pointer_control_names = {
            _canonical_physical_name(binding.control_name)
            for binding in tuple(group.get("bindings", ()))
        }
        pointer_storage_names = {
            _canonical_physical_name(binding.storage_name)
            for binding in tuple(group.get("bindings", ()))
        }
        for key, column in columns.items():
            # A whole-struct pointer array has one scalar control column plus
            # indexed pointee storage columns.  The control is not an array
            # element and must not make a valid storage shape look like a
            # scalar/indexed mixture.  Keep it in the binding validation but
            # infer dimensions from storage aliases only.
            if (
                group.get("pointer")
                and key[0] == "input"
                and _canonical_physical_name(column.base_name) in pointer_control_names
                and _canonical_physical_name(column.base_name) not in pointer_storage_names
            ):
                continue
            channel = key[0]
            by_channel.setdefault(channel, []).append(tuple(getattr(column, "element_path", ()) or ()))
        inferred: dict[str, tuple[int, ...]] = {}
        for channel, paths in by_channel.items():
            # Pointer controls and storage aliases can represent the same
            # logical element.  They are distinct physical roles, not
            # duplicate array elements for shape inference.
            unique_paths = list(dict.fromkeys(paths))
            shape, error = _infer_array_shape(unique_paths)
            if error is not None:
                conflicts.append(self._conflict("INVALID_ARRAY_SHAPE", function_key, entity_type="argument", logical_argument_key=existing.name, message=error, user=existing.name))
                continue
            inferred[channel] = shape
            if existing.dimensions and shape and shape != tuple(existing.dimensions):
                conflicts.append(self._conflict("INVALID_ARRAY_SHAPE", function_key, entity_type="argument", logical_argument_key=existing.name, message=f"workbook {channel} array shape {shape} conflicts with XML dimensions {existing.dimensions}", user=shape))
            elif existing.dimensions and not shape and columns:
                # A true XML array must have all physical indices in a
                # complete TESSY export; a scalar fallback would lose shape.
                conflicts.append(self._conflict("INVALID_ARRAY_SHAPE", function_key, entity_type="argument", logical_argument_key=existing.name, message=f"workbook {channel} omits indexed columns for array {existing.name!r}", user=existing.name))
            elif not existing.dimensions and shape:
                conflicts.append(self._conflict("INVALID_ARRAY_SHAPE", function_key, entity_type="argument", logical_argument_key=existing.name, message=f"workbook {channel} indexes scalar argument {existing.name!r}", user=shape))
        # Store the independently inferred channel shapes for row/XML folding.
        try:
            group["shape"].update(inferred)  # type: ignore[index]
        except (AttributeError, TypeError):
            pass
        return conflicts

    def _new_complex_row(
        self,
        export_case: ExportCase,
        schema: Mapping[str, Any],
        *,
        row_id: str,
        case_name: str,
        function_key: str,
        generation: int,
        row_errors: list[MergeConflict],
    ) -> TestRow | None:
        meta = RowMeta(
            row_id=row_id,
            case_name=case_name,
            external_test_case_id=export_case.external_test_case_id,
            origin="manual",
            status="active",
            generation_created=generation,
            generation_modified=generation,
            function_key=function_key,
            obligation_keys=(),
        )
        row = TestRow(
            values={},
            kind=export_case.description_type or "manual",
            description=export_case.description_raw,
            intent={},
            expected={},
            meta=meta,
        )
        updated, _deltas = self._update_complex_row(
            row,
            export_case,
            schema,
            function_key,
            row_errors,
        )
        return None if row_errors else updated

    def _update_complex_row(
        self,
        row: TestRow,
        export_case: ExportCase,
        schema: Mapping[str, Any],
        function_key: str,
        row_errors: list[MergeConflict],
    ) -> tuple[TestRow, list[MergeADelta]]:
        """Apply one export row while retaining logical array/pointer roles."""

        values = deepcopy(row.values)
        expected = deepcopy(row.expected)
        deltas: list[MergeADelta] = []

        def record_cell(
            target: dict[Any, Any],
            column: ExportColumn,
            cell: CellValue,
            *,
            logical_name: str | None = None,
            element_path: tuple[int, ...] | None = None,
            channel: str,
        ) -> None:
            lookup_column = column
            existing_key = _find_row_key(target, lookup_column)
            name = logical_name or column.name
            if existing_key is not None:
                key: Any = existing_key
            elif element_path:
                existing_path = tuple(int(item) for item in re.findall(r"\[([0-9]+)\]", name))
                key = name if existing_path == tuple(element_path) else _indexed_name(name, element_path)
            else:
                key = name
            before = target.get(key, _MISSING)
            if _field_equal(before, cell):
                return
            target[key] = cell
            deltas.append(
                MergeADelta(
                    "cell",
                    "update",
                    "input" if channel == "input" else "expected",
                    row.meta.row_id if row.meta is not None else None,
                    row.meta.case_name if row.meta is not None else None,
                    logical_name or column.name,
                    None if before is _MISSING else before,
                    cell,
                )
            )

        for group in schema.get("groups", ()):  # type: ignore[assignment]
            argument = group.get("argument")
            if not isinstance(argument, ArgumentState):
                continue
            name = argument.name
            if group.get("pointer"):
                self._update_pointer_row(
                    row,
                    export_case,
                    group,
                    record_cell,
                    values,
                    expected,
                    function_key,
                    row_errors,
                )
                continue
            for (channel, path, _role, _alias), column in sorted(
                group.get("columns", {}).items(),
                key=lambda item: (
                    0 if item[0][0] == "input" else 1,
                    tuple(item[0][1]),
                    getattr(item[1], "source_column", 0),
                ),
            ):
                cells = export_case.input_cells if channel == "input" else export_case.output_cells
                cell = cells.get(column.key, CellValue("missing", None, None))
                target = values if channel == "input" else expected
                record_cell(
                    target,
                    column,
                    cell,
                    # Keep the physical spelling for indexed members.  The
                    # base/path pair remains the normalized identity used by
                    # matching, while row state should round-trip nested
                    # names such as ``s.member[0].field[1]`` exactly.
                    logical_name=column.name,
                    element_path=tuple(path),
                    channel=channel,
                )

        description = export_case.description_raw
        meta = row.meta
        if meta is not None:
            if row.description != description:
                deltas.append(
                    MergeADelta(
                        "testcase",
                        "update",
                        "description_raw",
                        meta.row_id,
                        meta.case_name,
                        None,
                        row.description,
                        description,
                    )
                )
            if meta.external_test_case_id != export_case.external_test_case_id:
                deltas.append(
                    MergeADelta(
                        "testcase",
                        "update",
                        "external_test_case_id",
                        meta.row_id,
                        meta.case_name,
                        None,
                        meta.external_test_case_id,
                        export_case.external_test_case_id,
                    )
                )
                meta = replace(meta, external_test_case_id=export_case.external_test_case_id)
        return replace(row, values=values, expected=expected, description=description, meta=meta), deltas

    def _update_pointer_row(
        self,
        row: TestRow,
        export_case: ExportCase,
        group: Mapping[str, Any],
        record_cell: Callable[..., None],
        values: dict[Any, Any],
        expected: dict[Any, Any],
        function_key: str,
        row_errors: list[MergeConflict],
    ) -> None:
        argument = group.get("argument")
        if not isinstance(argument, ArgumentState):
            return
        bindings = tuple(group.get("bindings", ()))
        if not bindings:
            row_errors.append(
                self._conflict(
                    "INVALID_POINTER_BINDING",
                    function_key,
                    entity_type="argument",
                    logical_argument_key=argument.name,
                    message=f"pointer argument {argument.name!r} has no projected control/storage binding",
                    user=argument.name,
                )
            )
            return
        columns = tuple(group.get("columns", {}).values())
        # ``columns`` can contain the same whole-struct control once for every
        # member group.  De-duplicate only by physical identity here.
        unique_columns: list[ExportColumn] = []
        seen_columns: set[tuple[int, str, tuple[int, ...]]] = set()
        for column in columns:
            key = (int(getattr(column, "source_column", 0)), column.channel, tuple(column.element_path))
            if key not in seen_columns:
                seen_columns.add(key)
                unique_columns.append(column)

        def find_column(binding: Any, role: str, channel: str) -> ExportColumn | None:
            wanted = binding.control_name if role == "control" else binding.storage_name
            wanted_name = _canonical_physical_name(wanted)
            wanted_path = tuple(binding.element_path)
            candidates = [
                item
                for item in unique_columns
                if item.channel == channel
                and _canonical_physical_name(item.base_name) == wanted_name
                and (
                    tuple(item.element_path) == wanted_path
                    or (role == "control" and not item.element_path)
                )
            ]
            return candidates[0] if len(candidates) == 1 else None

        def raw_cell(case: ExportCase, column: ExportColumn | None) -> tuple[str | None, CellValue | None]:
            if column is None:
                return None, None
            cells = case.input_cells if column.channel == "input" else case.output_cells
            value = cells.get(column.key, CellValue("missing", None, None))
            return _cell_raw_text(value), value

        for binding in bindings:
            control_column = find_column(binding, "control", "input")
            storage_column = find_column(binding, "storage", "input")
            control, _control_cell = raw_cell(export_case, control_column)
            storage, storage_cell = raw_cell(export_case, storage_column)
            if control_column is None or storage_column is None:
                row_errors.append(
                    self._conflict(
                        "INVALID_POINTER_BINDING",
                        function_key,
                        entity_type="argument",
                        logical_argument_key=argument.name,
                        message=f"pointer {argument.name!r} is missing control/storage cells for {binding.element_name!r}",
                        user=binding.element_name,
                    )
                )
            else:
                try:
                    normalized = normalize_pointer_binding(
                        control,
                        storage,
                        expected_storage=binding.expected_storage,
                    )
                except (TypeError, ValueXmlError) as exc:
                    row_errors.append(
                        self._conflict(
                            "INVALID_POINTER_BINDING",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=argument.name,
                            message=f"invalid pointer binding for {binding.element_name!r}: {exc}",
                            user={"control": control, "storage": storage},
                        )
                    )
                    normalized = None
                if normalized is not None:
                    # Preserve the exact storage CellValue for non-NULL input;
                    # NULL is represented by its exact control lexeme so it
                    # cannot be mistaken for numeric zero.
                    if _is_null_text(normalized):
                        cell = CellValue("value", "NULL", "NULL")
                    elif storage_cell is not None and storage_cell.kind != "missing":
                        cell = storage_cell
                    else:
                        cell = CellValue("value", normalized, normalized)
                    logical_column = storage_column or control_column
                    record_cell(
                        values,
                        logical_column,
                        cell,
                        logical_name=binding.element_name,
                        element_path=tuple(binding.element_path),
                        channel="input",
                    )

            # Output is sourced only from pointee storage.  A synthetic
            # pointer-control output, if present, is intentionally ignored.
            if argument.direction in {"out", "inout"}:
                output_column = find_column(binding, "storage", "output")
                output_cell = None
                if output_column is not None:
                    output_cells = export_case.output_cells
                    output_cell = output_cells.get(output_column.key, CellValue("missing", None, None))
                if output_column is None:
                    row_errors.append(
                        self._conflict(
                            "INVALID_POINTER_BINDING",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=argument.name,
                            message=f"pointer {argument.name!r} has no pointee expected-output storage for {binding.element_name!r}",
                            user=binding.element_name,
                        )
                    )
                else:
                    record_cell(
                        expected,
                        output_column,
                        output_cell or CellValue("missing", None, None),
                        logical_name=binding.element_name,
                        element_path=tuple(binding.element_path),
                        channel="output",
                    )

    def _finalize_complex_argument_encodings(
        self,
        arguments: Sequence[ArgumentState],
        schema: Mapping[str, Any],
        rows: Sequence[TestRow],
    ) -> list[ArgumentState]:
        groups_by_name = {
            _canonical_argument_name(group["argument"].name): group
            for group in schema.get("groups", ())
            if isinstance(group.get("argument"), ArgumentState)
        }
        result: list[ArgumentState] = []
        for argument in arguments:
            group = groups_by_name.get(_canonical_argument_name(argument.name))
            if group is None:
                result.append(argument)
                continue
            if not argument.dimensions:
                output_encoding = argument.output_encoding
                if argument.direction in {"out", "inout"}:
                    output_encoding = "scalar"
                elif not any(key[0] == "output" for key in group.get("columns", {})):
                    output_encoding = "absent"
                result.append(replace(argument, range_encoding="scalar", output_encoding=output_encoding))
                continue

            range_encoding = argument.range_encoding
            output_encoding = argument.output_encoding
            for channel in ("input", "output"):
                if channel == "output" and argument.direction == "in":
                    output_encoding = "absent"
                    continue
                physical_paths = {
                    tuple(key[1])
                    for key in group.get("columns", {})
                    if key[0] == channel
                }
                if not physical_paths:
                    continue
                streams: list[tuple[Any, ...]] = []
                for path in sorted(physical_paths):
                    path_values: list[Any] = []
                    for row in rows:
                        mapping = row.values if channel == "input" else row.expected
                        path_values.append(_row_value_for_path(mapping, argument.name, path))
                    streams.append(tuple(_cell_token(value) for value in path_values))
                shared = bool(streams) and all(item == streams[0] for item in streams[1:])
                encoding = "shared" if shared else "expanded"
                # Existing shared encoding remains shared while all element
                # streams are equal.  A changed element that differs from its
                # siblings necessarily becomes expanded; encoding is kept
                # independent for input and output channels.
                if channel == "input":
                    range_encoding = encoding
                else:
                    output_encoding = encoding
            result.append(replace(argument, range_encoding=range_encoding, output_encoding=output_encoding))
        return result

    def _update_value_snapshot(
        self,
        snapshot: FunctionSnapshot,
        workbook: WorkbookSnapshot,
        schema: Mapping[str, Any],
        function_key: str,
    ) -> tuple[FunctionSnapshot | None, list[MergeConflict]]:
        """Fold workbook physical cells back into an XML logical snapshot."""

        groups_by_name = {
            _canonical_argument_name(group["argument"].name): group
            for group in schema.get("groups", ())
            if isinstance(group.get("argument"), ArgumentState)
        }
        state_names = set(schema.get("state_by_name", {}))
        represented_names = set(schema.get("represented_names", set()))
        conflicts: list[MergeConflict] = []
        updated_arguments: list[ValueArgument] = []
        for argument in snapshot.arguments:
            if argument.internal or argument.synthetic_pointer_control or "@address" in argument.name:
                updated_arguments.append(argument)
                continue
            # A complete workbook that omits an existing logical argument is
            # an explicit deletion (the caller has already passed the
            # required-argument blocker).  Do not retain a stale XML element
            # merely because it was not present in the workbook projection.
            canonical_name = _canonical_argument_name(argument.name)
            if canonical_name in state_names and canonical_name not in represented_names:
                continue
            group = groups_by_name.get(_canonical_argument_name(argument.name))
            if group is None:
                # Keep unknown XML arguments and their original placement and
                # metadata.  State/workbook validation owns additions.
                updated_arguments.append(argument)
                continue

            range_groups: list[tuple[str, ...]] | None = None
            output_groups: list[tuple[str, ...]] | None = None
            if group.get("pointer"):
                range_groups, pointer_conflicts = self._pointer_xml_streams(
                    argument,
                    group,
                    workbook,
                    function_key,
                )
                conflicts.extend(pointer_conflicts)
                if argument.direction in {"out", "inout"}:
                    output_groups = self._pointer_output_streams(group, workbook)
                    if output_groups is None:
                        conflicts.append(
                            self._conflict(
                                "INVALID_POINTER_BINDING",
                                function_key,
                                entity_type="argument",
                                logical_argument_key=argument.name,
                                message=f"pointer {argument.name!r} has no pointee output stream",
                            )
                        )
            else:
                range_groups = self._ordinary_xml_streams(group, workbook, "input")
                if argument.direction in {"out", "inout"}:
                    output_groups = self._ordinary_xml_streams(group, workbook, "output")

            if conflicts:
                updated_arguments.append(argument)
                continue
            try:
                new_range, range_encoding = _encode_xml_groups(
                    range_groups,
                    argument.dimensions,
                    output=False,
                )
                new_output, output_encoding = _encode_xml_groups(
                    output_groups,
                    argument.dimensions,
                    output=True,
                )
            except ValueXmlError as exc:
                conflicts.append(
                    self._conflict(
                        "INVALID_ARRAY_SHAPE",
                        function_key,
                        entity_type="argument",
                        logical_argument_key=argument.name,
                        message=str(exc),
                    )
                )
                updated_arguments.append(argument)
                continue
            attrs = dict(argument.attributes)
            if new_range is None:
                attrs.pop("range", None)
            else:
                attrs["range"] = new_range
            if new_output is None:
                attrs.pop("output", None)
            else:
                attrs["output"] = new_output
            updated_arguments.append(
                replace(
                    argument,
                    range=new_range,
                    output=new_output,
                    range_encoding=range_encoding,
                    output_encoding=output_encoding,
                    attributes=attrs,
                )
            )
        # A known new logical argument is emitted after all existing XML
        # arguments, preserving accepted order.  Pointer storage/control
        # aliases are never synthesized as separate XML arguments.
        existing_xml_names = {
            _canonical_argument_name(argument.name)
            for argument in snapshot.arguments
            if not _is_internal_argument_name(argument.name)
        }
        for group in schema.get("groups", ()):
            state_argument = group.get("argument")
            if not isinstance(state_argument, ArgumentState):
                continue
            if not group.get("new") or _canonical_argument_name(state_argument.name) in existing_xml_names:
                continue
            argument = _value_argument_from_state(state_argument)
            if group.get("pointer"):
                range_groups, pointer_conflicts = self._pointer_xml_streams(
                    argument,
                    group,
                    workbook,
                    function_key,
                )
                conflicts.extend(pointer_conflicts)
                output_groups = (
                    self._pointer_output_streams(group, workbook)
                    if argument.direction in {"out", "inout"}
                    else None
                )
            else:
                range_groups = self._ordinary_xml_streams(group, workbook, "input")
                output_groups = (
                    self._ordinary_xml_streams(group, workbook, "output")
                    if argument.direction in {"out", "inout"}
                    else None
                )
            if conflicts:
                continue
            try:
                new_range, range_encoding = _encode_xml_groups(
                    range_groups,
                    argument.dimensions,
                    output=False,
                )
                new_output, output_encoding = _encode_xml_groups(
                    output_groups,
                    argument.dimensions,
                    output=True,
                )
            except ValueXmlError as exc:
                conflicts.append(
                    self._conflict(
                        "INVALID_ARRAY_SHAPE",
                        function_key,
                        entity_type="argument",
                        logical_argument_key=state_argument.name,
                        message=str(exc),
                    )
                )
                continue
            attrs = dict(argument.attributes)
            if new_range is not None:
                attrs["range"] = new_range
            if new_output is not None:
                attrs["output"] = new_output
            updated_arguments.append(
                replace(
                    argument,
                    range=new_range,
                    output=new_output,
                    range_encoding=range_encoding,
                    output_encoding=output_encoding,
                    attributes=attrs,
                )
            )
            existing_xml_names.add(_canonical_argument_name(state_argument.name))
        if conflicts:
            return None, conflicts
        return replace(snapshot, arguments=tuple(updated_arguments)), []

    def _ordinary_xml_streams(
        self,
        group: Mapping[str, Any],
        workbook: WorkbookSnapshot,
        channel: str,
    ) -> list[tuple[str, ...]] | None:
        columns = [
            (tuple(key[1]), column)
            for key, column in group.get("columns", {}).items()
            if key[0] == channel
        ]
        if not columns:
            return None
        columns.sort(key=lambda item: item[0])
        streams: list[tuple[str, ...]] = []
        for path, column in columns:
            tokens: list[str] = []
            for case in workbook.cases:
                cells = case.input_cells if channel == "input" else case.output_cells
                cell = cells.get(column.key, CellValue("missing", None, None))
                raw = _cell_raw_text(cell)
                tokens.append("" if raw is None else raw)
            streams.append(tuple(tokens))
        return streams

    def _pointer_xml_streams(
        self,
        argument: ValueArgument,
        group: Mapping[str, Any],
        workbook: WorkbookSnapshot,
        function_key: str,
    ) -> tuple[list[tuple[str, ...]] | None, list[MergeConflict]]:
        bindings = tuple(group.get("bindings", ()))
        columns = tuple(group.get("columns", {}).values())
        conflicts: list[MergeConflict] = []
        streams: list[tuple[str, ...]] = []
        for binding in bindings:
            control = _find_pointer_column(columns, binding, "control", "input")
            storage = _find_pointer_column(columns, binding, "storage", "input")
            if control is None or storage is None:
                conflicts.append(
                    self._conflict(
                        "INVALID_POINTER_BINDING",
                        function_key,
                        entity_type="argument",
                        logical_argument_key=argument.name,
                        message=f"pointer {argument.name!r} is missing control/storage aliases for {binding.element_name!r}",
                    )
                )
                continue
            tokens: list[str] = []
            for case in workbook.cases:
                control_cells = case.input_cells
                control_raw = _cell_raw_text(control_cells.get(control.key, CellValue("missing", None, None)))
                storage_raw = _cell_raw_text(control_cells.get(storage.key, CellValue("missing", None, None)))
                try:
                    tokens.append(
                        normalize_pointer_binding(
                            control_raw,
                            storage_raw,
                            expected_storage=binding.expected_storage,
                        )
                    )
                except (TypeError, ValueXmlError) as exc:
                    conflicts.append(
                        self._conflict(
                            "INVALID_POINTER_BINDING",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=argument.name,
                            message=f"invalid pointer binding for {binding.element_name!r}: {exc}",
                            user={"control": control_raw, "storage": storage_raw},
                        )
                    )
                    tokens.append("")
            streams.append(tuple(tokens))
        return streams or None, conflicts

    @staticmethod
    def _pointer_output_streams(
        group: Mapping[str, Any],
        workbook: WorkbookSnapshot,
    ) -> list[tuple[str, ...]] | None:
        streams: list[tuple[str, ...]] = []
        columns = tuple(group.get("columns", {}).values())
        for binding in tuple(group.get("bindings", ())):
            column = _find_pointer_column(columns, binding, "storage", "output")
            if column is None:
                return None
            tokens: list[str] = []
            for case in workbook.cases:
                cell = case.output_cells.get(column.key, CellValue("missing", None, None))
                raw = _cell_raw_text(cell)
                tokens.append("" if raw is None else raw)
            streams.append(tuple(tokens))
        return streams or None

    def _synchronize_stubs(
        self,
        existing: Sequence[StubState],
        exported: Sequence[Any],
        function_key: str,
        *,
        required_stubs: Iterable[str] | Mapping[str, Any] | None,
        signatures: Any | None,
        signature_resolver: Any | None,
    ) -> tuple[tuple[StubState, ...] | None, list[MergeConflict], list[MergeADelta]]:
        """Merge module-level Stub Body plus source ownership/signatures."""

        conflicts: list[MergeConflict] = []
        deltas: list[MergeADelta] = []
        existing_by_name: dict[str, StubState] = {}
        existing_aliases: dict[str, str] = {}
        for state in existing:
            name = state.stub_key or state.function_name
            # A persisted conflict is not an acceptable input to a new
            # candidate, even when its signature metadata is otherwise
            # complete.  The model deliberately permits only the three
            # documented lifecycle statuses; do not silently coerce an
            # unknown/mutated value into one of them.
            if state.status == "conflict":
                conflicts.append(
                    self._conflict(
                        "STUB_STATE_CONFLICT",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=f"existing stub {name!r} is already in conflict status",
                        base=state.status,
                    )
                )
            elif state.status not in {"active", "orphaned"}:
                conflicts.append(
                    self._conflict(
                        "INVALID_STUB_STATUS",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=f"existing stub {name!r} has unsupported status {state.status!r}",
                        base=state.status,
                    )
                )
            if name in existing_by_name:
                conflicts.append(
                    self._conflict(
                        "DUPLICATE_STUB_NAME",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=f"module stub state contains duplicate {name!r}",
                    )
                )
            else:
                existing_by_name[name] = state
            # ``stub_key`` is the durable state key, while analysis/workbook
            # records commonly address the same module stub by
            # ``function_name``.  Keep both exact aliases without creating a
            # second state record.
            for alias in {state.stub_key, state.function_name}:
                if not alias:
                    continue
                previous = existing_aliases.get(alias)
                if previous is not None and previous != name:
                    conflicts.append(
                        self._conflict(
                            "DUPLICATE_STUB_NAME",
                            function_key,
                            entity_type="stub",
                            stub_key=alias,
                            message=f"module stub aliases {alias!r} identify multiple records",
                        )
                    )
                else:
                    existing_aliases[alias] = name
        exported_by_name: dict[str, Any] = {}
        for item in exported:
            name = str(getattr(item, "name", getattr(item, "stub_name", "")))
            if not name:
                continue
            if name in exported_by_name:
                conflicts.append(
                    self._conflict(
                        "DUPLICATE_STUB_NAME",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=f"workbook contains duplicate module stub {name!r}",
                    )
                )
            exported_by_name[name] = item

        required = _required_stub_names(required_stubs)
        try:
            signature_map = _stub_signature_mapping(signatures)
        except _StubSignatureResolutionError as exc:
            conflicts.append(
                self._conflict(
                    "STUB_SIGNATURE_RESOLUTION_CONFLICT",
                    function_key,
                    entity_type="stub",
                    message=str(exc),
                )
            )
            return None, conflicts, []
        source_signature_map = dict(signature_map)
        if isinstance(required_stubs, Mapping):
            source_signature_map.update(
                {
                    str(name): signature
                    for name, signature in required_stubs.items()
                    if signature not in (None, True, False)
                }
            )
        # A resolver is consulted only for unknown user-added stubs.  It is
        # allowed to return any of the documented signature representations;
        # no fallback signature is fabricated.
        result: dict[str, StubState] = {}
        matched_export_names: set[str] = set()
        for name, state in existing_by_name.items():
            export_name = next(
                (
                    alias
                    for alias in (name, state.stub_key, state.function_name)
                    if alias and alias in exported_by_name
                ),
                None,
            )
            item = exported_by_name.get(export_name) if export_name is not None else None
            if item is not None:
                matched_export_names.add(export_name)
                body = str(getattr(item, "body_raw", getattr(item, "body", "")) or "")
                updated = state
                if updated.status == "active" and function_key not in updated.owners:
                    owners = updated.owners | frozenset({function_key})
                    updated = replace(updated, owners=owners)
                    deltas.append(MergeADelta("stub", "update", "owners", None, None, name, state.owners, owners))
                if body != state.body_raw:
                    updated = replace(updated, body_raw=body, origin="manual", status="active")
                    deltas.append(MergeADelta("stub", "update", "body_raw", None, None, None, state.body_raw, body))
                # Source ownership/signature updates are explicit only.  A
                # shared module stub therefore remains one record with all
                # owners, even when several functions use it.
                source_sig = next(
                    (
                        source_signature_map.get(alias)
                        for alias in (name, state.stub_key, state.function_name, export_name)
                        if alias and source_signature_map.get(alias) is not None
                    ),
                    None,
                )
                requires_signature = (
                    updated.metadata_status != "complete"
                    or updated.return_type is None
                )
                if source_sig is None and requires_signature and signature_resolver is not None:
                    try:
                        source_sig = _resolve_stub_signature(signature_resolver, name)
                    except _StubSignatureResolutionError as exc:
                        conflicts.append(
                            self._conflict(
                                "STUB_SIGNATURE_RESOLUTION_CONFLICT",
                                function_key,
                                entity_type="stub",
                                stub_key=name,
                                message=str(exc),
                            )
                        )
                        result[name] = updated
                        continue
                if source_sig is not None:
                    try:
                        updated, sig_conflict = _merge_stub_signature(
                            updated,
                            source_sig,
                            body != state.body_raw,
                        )
                    except _StubSignatureResolutionError as exc:
                        conflicts.append(
                            self._conflict(
                                "STUB_SIGNATURE_RESOLUTION_CONFLICT",
                                function_key,
                                entity_type="stub",
                                stub_key=name,
                                message=str(exc),
                            )
                        )
                        result[name] = updated
                        continue
                    if sig_conflict is not None:
                        code, message = sig_conflict
                        conflicts.append(self._conflict(code, function_key, entity_type="stub", stub_key=name, message=message, base=state, user=body, new=source_sig))
                elif requires_signature:
                    conflicts.append(
                        self._conflict(
                            "MISSING_STUB_SIGNATURE",
                            function_key,
                            entity_type="stub",
                            stub_key=name,
                            message=f"existing stub {name!r} has incomplete signature metadata",
                        )
                    )
                result[name] = updated
                continue

            source_requires = any(
                alias in required or alias in source_signature_map
                for alias in (name, state.stub_key, state.function_name)
                if alias
            )
            if source_requires:
                conflicts.append(
                    self._conflict(
                        "REQUIRED_STUB_DELETED",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=f"required stub {name!r} is absent from complete workbook",
                    )
                )
                continue
            # If the caller gave no source inventory, generated/owned stubs
            # are conservatively considered required.  Explicit source
            # removal (an explicit required_stubs/signatures inventory is present)
            # enables lifecycle deletion/orphan behavior.
            source_inventory_supplied = required_stubs is not None or signatures is not None
            if not source_inventory_supplied and state.origin == "generated" and state.status == "active":
                conflicts.append(
                    self._conflict(
                        "REQUIRED_STUB_DELETED",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=f"required generated stub {name!r} is absent from complete workbook",
                    )
                )
                continue
            modified = state.origin != "generated" or state.status == "orphaned"
            if modified:
                orphan = replace(state, status="orphaned", owners=frozenset())
                result[name] = orphan
                deltas.append(MergeADelta("stub", "orphan", None, None, None, name, state, orphan))
            else:
                deltas.append(MergeADelta("stub", "delete", None, None, None, name, state, None))

        # User-added workbook stubs are accepted only with a resolvable known
        # signature.  Existing aliases/state records are never duplicated.
        for name, item in exported_by_name.items():
            if name in matched_export_names or name in existing_aliases:
                continue
            signature = source_signature_map.get(name) or signature_map.get(name)
            if signature is None and signature_resolver is not None:
                try:
                    signature = _resolve_stub_signature(signature_resolver, name)
                except _StubSignatureResolutionError as exc:
                    conflicts.append(
                        self._conflict(
                            "STUB_SIGNATURE_RESOLUTION_CONFLICT",
                            function_key,
                            entity_type="stub",
                            stub_key=name,
                            message=str(exc),
                        )
                    )
                    continue
            if signature is None:
                conflicts.append(
                    self._conflict(
                        "MISSING_STUB_SIGNATURE",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=f"new stub {name!r} has no known source signature",
                    )
                )
                continue
            try:
                normalized = _normalize_stub_signature(name, signature)
            except _StubSignatureResolutionError as exc:
                conflicts.append(
                    self._conflict(
                        "STUB_SIGNATURE_RESOLUTION_CONFLICT",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=str(exc),
                    )
                )
                continue
            if normalized is None:
                conflicts.append(
                    self._conflict(
                        "MISSING_STUB_SIGNATURE",
                        function_key,
                        entity_type="stub",
                        stub_key=name,
                        message=f"source signature for stub {name!r} is incomplete",
                    )
                )
                continue
            body = str(getattr(item, "body_raw", getattr(item, "body", "")) or "")
            state = StubState(
                stub_key=name,
                function_name=name,
                return_type=normalized[0],
                return_storage_name=normalized[1],
                parameters=normalized[2],
                body_raw=body,
                owners=frozenset(normalized[3]) | frozenset({function_key}),
                origin="manual",
                status="active",
                metadata_status="complete",
            )
            result[name] = state
            deltas.append(MergeADelta("stub", "add", None, None, None, name, None, state))

        if conflicts:
            return None, conflicts, []
        return tuple(result.values()), [], deltas

    def _scalar_columns(
        self,
        columns: Sequence[ExportColumn],
    ) -> tuple[list[ExportColumn], list[tuple[str, ExportColumn]]]:
        scalar: list[ExportColumn] = []
        unsupported: list[tuple[str, ExportColumn]] = []
        by_name_channel: set[tuple[str, str]] = set()
        by_name: dict[str, set[str]] = {}
        for column in columns:
            if column.element_path:
                unsupported.append(("UNSUPPORTED_ARRAY", column))
                continue
            if column.name != column.base_name:
                # A scalar name with no indices is expected to retain its
                # canonical spelling.  This branch is mostly defensive.
                unsupported.append(("UNSUPPORTED_ARRAY", column))
                continue
            key = (column.base_name, column.channel)
            if key in by_name_channel:
                unsupported.append(("DUPLICATE_ARGUMENT_COLUMN", column))
                continue
            by_name_channel.add(key)
            by_name.setdefault(column.base_name, set()).add(column.channel)
            scalar.append(column)
        for name, channels in by_name.items():
            if channels == {"input", "output"}:
                # Pairing is intentionally deferred to PR09.  Do not let a
                # scalar-only engine silently turn an INOUT pair into two
                # independent arguments.
                for column in scalar:
                    if column.base_name == name:
                        unsupported.append(("UNSUPPORTED_INOUT", column))
                scalar = [column for column in scalar if column.base_name != name]
        return scalar, unsupported

    def _validate_working_identity(
        self,
        accepted: FunctionIterationState | WorkingSnapshot,
        working: WorkingSnapshot,
        function_key: str,
    ) -> list[MergeConflict]:
        """Fail closed on malformed persisted identity before matching/allocation.

        ``WorkingSnapshot`` intentionally keeps its model validation small for
        migration compatibility, so Merge A must validate the invariants it
        relies on.  In particular, silently dropping a row-order reference or
        skipping a malformed case name would make a complete export look like
        an intentional deletion.
        """

        conflicts: list[MergeConflict] = []

        def add(
            code: str,
            message: str,
            *,
            row_id: str | None = None,
            case_name: str | None = None,
            user: Any = None,
        ) -> None:
            conflicts.append(
                self._conflict(
                    code,
                    function_key,
                    entity_type="identity",
                    row_id=row_id,
                    case_name=case_name,
                    message=message,
                    user=user,
                )
            )

        rows_by_id: dict[str, tuple[int, TestRow]] = {}
        active_ids: set[str] = set()
        seen_case_names: dict[str, str] = {}
        seen_case_numbers: dict[int, str] = {}
        max_case_number = 0

        for index, row in enumerate(working.rows):
            meta = row.meta
            if meta is None:
                add(
                    "MISSING_ROW_IDENTITY",
                    "every persisted WORKING row must carry RowMeta identity",
                    user={"row_index": index},
                )
                continue

            row_id = meta.row_id
            if not isinstance(row_id, str):
                add(
                    "INVALID_WORKING_IDENTITY",
                    f"row_id must be a canonical string, got {row_id!r}",
                    user={"row_index": index, "row_id": row_id},
                )
            else:
                try:
                    validate_row_id(row_id)
                except (IdentityError, TypeError) as exc:
                    add(
                        "INVALID_WORKING_IDENTITY",
                        f"invalid row_id {row_id!r}: {exc}",
                        row_id=row_id,
                        user={"row_index": index, "row_id": row_id},
                    )
                if row_id in rows_by_id:
                    add(
                        "INVALID_WORKING_IDENTITY",
                        f"duplicate row_id {row_id!r} in WORKING rows",
                        row_id=row_id,
                        user={"row_index": index, "previous_index": rows_by_id[row_id][0]},
                    )
                else:
                    rows_by_id[row_id] = (index, row)
                if meta.status == "active":
                    active_ids.add(row_id)

            case_name = meta.case_name
            if not isinstance(case_name, str):
                add(
                    "INVALID_WORKING_IDENTITY",
                    f"case_name must be a canonical string, got {case_name!r}",
                    row_id=row_id if isinstance(row_id, str) else None,
                    user={"row_index": index, "case_name": case_name},
                )
            else:
                try:
                    validate_case_name(case_name)
                except (IdentityError, TypeError) as exc:
                    add(
                        "INVALID_WORKING_IDENTITY",
                        f"invalid case_name {case_name!r}: {exc}",
                        row_id=row_id if isinstance(row_id, str) else None,
                        case_name=case_name,
                        user={"row_index": index, "case_name": case_name},
                    )
                if case_name in seen_case_names:
                    add(
                        "INVALID_WORKING_IDENTITY",
                        f"duplicate case_name {case_name!r} in WORKING rows",
                        row_id=row_id if isinstance(row_id, str) else None,
                        case_name=case_name,
                        user={"previous_row_id": seen_case_names[case_name]},
                    )
                else:
                    seen_case_names[case_name] = row_id if isinstance(row_id, str) else ""
                case_match = re.search(r"\.c([0-9]+)$", case_name)
                if case_match is not None:
                    number = int(case_match.group(1), 10)
                    max_case_number = max(max_case_number, number)
                    previous = seen_case_numbers.get(number)
                    if previous is not None and previous != case_name:
                        add(
                            "INVALID_WORKING_IDENTITY",
                            f"case number {number} is used by both {previous!r} and {case_name!r}",
                            row_id=row_id if isinstance(row_id, str) else None,
                            case_name=case_name,
                            user={"previous_case_name": previous, "case_number": number},
                        )
                    else:
                        seen_case_numbers[number] = case_name

        try:
            order_ids = list(working.row_order)
        except TypeError as exc:
            add(
                "INVALID_WORKING_ROW_ORDER",
                f"WORKING.row_order must be an iterable of row IDs: {exc}",
                user=working.row_order,
            )
            order_ids = []
        hashable_order_ids = [entry for entry in order_ids if isinstance(entry, str)]
        try:
            duplicate_order_ids = len(set(order_ids)) != len(order_ids)
        except TypeError:
            duplicate_order_ids = False
        if duplicate_order_ids:
            add(
                "INVALID_WORKING_ROW_ORDER",
                "WORKING.row_order contains duplicate row IDs",
                user=order_ids,
            )

        for row_id in order_ids:
            if not isinstance(row_id, str):
                add(
                    "INVALID_WORKING_ROW_ORDER",
                    f"row_order entry must be a canonical row_id, got {row_id!r}",
                    user=row_id,
                )
                continue
            try:
                validate_row_id(row_id)
            except (IdentityError, TypeError) as exc:
                add(
                    "INVALID_WORKING_ROW_ORDER",
                    f"invalid row_order row_id {row_id!r}: {exc}",
                    row_id=row_id,
                    user=row_id,
                )
            row_record = rows_by_id.get(row_id)
            if row_record is None:
                add(
                    "INVALID_WORKING_ROW_ORDER",
                    f"row_order references unknown row_id {row_id!r}",
                    row_id=row_id,
                    user=row_id,
                )
            elif row_record[1].meta is None or row_record[1].meta.status != "active":
                add(
                    "INVALID_WORKING_ROW_ORDER",
                    f"row_order references non-active row_id {row_id!r}",
                    row_id=row_id,
                    case_name=row_record[1].meta.case_name if row_record[1].meta is not None else None,
                    user=row_id,
                )

        ordered_set = set(hashable_order_ids)
        for row_id in sorted(active_ids - ordered_set):
            add(
                "INVALID_WORKING_ROW_ORDER",
                f"active row_id {row_id!r} is missing from WORKING.row_order",
                row_id=row_id,
                user=row_id,
            )

        if isinstance(accepted, FunctionIterationState):
            counter = accepted.next_case_number
            if type(counter) is not int or counter < 1:
                add(
                    "INVALID_CASE_COUNTER",
                    f"next_case_number must be a positive integer, got {counter!r}",
                    user=counter,
                )
            elif counter <= max_case_number:
                add(
                    "INVALID_CASE_COUNTER",
                    f"next_case_number {counter} is not beyond the highest accepted case number {max_case_number}",
                    user={"next_case_number": counter, "max_case_number": max_case_number},
                )
        return conflicts

    @staticmethod
    def _active_rows(working: WorkingSnapshot) -> list[tuple[int, TestRow]]:
        active_ids = set(working.row_order)
        rows_by_id = {
            row.meta.row_id: (index, row)
            for index, row in enumerate(working.rows)
            if row.meta is not None
        }
        return [
            rows_by_id[row_id]
            for row_id in working.row_order
            if row_id in active_ids
            and row_id in rows_by_id
            and rows_by_id[row_id][1].meta is not None
            and rows_by_id[row_id][1].meta.status == "active"
        ]

    def _match_rows(
        self,
        active_rows: Sequence[tuple[int, TestRow]],
        workbook_cases: Sequence[ExportCase],
        function_key: str,
        *,
        case_signature: Callable[[ExportCase], Any] | None = None,
        row_signature: Callable[[TestRow], Any] | None = None,
    ) -> tuple[dict[int, int], list[MergeConflict]]:
        """Match rows by the spec priority without first-match fallthrough."""

        matches: dict[int, int] = {}
        used: set[int] = set()
        conflicts: list[MergeConflict] = []
        blocked_workbook: set[int] = set()

        def conflict_for_case(wb_index: int, message: str, *, candidates: Sequence[int] = ()) -> None:
            case = workbook_cases[wb_index]
            conflicts.append(
                self._conflict(
                    "AMBIGUOUS_ROW_MATCH",
                    function_key,
                    entity_type="testcase",
                    row_id=None,
                    case_name=case.case_name,
                    message=message,
                    user={"row_label": case.row_label, "candidates": list(candidates)},
                )
            )
            blocked_workbook.add(wb_index)

        def assign(wb_index: int, active_position: int, stage: str) -> None:
            if wb_index in matches:
                return
            if active_position in used:
                conflict_for_case(
                    wb_index,
                    f"{stage} points to a working row already claimed by another export row",
                    candidates=(active_position,),
                )
                return
            matches[wb_index] = active_position
            used.add(active_position)

        def candidates_for(
            wb_index: int,
            predicate: Callable[[TestRow], bool],
            *,
            available_only: bool = False,
        ) -> list[int]:
            return [
                position
                for position, (_, row) in enumerate(active_rows)
                if (not available_only or position not in used)
                and predicate(row)
            ]

        # 1. Exact persistent case_name.
        for wb_index, case in enumerate(workbook_cases):
            if wb_index in blocked_workbook or not case.case_name:
                continue
            candidates = candidates_for(
                wb_index,
                lambda row, name=case.case_name: row.meta is not None and row.meta.case_name == name,
            )
            if len(candidates) == 1:
                assign(wb_index, candidates[0], "case_name")
            elif len(candidates) > 1:
                conflict_for_case(wb_index, "case_name is not unique", candidates=candidates)

        # 2. Unique external Description ID.
        for wb_index, case in enumerate(workbook_cases):
            if wb_index in blocked_workbook or wb_index in matches or not case.external_test_case_id:
                continue
            candidates = candidates_for(
                wb_index,
                lambda row, external_id=case.external_test_case_id: row.meta is not None
                and row.meta.external_test_case_id == external_id,
            )
            if len(candidates) == 1:
                assign(wb_index, candidates[0], "external_test_case_id")
            elif len(candidates) > 1:
                conflict_for_case(wb_index, "external_test_case_id is not unique", candidates=candidates)

        # 3. Exact generator obligation signature, when a caller-provided
        # normalized workbook case exposes it.  PR07 intentionally does not
        # require this optional attribute.
        for wb_index, case in enumerate(workbook_cases):
            if wb_index in blocked_workbook or wb_index in matches:
                continue
            obligations = tuple(getattr(case, "obligation_keys", ()) or ())
            if not obligations:
                continue
            expected = frozenset(obligations)
            candidates = candidates_for(
                wb_index,
                lambda row, expected=expected: row.meta is not None
                and frozenset(row.meta.obligation_keys) == expected,
                available_only=True,
            )
            if len(candidates) == 1:
                assign(wb_index, candidates[0], "obligation signature")
            elif len(candidates) > 1:
                conflict_for_case(wb_index, "generator obligation signature is not unique", candidates=candidates)

        # 4. Exact normalized scalar cell signature.
        for wb_index, case in enumerate(workbook_cases):
            if wb_index in blocked_workbook or wb_index in matches:
                continue
            signature = case_signature(case) if case_signature is not None else _workbook_case_signature(case)
            candidates = candidates_for(
                wb_index,
                lambda row, signature=signature: (
                    row_signature(row) if row_signature is not None else _row_signature(row)
                ) == signature,
                available_only=True,
            )
            if len(candidates) == 1:
                assign(wb_index, candidates[0], "cell signature")
            elif len(candidates) > 1:
                conflict_for_case(wb_index, "normalized cell signature is not unique", candidates=candidates)

        # 5/6. Unique sequence anchors and conservative positional fallback.
        anchors = sorted(matches.items())
        boundaries = [(-1, -1), *anchors, (len(workbook_cases), len(active_rows))]
        for (left_wb, left_work), (right_wb, right_work) in zip(boundaries, boundaries[1:]):
            wb_gap = [
                index
                for index in range(left_wb + 1, right_wb)
                if index not in matches and index not in blocked_workbook
            ]
            work_gap = [
                position
                for position in range(left_work + 1, right_work)
                if position not in used
            ]
            if not wb_gap or not work_gap:
                continue
            if len(wb_gap) == 1 and len(work_gap) == 1:
                assign(wb_gap[0], work_gap[0], "unique sequence anchor")
            else:
                for wb_index in wb_gap:
                    conflict_for_case(
                        wb_index,
                        "multiple rows remain equally plausible after exact anchors",
                        candidates=work_gap,
                    )

        # If there are no anchors, a single row on each side is the only
        # positional fallback that is provably unique.  More than one row is
        # deliberately left as an ambiguity rather than zipped by position.
        unmatched_wb = [
            index
            for index in range(len(workbook_cases))
            if index not in matches and index not in blocked_workbook
        ]
        unmatched_work = [position for position in range(len(active_rows)) if position not in used]
        if unmatched_wb and unmatched_work and len(unmatched_wb) == len(unmatched_work) == 1:
            assign(unmatched_wb[0], unmatched_work[0], "unique positional fallback")
            unmatched_wb = [
                index
                for index in range(len(workbook_cases))
                if index not in matches and index not in blocked_workbook
            ]
            unmatched_work = [position for position in range(len(active_rows)) if position not in used]
        if unmatched_wb and unmatched_work:
            for wb_index in unmatched_wb:
                conflict_for_case(
                    wb_index,
                    "row matching is ambiguous without a unique anchor",
                    candidates=unmatched_work,
                )
        return matches, conflicts

    def _plan_arguments(
        self,
        working: WorkingSnapshot,
        columns: Sequence[ExportColumn],
        resolver: Any | None,
        function_key: str,
        *,
        required_arguments: Iterable[str] | None,
    ) -> tuple[dict[str, Any], list[MergeConflict]]:
        conflicts: list[MergeConflict] = []
        existing: dict[str, list[ArgumentState]] = {}
        for argument in working.arguments:
            existing.setdefault(argument.name, []).append(argument)

        explicit_required = set(required_arguments or ())
        plan: dict[str, Any] = {
            "columns": list(columns),
            "existing": existing,
            "new": [],
            "deleted": [],
            "workbook_names": {column.name for column in columns},
        }

        # A single scalar column name is a logical argument in PR08.  Arrays,
        # pointers, and inout are rejected earlier and never enter this plan.
        for column in columns:
            candidates = existing.get(column.name, [])
            if len(candidates) > 1:
                conflicts.append(
                    self._conflict(
                        "AMBIGUOUS_ARGUMENT_DIRECTION",
                        function_key,
                        entity_type="argument",
                        logical_argument_key=column.name,
                        message=f"more than one working argument matches {column.name!r}",
                        user=column.name,
                    )
                )
                continue
            if candidates:
                argument = candidates[0]
                if argument.direction not in _SCALAR_DIRECTIONS:
                    conflicts.append(
                        self._conflict(
                            "UNSUPPORTED_ARGUMENT_SCHEMA",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=column.name,
                            message=f"argument {column.name!r} is not scalar",
                            user=column.name,
                        )
                    )
                    continue
                expected_channel = "input" if argument.direction == "in" else "output"
                if column.channel != expected_channel:
                    conflicts.append(
                        self._conflict(
                            "AMBIGUOUS_ARGUMENT_DIRECTION",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=column.name,
                            message=f"workbook channel {column.channel!r} conflicts with {argument.direction!r}",
                            user=column.name,
                        )
                    )
                if argument.fmt is None or argument.fmt == "":
                    conflicts.append(
                        self._conflict(
                            "MISSING_ARGUMENT_FORMAT",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=column.name,
                            message=f"existing argument {column.name!r} lacks format metadata",
                            user=column.name,
                        )
                    )
                if argument.xml_len is None or argument.xml_len == "":
                    conflicts.append(
                        self._conflict(
                            "MISSING_ARGUMENT_LENGTH",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=column.name,
                            message=f"existing argument {column.name!r} lacks XML length metadata",
                            user=column.name,
                        )
                    )
                if argument.pointer is not False:
                    conflicts.append(
                        self._conflict(
                            "MISSING_POINTER_METADATA" if argument.pointer is None else "UNSUPPORTED_POINTER",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=column.name,
                            message=f"argument {column.name!r} is not a known scalar non-pointer",
                            user=column.name,
                        )
                    )
                if argument.dimensions:
                    conflicts.append(
                        self._conflict(
                            "UNSUPPORTED_ARRAY",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=column.name,
                            message=f"argument {column.name!r} has array dimensions",
                            user=column.name,
                        )
                    )
                continue

            metadata = _resolve_argument_metadata(resolver, column.name, column)
            normalized, metadata_errors = _normalize_new_metadata(metadata, column)
            if metadata_errors:
                for code, message in metadata_errors:
                    conflicts.append(
                        self._conflict(
                            code,
                            function_key,
                            entity_type="argument",
                            logical_argument_key=column.name,
                            message=message,
                            user=column.name,
                        )
                    )
            else:
                plan["new"].append(normalized)

        # Missing workbook columns are complete-export deletions.  Only scalar
        # arguments are owned by this PR; later PRs own arrays/pointers/inout.
        for name, arguments in existing.items():
            for argument in arguments:
                if argument.direction not in _SCALAR_DIRECTIONS:
                    continue
                if argument.pointer is not False or argument.dimensions:
                    continue
                if name in plan["workbook_names"]:
                    continue
                is_required = argument.source == "generated" or name in explicit_required
                if is_required:
                    conflicts.append(
                        self._conflict(
                            "REQUIRED_ARGUMENT_DELETED",
                            function_key,
                            entity_type="argument",
                            logical_argument_key=name,
                            message=f"required argument {name!r} is absent from complete workbook",
                            user=None,
                        )
                    )
                else:
                    plan["deleted"].append(argument)
        return plan, conflicts

    def _apply_argument_plan(
        self,
        working: WorkingSnapshot,
        plan: Mapping[str, Any],
        function_key: str,
    ) -> tuple[WorkingSnapshot, list[MergeADelta]]:
        deltas: list[MergeADelta] = []
        deleted_names = {argument.name for argument in plan["deleted"]}
        arguments = [argument for argument in working.arguments if argument.name not in deleted_names]
        next_order = max((argument.original_order for argument in arguments), default=-1) + 1
        for argument in plan["new"]:
            key = argument.key
            if any(existing.key == key or existing.name == argument.name for existing in arguments):
                continue
            argument = replace(argument, original_order=next_order)
            next_order += 1
            arguments.append(argument)
            deltas.append(
                MergeADelta(
                    "argument",
                    "add",
                    None,
                    None,
                    None,
                    argument.name,
                    None,
                    argument,
                )
            )
        tombstones = list(working.tombstones)
        existing_tombstones = {(item.entity_type, item.entity_key) for item in tombstones}
        for argument in plan["deleted"]:
            key = ("argument", argument.name)
            if key not in existing_tombstones:
                tombstones.append(Tombstone("argument", argument.name, working.generation))
            deltas.append(
                MergeADelta(
                    "argument",
                    "delete",
                    None,
                    None,
                    None,
                    argument.name,
                    argument,
                    None,
                )
            )
        return replace(working, arguments=tuple(arguments), tombstones=tuple(tombstones)), deltas

    def _update_existing_row(
        self,
        row: TestRow,
        export_case: ExportCase,
        columns: Sequence[ExportColumn],
        argument_plan: Mapping[str, Any],
    ) -> tuple[TestRow, list[MergeADelta]]:
        values = deepcopy(row.values)
        expected = deepcopy(row.expected)
        deltas: list[MergeADelta] = []
        for column in columns:
            cells = export_case.input_cells if column.channel == "input" else export_case.output_cells
            cell = cells.get(column.key, CellValue("missing", None, None))
            target = values if column.channel == "input" else expected
            existing_key = _find_row_key(target, column)
            key = existing_key if existing_key is not None else column.name
            before = target.get(key, _MISSING)
            after = cell
            if not _field_equal(before, after):
                target[key] = after
                deltas.append(
                    MergeADelta(
                        "cell",
                        "update",
                        "input" if column.channel == "input" else "expected",
                        row.meta.row_id if row.meta else None,
                        row.meta.case_name if row.meta else None,
                        column.name,
                        None if before is _MISSING else before,
                        after,
                    )
                )

        # Complete-export absence is a deletion for scalar arguments.  A blank
        # cell remains a CellValue and therefore never triggers this branch.
        workbook_names = {column.name for column in columns}
        for argument in argument_plan.get("deleted", ()):
            if argument.name in workbook_names:
                continue
            for mapping, field in ((values, "input"), (expected, "expected")):
                for key in list(mapping):
                    if _row_key_name(key) == argument.name:
                        before = mapping.pop(key)
                        deltas.append(
                            MergeADelta(
                                "cell",
                                "delete",
                                field,
                                row.meta.row_id if row.meta else None,
                                row.meta.case_name if row.meta else None,
                                argument.name,
                                before,
                                None,
                            )
                        )

        description = export_case.description_raw
        meta = row.meta
        if meta is not None:
            if row.description != description:
                deltas.append(
                    MergeADelta(
                        "testcase",
                        "update",
                        "description_raw",
                        meta.row_id,
                        meta.case_name,
                        None,
                        row.description,
                        description,
                    )
                )
            if meta.external_test_case_id != export_case.external_test_case_id:
                deltas.append(
                    MergeADelta(
                        "testcase",
                        "update",
                        "external_test_case_id",
                        meta.row_id,
                        meta.case_name,
                        None,
                        meta.external_test_case_id,
                        export_case.external_test_case_id,
                    )
                )
                meta = replace(meta, external_test_case_id=export_case.external_test_case_id)
        return replace(row, values=values, expected=expected, description=description, meta=meta), deltas

    def _new_manual_row(
        self,
        export_case: ExportCase,
        function_key: str,
        generation: int,
        snapshot_id: str,
        existing_row_ids: set[str],
        existing_case_names: set[str],
        next_case_number: int,
        source_file: str,
    ) -> tuple[TestRow, str, str, int]:
        del snapshot_id
        row_id = self._allocate_row_id(existing_row_ids)
        case_name, next_case_number = self._allocate_case_name(
            source_file,
            next_case_number,
            existing_case_names,
        )
        values = {
            column.name: export_case.input_cells.get(column.key, CellValue("missing", None, None))
            for column in working_columns_for_case(export_case, "input")
        }
        expected = {
            column.name: export_case.output_cells.get(column.key, CellValue("missing", None, None))
            for column in working_columns_for_case(export_case, "output")
        }
        meta = RowMeta(
            row_id=row_id,
            case_name=case_name,
            external_test_case_id=export_case.external_test_case_id,
            origin="manual",
            status="active",
            generation_created=generation,
            generation_modified=generation,
            function_key=function_key,
            obligation_keys=(),
        )
        row = TestRow(
            values=values,
            kind=export_case.description_type or "manual",
            description=export_case.description_raw,
            intent={},
            expected=expected,
            meta=meta,
        )
        return row, row_id, case_name, next_case_number

    def _allocate_row_id(self, existing_row_ids: set[str]) -> str:
        return allocate_row_id(existing_row_ids, uuid_factory=self.uuid_factory)

    def _allocate_case_name(
        self,
        source_file: str,
        next_case_number: int,
        existing_case_names: set[str],
    ) -> tuple[str, int]:
        allocation = allocate_case_name(
            source_file,
            next_case_number,
            existing_case_names,
            on_collision="error",
        )
        return allocation.case_name, allocation.next_case_number

    @staticmethod
    def _next_case_number(accepted: FunctionIterationState | WorkingSnapshot, existing_case_names: set[str]) -> int:
        if isinstance(accepted, FunctionIterationState):
            return accepted.next_case_number
        numbers = []
        for name in existing_case_names:
            match = re.search(r"\.c([0-9]+)$", name)
            if match:
                numbers.append(int(match.group(1)) + 1)
        return max(numbers or [1])

    @staticmethod
    def _conflict(
        code: str,
        function_key: str,
        *,
        entity_type: str,
        message: str,
        row_id: str | None = None,
        case_name: str | None = None,
        logical_argument_key: str | None = None,
        stub_key: str | None = None,
        base: Any = None,
        user: Any = None,
        new: Any = None,
    ) -> MergeConflict:
        return MergeConflict(
            code=code,
            severity="blocking",
            entity_type=entity_type,
            function_key=function_key,
            row_id=row_id,
            case_name=case_name,
            logical_argument_key=logical_argument_key,
            stub_key=stub_key,
            base=base,
            user=user,
            new=new,
            message=message,
            # Merge A consumes one complete TESSY export into WORKING.  Its
            # blockers are import/schema/identity diagnostics, not a
            # BASE/USER/NEW three-way value conflict.  Keep the resolution
            # set empty until a real Merge A conflict model can prove a
            # deterministic user/new choice.
            allowed_resolutions=(),
        )


# Complex array/pointer projection helpers are defined in this module.

def _required_stub_names(value: Iterable[str] | Mapping[str, Any] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, Mapping):
        return {str(key) for key in value}
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


def _stub_signature_mapping(value: Any | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        try:
            return {str(key): item for key, item in value.items()}
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"source stub-signature mapping could not be read: {exc}"
            ) from exc
    try:
        stubs = getattr(value, "stubs")
    except AttributeError:
        stubs = _MISSING
    except Exception as exc:
        raise _StubSignatureResolutionError(
            f"source stub-signature provider failed while reading stubs: {exc}"
        ) from exc
    if stubs is not _MISSING:
        return _stub_signature_mapping(stubs)
    try:
        called_functions = getattr(value, "called_functions")
    except AttributeError:
        called_functions = _MISSING
    except Exception as exc:
        raise _StubSignatureResolutionError(
            f"source stub-signature provider failed while reading called_functions: {exc}"
        ) from exc
    if called_functions is not _MISSING:
        return _stub_signature_mapping(called_functions)
    if isinstance(value, (str, bytes, bytearray)):
        return {}
    try:
        items = tuple(value)
    except TypeError:
        try:
            name = getattr(value, "name", getattr(value, "function_name", None))
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"source stub-signature provider failed while reading a signature: {exc}"
            ) from exc
        return {str(name): value} if name else {}
    except Exception as exc:
        raise _StubSignatureResolutionError(
            f"source stub-signature provider failed while iterating signatures: {exc}"
        ) from exc
    result: dict[str, Any] = {}
    for item in items:
        try:
            name = getattr(item, "name", getattr(item, "function_name", getattr(item, "stub_key", None)))
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"source stub-signature provider returned an unreadable record: {exc}"
            ) from exc
        if name:
            result[str(name)] = item
    return result


def _resolve_stub_signature(resolver: Any, name: str) -> Any | None:
    if isinstance(resolver, Mapping):
        try:
            return resolver.get(name)
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"stub signature mapping failed for {name!r}: {exc}"
            ) from exc
    try:
        find = getattr(resolver, "find", None)
    except Exception as exc:
        raise _StubSignatureResolutionError(
            f"stub signature resolver failed while reading find(): {exc}"
        ) from exc
    if callable(find):
        try:
            return find(name)
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"stub signature resolver find({name!r}) failed: {exc}"
            ) from exc
    try:
        resolve = getattr(resolver, "resolve", None)
    except Exception as exc:
        raise _StubSignatureResolutionError(
            f"stub signature resolver failed while reading resolve(): {exc}"
        ) from exc
    if callable(resolve):
        try:
            return resolve(name)
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"stub signature resolver resolve({name!r}) failed: {exc}"
            ) from exc
    if callable(resolver):
        try:
            return resolver(name)
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"stub signature resolver call({name!r}) failed: {exc}"
            ) from exc
    return None


def _normalize_stub_signature(name: str, signature: Any) -> tuple[str | None, str | None, tuple[ModelStubParameter, ...], tuple[str, ...]] | None:
    if isinstance(signature, StubState):
        if signature.metadata_status != "complete" or signature.return_type is None:
            return None
        return signature.return_type, signature.return_storage_name, tuple(signature.parameters), tuple(signature.owners)

    def attr(*names: str, default: Any = None) -> Any:
        try:
            return _first_attr(signature, names, default=default)
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"stub signature {name!r} failed while reading {names[0]!r}: {exc}"
            ) from exc

    return_type = attr("return_type", "ret_type", default=None)
    return_storage = attr("return_storage_name", "return_name", "ret_storage_name", default=None)
    try:
        return_info = getattr(signature, "return_info", None)
    except Exception as exc:
        raise _StubSignatureResolutionError(
            f"stub signature {name!r} failed while reading return_info: {exc}"
        ) from exc
    if callable(return_info):
        try:
            info = return_info()
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"stub signature {name!r} failed while reading return_info(): {exc}"
            ) from exc
        if info is not None:
            try:
                return_type = getattr(info, "type_name", return_type)
                return_storage = getattr(info, "name", return_storage)
            except Exception as exc:
                raise _StubSignatureResolutionError(
                    f"stub signature {name!r} returned unreadable return info: {exc}"
                ) from exc
    ret_ctype = attr("ret_ctype", default=None)
    ret_struct = attr("ret_struct", default=None)
    if return_type is None and ret_ctype is not None:
        try:
            return_type = getattr(ret_ctype, "name", str(ret_ctype))
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"stub signature {name!r} returned unreadable return type: {exc}"
            ) from exc
    if return_type is None and ret_struct is not None:
        return_type = str(ret_struct)
    parameters_raw = attr("parameters", "params", "arguments", default=_MISSING)
    if parameters_raw is _MISSING or parameters_raw is None:
        return None
    try:
        parameters_items = tuple(parameters_raw)
    except TypeError:
        return None
    except Exception as exc:
        raise _StubSignatureResolutionError(
            f"stub signature {name!r} failed while reading parameters: {exc}"
        ) from exc
    parameters: list[ModelStubParameter] = []
    for parameter in parameters_items:
        if isinstance(parameter, ModelStubParameter):
            parameters.append(parameter)
            continue
        if isinstance(parameter, Mapping):
            type_name = parameter.get("type_name", parameter.get("type", parameter.get("typeName")))
            parameter_name = parameter.get("name")
        elif isinstance(parameter, (tuple, list)) and len(parameter) >= 2:
            type_name, parameter_name = parameter[0], parameter[1]
        else:
            try:
                type_name = getattr(parameter, "type_name", getattr(parameter, "type", None))
                parameter_name = getattr(parameter, "name", None)
            except Exception as exc:
                raise _StubSignatureResolutionError(
                    f"stub signature {name!r} returned unreadable parameter metadata: {exc}"
                ) from exc
        if type_name is None or parameter_name is None:
            return None
        try:
            parameters.append(ModelStubParameter(str(type_name), str(parameter_name)))
        except (TypeError, ValueError):
            return None
    owners_raw = attr("owners", default=())
    if isinstance(owners_raw, str):
        owners = (owners_raw,)
    else:
        try:
            owners = tuple(str(item) for item in owners_raw)
        except TypeError:
            owners = ()
        except Exception as exc:
            raise _StubSignatureResolutionError(
                f"stub signature {name!r} returned unreadable owners: {exc}"
            ) from exc
    # ``void`` is a valid known signature.  A missing return type is not.
    if return_type is None:
        return None
    return str(return_type), None if return_storage is None else str(return_storage), tuple(parameters), owners


def _merge_stub_signature(
    state: StubState,
    signature: Any,
    body_modified: bool,
) -> tuple[StubState, tuple[str, str] | None]:
    normalized = _normalize_stub_signature(state.stub_key, signature)
    if normalized is None:
        return state, ("MISSING_STUB_SIGNATURE", "source stub signature is incomplete")
    return_type, return_storage, parameters, owners = normalized
    old_signature = (state.return_type, state.return_storage_name, tuple(state.parameters))
    new_signature = (return_type, return_storage, tuple(parameters))
    if body_modified and old_signature != new_signature:
        return state, (
            "STUB_BODY_SIGNATURE_CONFLICT",
            "stub body and source signature changed concurrently",
        )
    updated_owners = state.owners | frozenset(owners)
    return replace(
        state,
        return_type=return_type,
        return_storage_name=return_storage,
        parameters=tuple(parameters),
        owners=updated_owners,
        metadata_status="complete",
    ), None


def _resolve_argument_metadata(
    resolver: Any | None,
    name: str,
    column: ExportColumn,
    *,
    declared_name: str | None = None,
) -> Any | None:
    if resolver is None:
        return None
    candidate_names: list[str] = []
    for candidate in (
        declared_name,
        name,
        getattr(column, "base_name", name),
        getattr(column, "name", name),
    ):
        if candidate is None:
            continue
        text = str(candidate)
        if text not in candidate_names:
            candidate_names.append(text)
    if isinstance(resolver, Mapping):
        for key in (*candidate_names, column.key):
            try:
                if key in resolver:
                    return resolver[key]
            except TypeError:
                continue
        # Last-resort normalized key lookup is exact with respect to member
        # and local-static suffixes; only array indices are removed.
        normalized = _canonical_argument_name(name)
        for key, value in resolver.items():
            if isinstance(key, str) and _canonical_argument_name(key) == normalized:
                return value
        return None
    # ``Inventory`` is intentionally a lightweight dataclass and does not
    # guarantee a ``find`` method.  Resolve against its complete variable
    # list before falling through to callable/provider APIs.  Exact declared
    # array spellings are tried first, so ``a[2]`` is never replaced by the
    # first physical leaf spelling ``a[1]``.
    variables = getattr(resolver, "variables", None)
    if variables is not None:
        for variable in variables:
            variable_name = getattr(variable, "name", None)
            if variable_name is not None and str(variable_name) in candidate_names:
                return variable
        target = _canonical_argument_name(declared_name or name)
        target_rank = len(re.findall(r"\[[^\]]*\]", declared_name or name))
        for variable in variables:
            variable_name = getattr(variable, "name", None)
            if variable_name is None:
                continue
            variable_text = str(variable_name)
            if (
                _canonical_argument_name(variable_text) == target
                and len(re.findall(r"\[[^\]]*\]", variable_text)) == target_rank
            ):
                return variable
    resolve = getattr(resolver, "resolve", None)
    if callable(resolve):
        for candidate in candidate_names:
            result = _call_resolver(resolve, candidate, column)
            if result is not None:
                return result
        return None
    find = getattr(resolver, "find", None)
    if callable(find):
        for candidate in candidate_names:
            try:
                result = find(candidate)
            except TypeError:
                result = None
            if result is not None:
                return result
        return None
    if callable(resolver):
        for candidate in candidate_names:
            result = _call_resolver(resolver, candidate, column)
            if result is not None:
                return result
        return None
    return None


def _call_resolver(resolver: Callable[..., Any], name: str, column: ExportColumn) -> Any | None:
    try:
        signature = inspect.signature(resolver)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        if len(positional) <= 1:
            return resolver(name)
    return resolver(name, column)


def _normalize_new_metadata(
    metadata: Any | None,
    column: ExportColumn,
) -> tuple[ArgumentState | None, list[tuple[str, str]]]:
    errors: list[tuple[str, str]] = []
    if metadata is None:
        return None, [
            ("MISSING_ARGUMENT_FORMAT", f"new argument {column.name!r} has no explicit format metadata"),
            ("MISSING_ARGUMENT_LENGTH", f"new argument {column.name!r} has no explicit XML length metadata"),
            ("MISSING_POINTER_METADATA", f"new argument {column.name!r} has no explicit pointer metadata"),
        ]
    if isinstance(metadata, ArgumentState):
        argument = deepcopy(metadata)
    else:
        value = lambda *names, default=None: _first_attr(metadata, names, default=default)
        name = value("name", default=column.name)
        direction = _normalize_direction(value("direction", default=column.direction))
        fmt = value("fmt", "format", default=None)
        xml_len = value("xml_len", "len", "length", default=None)
        pointer = value("pointer", "is_pointer", default=None)
        testmethod = value("testmethod", "test_method", default=None)
        dimensions = tuple(value("dimensions", default=()) or ())
        source = value("source", default="generated")
        metadata_status = value("metadata_status", default="complete")
        range_encoding = value("range_encoding", default="scalar")
        output_encoding = value(
            "output_encoding",
            default="scalar" if direction == "out" else "absent",
        )
        try:
            argument = ArgumentState(
                LogicalArgumentKey(str(name), 0),
                str(name),
                direction,
                fmt,
                None if xml_len is None else str(xml_len),
                pointer,
                testmethod,
                dimensions,
                range_encoding,
                output_encoding,
                source,
                metadata_status,
                0,
            )
        except (TypeError, ValueError) as exc:
            return None, [("MISSING_ARGUMENT_METADATA", f"invalid metadata for new argument {column.name!r}: {exc}")]

    if argument.name != column.name:
        errors.append(("AMBIGUOUS_ARGUMENT_DIRECTION", f"resolver metadata names {argument.name!r}, workbook column is {column.name!r}"))
    if argument.key.canonical_xml_name != column.name or argument.key.occurrence != 0:
        errors.append(("AMBIGUOUS_ARGUMENT_DIRECTION", f"resolver logical key {argument.key!r} does not identify workbook column {column.name!r}"))
    if argument.direction != column.direction:
        errors.append(("AMBIGUOUS_ARGUMENT_DIRECTION", f"resolver direction {argument.direction!r} conflicts with workbook {column.direction!r}"))
    if argument.fmt is None or argument.fmt == "":
        errors.append(("MISSING_ARGUMENT_FORMAT", f"new argument {column.name!r} lacks format metadata"))
    if argument.xml_len is None or argument.xml_len == "":
        errors.append(("MISSING_ARGUMENT_LENGTH", f"new argument {column.name!r} lacks XML length metadata"))
    if argument.pointer is None:
        errors.append(("MISSING_POINTER_METADATA", f"new argument {column.name!r} lacks pointer metadata"))
    elif argument.pointer:
        errors.append(("UNSUPPORTED_POINTER", f"new argument {column.name!r} is a pointer; PR09 owns it"))
    if argument.dimensions:
        errors.append(("UNSUPPORTED_ARRAY", f"new argument {column.name!r} has array dimensions; PR09 owns it"))
    if argument.metadata_status != "complete":
        errors.append(("MISSING_ARGUMENT_METADATA", f"new argument {column.name!r} metadata is not complete"))
    return (None if errors else argument), errors


# Generic metadata helpers are defined in this module.


def _workbook_case_signature(case: ExportCase) -> tuple[tuple[str, str, tuple[int, ...], tuple[Any, ...]], ...]:
    entries: list[tuple[str, str, tuple[int, ...], tuple[Any, ...]]] = []
    for column_cells, channel in ((case.input_cells, "input"), (case.output_cells, "output")):
        for key, cell in column_cells.items():
            entries.append((key.canonical_name, channel, key.element_path, _cell_token(cell)))
    return tuple(sorted(entries, key=repr))


def _row_signature(row: TestRow) -> tuple[tuple[str, str, tuple[int, ...], tuple[Any, ...]], ...]:
    entries: list[tuple[str, str, tuple[int, ...], tuple[Any, ...]]] = []
    for mapping, channel in ((row.values, "input"), (row.expected, "output")):
        for key, value in mapping.items():
            name, element_path = _row_key_parts(key)
            entries.append((name, channel, element_path, _cell_token(value)))
    return tuple(sorted(entries, key=repr))


def _find_row_key(mapping: Mapping[Any, Any], column: ExportColumn) -> Any | None:
    for key in mapping:
        name, element_path = _row_key_parts(key)
        if name == column.base_name and element_path == column.element_path:
            return key
    return None


def _field_equal(before: Any, after: CellValue) -> bool:
    if before is _MISSING:
        return False
    if isinstance(before, CellValue):
        return before == after
    if after.kind == "missing":
        return False
    if after.kind == "explicit_empty":
        # A primitive empty string does not prove that the source cell was
        # physically present.  Promote it to CellValue so an explicit empty
        # remains distinguishable from a missing cell on future cycles.
        return False
    # A primitive generated value can be reused when it has the exact semantic
    # and ordinary raw spelling; otherwise promote it to CellValue to retain
    # the user's lexeme.
    if before == after.semantic_value and str(before) == (after.raw_text or ""):
        return True
    # TESSY exports ordinary numeric cells as text in the current dialect;
    # compare that canonical spelling with an existing primitive numeric
    # generated value without losing the workbook lexeme when it differs.
    if isinstance(before, (int, float)) and not isinstance(before, bool):
        if str(before) == (after.raw_text or ""):
            return True
    return False


def working_columns_for_case(case: ExportCase, channel: str) -> list[ExportColumn]:
    cells = case.input_cells if channel == "input" else case.output_cells
    # PR07 keys carry the physical column identity; this lightweight view is
    # sufficient for new row construction and does not implement any merge
    # policy.
    result: list[ExportColumn] = []
    for key in cells:
        result.append(
            ExportColumn(
                key=key,
                name=key.canonical_name,
                direction="in" if channel == "input" else "out",
                channel=channel,
                raw_direction="i" if channel == "input" else "o",
                raw_type_hint=None,
                element_path=key.element_path,
                base_name=key.canonical_name,
                source_column=0,
            )
        )
    return result


# Functional facade retaining the PR08 entry point while exposing the same
# explicit PR09 contract as ``MergeAEngine.merge``.
def merge_a(
    accepted: FunctionIterationState | WorkingSnapshot,
    workbook: WorkbookSnapshot,
    resolver: Any | None = None,
    *,
    argument_resolver: Any | None = None,
    inventory: Any | None = None,
    required_arguments: Iterable[str] | None = None,
    function_key: str | None = None,
    source_file: str | None = None,
    value_snapshot: FunctionSnapshot | None = None,
    module_stub_state: Iterable[StubState] | None = None,
    required_stubs: Iterable[str] | Mapping[str, Any] | None = None,
    stub_signatures: Any | None = None,
    stub_signature_resolver: Any | None = None,
    whole_struct_bases: Iterable[str] | Mapping[str, bool] | None = None,
) -> MergeAResult:
    return MergeAEngine().merge(
        accepted,
        workbook,
        resolver=resolver,
        argument_resolver=argument_resolver,
        inventory=inventory,
        required_arguments=required_arguments,
        function_key=function_key,
        source_file=source_file,
        value_snapshot=value_snapshot,
        module_stub_state=module_stub_state,
        required_stubs=required_stubs,
        stub_signatures=stub_signatures,
        stub_signature_resolver=stub_signature_resolver,
        whole_struct_bases=whole_struct_bases,
    )


__all__ = [
    "MergeADelta",
    "MergeAEngine",
    "MergeAResult",
    "ScalarArgumentMetadata",
    "merge_a",
]
