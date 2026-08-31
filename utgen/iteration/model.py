"""Strict iteration-domain records and canonical JSON serialization.

The iteration package is deliberately separate from the generation models.  It
owns the normalized records required by the state boundary, while the existing
``utgen.model.TestRow`` remains the generator's row type and is supported by
the serializer without defining a second TestRow class.

PR02 intentionally did not define PointerState or Orphan business records.
Pointer metadata is represented by ``ArgumentState.pointer`` and orphan status
by the existing status fields.  PR05 adds the fixed three-field Tombstone
record needed by persisted working snapshots; it still does not define a
separate PointerState or Orphan model.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
import copy
from datetime import datetime
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, TypeVar

from utgen.model import TestRow


CURRENT_SCHEMA_VERSION = 2

_DIRECTIONS = frozenset({"in", "out", "inout"})
_ROW_ORIGINS = frozenset({"generated", "manual", "merged"})
_ROW_STATUSES = frozenset({"active", "orphaned", "superseded", "conflict"})
_STUB_STATUSES = frozenset({"active", "orphaned", "conflict"})
_METADATA_STATUSES = frozenset({"complete", "unresolved", "conflict"})
_CHANNELS = frozenset({"input", "output"})
_RANGE_ENCODINGS = frozenset({"scalar", "shared", "expanded"})
_OUTPUT_ENCODINGS = frozenset({"absent", "scalar", "shared", "expanded"})
_SEVERITIES = frozenset({"blocking", "warning"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ANALYSIS_ID = re.compile(r"analysis_v1_[0-9a-f]{64}\Z")
_SNAPSHOT_ID = re.compile(r"snap_[0-9a-f]{32}\Z")
_REVISION_ID = re.compile(r"rev_[0-9a-f]{32}\Z")
_OPERATION_ID = re.compile(r"op_[0-9a-f]{32}\Z")
_WINDOWS_RESERVED_COMPONENTS = frozenset({"CON", "PRN", "AUX", "NUL", "CLOCK$"})
_WINDOWS_RESERVED_NUMBER = re.compile(r"(?:COM|LPT)[1-9]\Z", re.IGNORECASE)
# Decision IDs are represented as canonical decimal integers in JSON object
# keys.  In particular, leading zeroes and negative zero are not canonical.
_INTENT_INTEGER_KEY = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_CANONICAL_TIMESTAMP = re.compile(
    r"^(?P<local>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})\Z"
)
_MISSING = object()


class ModelError(ValueError):
    """Base error for invalid domain records or serialized state."""


class UnknownFieldError(ModelError):
    """Raised when a model receives a key outside its declared schema."""


class SchemaVersionError(ModelError):
    """Raised when a serialized state has no valid schema version."""


class UnsupportedSchemaError(SchemaVersionError):
    """Raised for schema versions not supported by the current reader."""


def _canonicalize_timestamp(value: Any) -> str:
    """Validate and canonicalize the accepted second-precision timestamp form."""

    if type(value) is not str:
        raise _type_error("updated_at", "a string", value)
    match = _CANONICAL_TIMESTAMP.fullmatch(value)
    if match is None:
        raise ModelError(
            "updated_at must use YYYY-MM-DDTHH:MM:SS±HH:MM or UTC Z form "
            "without fractional seconds"
        )
    local = match.group("local")
    zone = match.group("zone")
    try:
        # strptime deliberately rejects impossible dates and leap seconds.
        datetime.strptime(local, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ModelError(f"updated_at is not a valid timestamp: {value!r}") from exc
    if zone == "Z":
        return f"{local}Z"
    offset_hours = int(zone[1:3])
    offset_minutes = int(zone[4:6])
    if offset_hours > 23 or offset_minutes > 59:
        raise ModelError(f"updated_at has an invalid timezone offset: {value!r}")
    if offset_hours == 0 and offset_minutes == 0:
        return f"{local}Z"
    return f"{local}{zone}"


def _type_error(field_name: str, expected: str, value: Any) -> TypeError:
    return TypeError(f"{field_name} must be {expected}, got {type(value).__name__}")


def _require_string(value: Any, field_name: str, *, nonempty: bool = False) -> str:
    if type(value) is not str:
        raise _type_error(field_name, "a string", value)
    if nonempty and not value:
        raise ModelError(f"{field_name} must not be empty")
    return value


def _require_int(value: Any, field_name: str, *, nonnegative: bool = False) -> int:
    if type(value) is not int:
        raise _type_error(field_name, "an integer", value)
    if nonnegative and value < 0:
        raise ModelError(f"{field_name} must be non-negative")
    return value


def _require_bool_or_none(value: Any, field_name: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise _type_error(field_name, "a boolean or null", value)
    return value


def _require_optional_string(value: Any, field_name: str) -> str | None:
    if value is not None and type(value) is not str:
        raise _type_error(field_name, "a string or null", value)
    return value


def _require_enum(value: Any, field_name: str, allowed: frozenset[str]) -> str:
    value = _require_string(value, field_name)
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ModelError(f"{field_name} has unsupported value {value!r}; expected one of {choices}")
    return value


def _require_tuple(value: Any, field_name: str) -> tuple:
    if type(value) is not tuple:
        raise _type_error(field_name, "a tuple", value)
    return value


def _require_frozenset(value: Any, field_name: str) -> frozenset:
    if type(value) is not frozenset:
        raise _type_error(field_name, "a frozenset", value)
    return value


def _require_json_array(value: Any, field_name: str) -> list[Any]:
    if type(value) is not list:
        raise _type_error(field_name, "a JSON array", value)
    return value


def _tuple_from_json(value: Any, field_name: str) -> tuple:
    return tuple(_require_json_array(value, field_name))


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _type_error(field_name, "an object", value)
    for key in value:
        if type(key) is not str:
            raise ModelError(f"{field_name} contains a non-string key")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: frozenset[str], context: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise UnknownFieldError(f"{context} contains unknown field(s): {', '.join(unknown)}")


def _required(data: Mapping[str, Any], name: str, context: str) -> Any:
    value = data.get(name, _MISSING)
    if value is _MISSING:
        raise ModelError(f"{context} is missing required field {name!r}")
    return value


def _validate_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    value = _require_tuple(value, field_name)
    for index, item in enumerate(value):
        _require_string(item, f"{field_name}[{index}]")
    return value


def _validate_nonnegative_int_tuple(value: Any, field_name: str) -> tuple[int, ...]:
    value = _require_tuple(value, field_name)
    for index, item in enumerate(value):
        _require_int(item, f"{field_name}[{index}]", nonnegative=True)
    return value


def _validate_json_mapping(value: Any, field_name: str) -> dict[str, Any]:
    value = _require_mapping(value, field_name)
    return {key: _validate_json_value(item, f"{field_name}.{key}") for key, item in value.items()}


def _validate_json_value(value: Any, field_name: str = "value") -> Any:
    """Validate values that can be represented by the canonical JSON codec."""

    if value is None or type(value) in (bool, str, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ModelError(f"{field_name} contains a non-finite float")
        return value
    if type(value) is list:
        return [_validate_json_value(item, f"{field_name}[{index}]") for index, item in enumerate(value)]
    if type(value) is tuple:
        return tuple(_validate_json_value(item, f"{field_name}[{index}]") for index, item in enumerate(value))
    if type(value) is frozenset:
        return frozenset(_validate_json_value(item, f"{field_name} item") for item in value)
    if isinstance(value, Mapping):
        return _validate_json_mapping(value, field_name)
    raise TypeError(f"{field_name} contains unsupported non-JSON value {type(value).__name__}")


class _SerializableModel:
    """Small common API; concrete classes retain their documented dataclass shape."""

    def to_dict(self) -> dict[str, Any]:
        return _model_to_dict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]):
        return _model_from_dict(data, cls)


@dataclass
class RowMeta(_SerializableModel):
    row_id: str
    case_name: str
    external_test_case_id: str | None
    origin: str
    status: str
    generation_created: int
    generation_modified: int
    function_key: str
    # Structured obligations were introduced after the original eight-field row
    # identity record.  Keep a default so old positional constructors and v2
    # payloads remain readable during migration.
    obligation_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_string(self.row_id, "row_id", nonempty=True)
        _require_string(self.case_name, "case_name", nonempty=True)
        _require_optional_string(self.external_test_case_id, "external_test_case_id")
        _require_enum(self.origin, "origin", _ROW_ORIGINS)
        _require_enum(self.status, "status", _ROW_STATUSES)
        _require_int(self.generation_created, "generation_created", nonnegative=True)
        _require_int(self.generation_modified, "generation_modified", nonnegative=True)
        if self.generation_modified < self.generation_created:
            raise ModelError("generation_modified must not precede generation_created")
        _require_string(self.function_key, "function_key", nonempty=True)
        values = _validate_string_tuple(self.obligation_keys, "obligation_keys")
        # Obligation identity is a set at the row boundary.  Canonicalize direct
        # construction as well as deserialization so equivalent rows compare and
        # serialize identically even when a generator supplied duplicates/order
        # churn.
        self.obligation_keys = tuple(sorted(set(values)))


@dataclass(frozen=True)
class CellValue(_SerializableModel):
    kind: str
    raw_text: str | None
    semantic_value: object | None

    def __post_init__(self) -> None:
        _require_enum(self.kind, "kind", frozenset({"value", "explicit_empty", "missing"}))
        _require_optional_string(self.raw_text, "raw_text")


@dataclass(frozen=True)
class ExcelColumnKey(_SerializableModel):
    canonical_name: str
    channel: str
    element_path: tuple[int, ...]
    occurrence: int

    def __post_init__(self) -> None:
        _require_string(self.canonical_name, "canonical_name", nonempty=True)
        _require_enum(self.channel, "channel", _CHANNELS)
        _validate_nonnegative_int_tuple(self.element_path, "element_path")
        _require_int(self.occurrence, "occurrence", nonnegative=True)


@dataclass(frozen=True)
class LogicalArgumentKey(_SerializableModel):
    canonical_xml_name: str
    occurrence: int

    def __post_init__(self) -> None:
        _require_string(self.canonical_xml_name, "canonical_xml_name", nonempty=True)
        _require_int(self.occurrence, "occurrence", nonnegative=True)


@dataclass
class ArgumentState(_SerializableModel):
    key: LogicalArgumentKey
    name: str
    direction: str
    fmt: str | None
    xml_len: str | None
    pointer: bool | None
    testmethod: str | None
    dimensions: tuple[int, ...]
    range_encoding: str
    output_encoding: str
    source: str
    metadata_status: str
    original_order: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, LogicalArgumentKey):
            raise _type_error("key", "a LogicalArgumentKey", self.key)
        _require_string(self.name, "name", nonempty=True)
        _require_enum(self.direction, "direction", _DIRECTIONS)
        _require_optional_string(self.fmt, "fmt")
        _require_optional_string(self.xml_len, "xml_len")
        _require_bool_or_none(self.pointer, "pointer")
        _require_optional_string(self.testmethod, "testmethod")
        _validate_nonnegative_int_tuple(self.dimensions, "dimensions")
        _require_enum(self.range_encoding, "range_encoding", _RANGE_ENCODINGS)
        _require_enum(self.output_encoding, "output_encoding", _OUTPUT_ENCODINGS)
        _require_enum(self.source, "source", frozenset({"generated", "manual"}))
        _require_enum(self.metadata_status, "metadata_status", _METADATA_STATUSES)
        _require_int(self.original_order, "original_order", nonnegative=True)


@dataclass(frozen=True)
class StubParameter(_SerializableModel):
    type_name: str
    name: str

    def __post_init__(self) -> None:
        _require_string(self.type_name, "type_name", nonempty=True)
        _require_string(self.name, "name", nonempty=True)


@dataclass
class StubState(_SerializableModel):
    stub_key: str
    function_name: str
    return_type: str | None
    return_storage_name: str | None
    parameters: tuple[StubParameter, ...]
    body_raw: str
    owners: frozenset[str]
    origin: str
    status: str
    metadata_status: str

    def __post_init__(self) -> None:
        _require_string(self.stub_key, "stub_key", nonempty=True)
        _require_string(self.function_name, "function_name", nonempty=True)
        _require_optional_string(self.return_type, "return_type")
        _require_optional_string(self.return_storage_name, "return_storage_name")
        parameters = _require_tuple(self.parameters, "parameters")
        if not all(isinstance(parameter, StubParameter) for parameter in parameters):
            raise ModelError("parameters must contain only StubParameter records")
        _require_string(self.body_raw, "body_raw")
        owners = _require_frozenset(self.owners, "owners")
        if not all(type(owner) is str and owner for owner in owners):
            raise ModelError("owners must contain non-empty strings")
        _require_enum(self.origin, "origin", _ROW_ORIGINS)
        _require_enum(self.status, "status", _STUB_STATUSES)
        _require_enum(self.metadata_status, "metadata_status", _METADATA_STATUSES)


@dataclass
class MergeConflict(_SerializableModel):
    code: str
    severity: str
    entity_type: str
    function_key: str
    row_id: str | None
    case_name: str | None
    logical_argument_key: str | None
    stub_key: str | None
    base: object
    user: object
    new: object
    message: str
    allowed_resolutions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_string(self.code, "code", nonempty=True)
        _require_enum(self.severity, "severity", _SEVERITIES)
        _require_string(self.entity_type, "entity_type", nonempty=True)
        _require_string(self.function_key, "function_key", nonempty=True)
        _require_optional_string(self.row_id, "row_id")
        _require_optional_string(self.case_name, "case_name")
        _require_optional_string(self.logical_argument_key, "logical_argument_key")
        _require_optional_string(self.stub_key, "stub_key")
        _require_string(self.message, "message")
        _validate_string_tuple(self.allowed_resolutions, "allowed_resolutions")


@dataclass(frozen=True)
class DisplayVersion(_SerializableModel):
    """User-visible per-Test-Object version and accepted timestamp.

    The version is kept as integer components until an XML/manifest boundary
    formats it as ``major.minor``.  ``updated_at`` is normalized so every UTC
    spelling has the single canonical ``Z`` representation.
    """

    major: int
    minor: int
    updated_at: str

    def __post_init__(self) -> None:
        _require_int(self.major, "major", nonnegative=True)
        _require_int(self.minor, "minor", nonnegative=True)
        object.__setattr__(self, "updated_at", _canonicalize_timestamp(self.updated_at))


@dataclass(frozen=True)
class AnalysisRef(_SerializableModel):
    """Immutable reference metadata for one stored analysis DB snapshot."""

    analysis_id: str
    db_schema_version: int
    analysis_content_sha256: str
    db_file_sha256: str
    source_sha256: str
    analysis_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.analysis_id, "analysis_id", nonempty=True)
        if _ANALYSIS_ID.fullmatch(self.analysis_id) is None:
            raise ModelError("analysis_id must match analysis_v1_ followed by 64 lowercase hexadecimal digits")
        _require_int(self.db_schema_version, "db_schema_version", nonnegative=True)
        if self.db_schema_version != 1:
            raise ModelError("only db_schema_version 1 is supported")
        for name, value in (
            ("analysis_content_sha256", self.analysis_content_sha256),
            ("db_file_sha256", self.db_file_sha256),
            ("source_sha256", self.source_sha256),
        ):
            _require_string(value, name, nonempty=True)
            if _SHA256.fullmatch(value) is None:
                raise ModelError(f"{name} must be 64 lowercase hexadecimal digits")
        if self.analysis_id != f"analysis_v1_{self.analysis_content_sha256}":
            raise ModelError("analysis_id must be derived from analysis_content_sha256")
        _require_optional_string(self.analysis_fingerprint, "analysis_fingerprint")


def _require_persistent_id(value: Any, field_name: str, pattern: re.Pattern[str]) -> str:
    value = _require_string(value, field_name, nonempty=True)
    if pattern.fullmatch(value) is None:
        raise ModelError(f"{field_name} has an invalid persistent UUID ID")
    return value


def _require_optional_fingerprint(value: Any, field_name: str) -> str | None:
    value = _require_optional_string(value, field_name)
    if value is not None and not value:
        raise ModelError(f"{field_name} must not be empty when present")
    return value


def _validate_portable_component(value: Any, field_name: str) -> str:
    """Reject path components unsafe on either POSIX or Windows."""

    value = _require_string(value, field_name, nonempty=True)
    if value in {".", ".."} or value[-1] in {" ", "."}:
        raise ModelError(f"{field_name} is not a portable path component")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ModelError(f"{field_name} contains a control character")
    if any(character in '<>:"|?*\\/' for character in value):
        raise ModelError(f"{field_name} contains a path-reserved character")
    stem = value.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_COMPONENTS or _WINDOWS_RESERVED_NUMBER.fullmatch(stem):
        raise ModelError(f"{field_name} uses a Windows reserved name")
    return value


def _validate_portable_relative_path(value: Any, field_name: str) -> str:
    value = _require_string(value, field_name, nonempty=True)
    if value.startswith("/") or ":" in value or "\\" in value:
        raise ModelError(f"{field_name} must be a portable relative path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ModelError(f"{field_name} contains an unsafe path component")
    for index, part in enumerate(parts):
        _validate_portable_component(part, f"{field_name}[{index}]")
    return value


@dataclass(frozen=True)
class Tombstone(_SerializableModel):
    """A compact record of an explicitly deleted state entity.

    PR05 persists the fixed three-field shape only.  Merge policy decides when
    a tombstone is created; the repository merely validates and stores it.
    """

    entity_type: str
    entity_key: str
    generation_deleted: int

    def __post_init__(self) -> None:
        _require_string(self.entity_type, "entity_type", nonempty=True)
        _require_string(self.entity_key, "entity_key", nonempty=True)
        _require_int(self.generation_deleted, "generation_deleted", nonnegative=True)


@dataclass(frozen=True)
class BaseSnapshot(_SerializableModel):
    """Pure generator snapshot used as the merge ancestor."""

    snapshot_id: str
    generation: int
    analysis_id: str
    function_fingerprint: str | None
    dependency_fingerprint: str | None
    rows: tuple[TestRow, ...]
    origin: str = "pure_new"

    def __post_init__(self) -> None:
        _require_persistent_id(self.snapshot_id, "snapshot_id", _SNAPSHOT_ID)
        _require_int(self.generation, "generation", nonnegative=True)
        if _ANALYSIS_ID.fullmatch(self.analysis_id) is None:
            raise ModelError("analysis_id must match analysis_v1_ followed by 64 lowercase hexadecimal digits")
        _require_optional_fingerprint(self.function_fingerprint, "function_fingerprint")
        _require_optional_fingerprint(self.dependency_fingerprint, "dependency_fingerprint")
        rows = _require_tuple(self.rows, "rows")
        if not all(isinstance(row, TestRow) for row in rows):
            raise ModelError("rows must contain only TestRow records")
        if self.origin != "pure_new":
            raise ModelError("BaseSnapshot.origin is fixed to 'pure_new'")


@dataclass(frozen=True)
class WorkingSnapshot(_SerializableModel):
    """User-facing snapshot; it may carry tombstones but not a BASE origin."""

    snapshot_id: str
    generation: int
    analysis_id: str
    row_order: tuple[str, ...]
    rows: tuple[TestRow, ...]
    arguments: tuple[ArgumentState, ...]
    tombstones: tuple[Tombstone, ...] = ()

    def __post_init__(self) -> None:
        _require_persistent_id(self.snapshot_id, "snapshot_id", _SNAPSHOT_ID)
        _require_int(self.generation, "generation", nonnegative=True)
        if _ANALYSIS_ID.fullmatch(self.analysis_id) is None:
            raise ModelError("analysis_id must match analysis_v1_ followed by 64 lowercase hexadecimal digits")
        row_order = _validate_string_tuple(self.row_order, "row_order")
        rows = _require_tuple(self.rows, "rows")
        if not all(isinstance(row, TestRow) for row in rows):
            raise ModelError("rows must contain only TestRow records")
        arguments = _require_tuple(self.arguments, "arguments")
        if not all(isinstance(argument, ArgumentState) for argument in arguments):
            raise ModelError("arguments must contain only ArgumentState records")
        tombstones = _require_tuple(self.tombstones, "tombstones")
        if not all(isinstance(tombstone, Tombstone) for tombstone in tombstones):
            raise ModelError("tombstones must contain only Tombstone records")
        row_ids = {row.meta.row_id for row in rows if row.meta is not None}
        if len(row_ids) != len([row for row in rows if row.meta is not None]):
            raise ModelError("WorkingSnapshot rows contain duplicate row IDs")
        if len(set(row_order)) != len(row_order):
            raise ModelError("WorkingSnapshot.row_order contains duplicate row IDs")


@dataclass(frozen=True)
class FunctionIterationState(_SerializableModel):
    """Strict persisted state for one Test-Object/function."""

    function_key: str
    source_file: str
    function: str
    next_case_number: int
    accepted_analysis_id: str
    display_version: DisplayVersion
    base: BaseSnapshot
    working: WorkingSnapshot
    schema_version: int = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _require_string(self.function_key, "function_key", nonempty=True)
        _require_string(self.source_file, "source_file", nonempty=True)
        _require_string(self.function, "function", nonempty=True)
        _require_int(self.next_case_number, "next_case_number", nonnegative=True)
        if self.next_case_number < 1:
            raise ModelError("next_case_number must be positive")
        if _ANALYSIS_ID.fullmatch(self.accepted_analysis_id) is None:
            raise ModelError(
                "accepted_analysis_id must match analysis_v1_ followed by 64 lowercase hexadecimal digits"
            )
        if not isinstance(self.display_version, DisplayVersion):
            raise _type_error("display_version", "a DisplayVersion", self.display_version)
        if not isinstance(self.base, BaseSnapshot):
            raise _type_error("base", "a BaseSnapshot", self.base)
        if not isinstance(self.working, WorkingSnapshot):
            raise _type_error("working", "a WorkingSnapshot", self.working)
        if self.base.analysis_id != self.accepted_analysis_id:
            raise ModelError("base.analysis_id must equal accepted_analysis_id")
        if self.working.analysis_id != self.accepted_analysis_id:
            raise ModelError("working.analysis_id must equal accepted_analysis_id")


def _validate_display_version_map(value: Any, field_name: str) -> Mapping[str, DisplayVersion]:
    value = _require_mapping(value, field_name)
    result: dict[str, DisplayVersion] = {}
    for key, item in value.items():
        _require_string(key, f"{field_name} key", nonempty=True)
        if not isinstance(item, DisplayVersion):
            raise _type_error(f"{field_name}.{key}", "a DisplayVersion", item)
        result[key] = item
    return MappingProxyType(result)


@dataclass(frozen=True)
class ModuleManifest(_SerializableModel):
    """One module entry in the project-wide root manifest."""

    module_key: str
    source_file: str
    accepted_generation: int
    base_snapshot_id: str
    working_snapshot_id: str
    generator_version: str
    config_fingerprint: str | None
    tessy_dialect: str
    analysis: AnalysisRef
    function_versions: Mapping[str, DisplayVersion]
    revision: int
    revision_id: str

    def __post_init__(self) -> None:
        _validate_portable_component(self.module_key, "module_key")
        _require_string(self.source_file, "source_file", nonempty=True)
        _require_int(self.accepted_generation, "accepted_generation", nonnegative=True)
        _require_persistent_id(self.base_snapshot_id, "base_snapshot_id", _SNAPSHOT_ID)
        _require_persistent_id(self.working_snapshot_id, "working_snapshot_id", _SNAPSHOT_ID)
        _require_string(self.generator_version, "generator_version", nonempty=True)
        _require_optional_fingerprint(self.config_fingerprint, "config_fingerprint")
        _require_string(self.tessy_dialect, "tessy_dialect", nonempty=True)
        if not isinstance(self.analysis, AnalysisRef):
            raise _type_error("analysis", "an AnalysisRef", self.analysis)
        versions = _validate_display_version_map(self.function_versions, "function_versions")
        object.__setattr__(self, "function_versions", versions)
        _require_int(self.revision, "revision", nonnegative=True)
        if self.revision < 1:
            raise ModelError("accepted module revision must be positive")
        _require_persistent_id(self.revision_id, "revision_id", _REVISION_ID)


@dataclass(frozen=True)
class RootManifest(_SerializableModel):
    """Project-wide current pointer, with one strict module map."""

    schema_version: int
    modules: Mapping[str, ModuleManifest]

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        modules = _require_mapping(self.modules, "modules")
        result: dict[str, ModuleManifest] = {}
        for key, value in modules.items():
            _validate_portable_component(key, "modules key")
            if not isinstance(value, ModuleManifest):
                raise _type_error(f"modules.{key}", "a ModuleManifest", value)
            if value.module_key != key:
                raise ModelError(f"modules.{key} does not match ModuleManifest.module_key")
            result[key] = value
        object.__setattr__(self, "modules", MappingProxyType(result))


@dataclass(frozen=True)
class BundleManifest(_SerializableModel):
    """Immutable manifest stored inside one finalized revision directory."""

    schema_version: int
    module_key: str
    revision_id: str
    revision: int
    parent_revision_id: str | None
    accepted_generation: int
    base_snapshot_id: str
    working_snapshot_id: str
    analysis: AnalysisRef
    artifact_hashes: Mapping[str, str]
    parent_revision: int | None = None
    artifact_kinds: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_portable_component(self.module_key, "module_key")
        _require_persistent_id(self.revision_id, "revision_id", _REVISION_ID)
        _require_int(self.revision, "revision", nonnegative=True)
        if self.revision < 1:
            raise ModelError("revision must be positive")
        if self.revision == 1:
            if self.parent_revision_id is not None or self.parent_revision is not None:
                raise ModelError("revision 1 must not have a parent revision")
        else:
            if self.parent_revision_id is None or self.parent_revision is None:
                raise ModelError("revisions after 1 require both parent revision fields")
        if self.parent_revision_id is not None:
            _require_persistent_id(self.parent_revision_id, "parent_revision_id", _REVISION_ID)
        if self.parent_revision is not None:
            _require_int(self.parent_revision, "parent_revision", nonnegative=True)
            if self.parent_revision != self.revision - 1:
                raise ModelError("parent_revision must equal revision - 1")
        _require_int(self.accepted_generation, "accepted_generation", nonnegative=True)
        _require_persistent_id(self.base_snapshot_id, "base_snapshot_id", _SNAPSHOT_ID)
        _require_persistent_id(self.working_snapshot_id, "working_snapshot_id", _SNAPSHOT_ID)
        if not isinstance(self.analysis, AnalysisRef):
            raise _type_error("analysis", "an AnalysisRef", self.analysis)
        hashes = _require_mapping(self.artifact_hashes, "artifact_hashes")
        normalized: dict[str, str] = {}
        for path, digest in hashes.items():
            _validate_portable_relative_path(path, "artifact_hashes path")
            if _SHA256.fullmatch(digest) is None:
                raise ModelError(f"artifact_hashes.{path} must be 64 lowercase hexadecimal digits")
            normalized[path] = digest
        object.__setattr__(self, "artifact_hashes", MappingProxyType(normalized))
        kinds = _require_mapping(self.artifact_kinds, "artifact_kinds")
        normalized_kinds: dict[str, str] = {}
        for path, kind in kinds.items():
            _validate_portable_relative_path(path, "artifact_kinds path")
            _require_string(kind, f"artifact_kinds.{path}", nonempty=True)
            if path not in normalized:
                raise ModelError(f"artifact_kinds contains an unknown artifact path: {path}")
            normalized_kinds[path] = kind
        if set(normalized_kinds) != set(normalized):
            raise ModelError("artifact_kinds must describe every artifact path")
        object.__setattr__(self, "artifact_kinds", MappingProxyType(normalized_kinds))


@dataclass(frozen=True)
class StateEnvelope(_SerializableModel):
    """A strict v2 schema envelope for one supported domain record.

    The envelope carries no repository/business fields.  It lets PR02 prove
    versioned model round-trips while BASE/WORKING/tombstone persistence stays
    with the later repository work.
    """

    schema_version: int
    model_type: str
    payload: object

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _require_string(self.model_type, "model_type", nonempty=True)
        payload_type = _MODEL_TYPES.get(self.model_type)
        if payload_type is None:
            raise ModelError(f"model_type {self.model_type!r} is not supported")
        if not isinstance(self.payload, payload_type):
            raise _type_error("payload", payload_type.__name__, self.payload)

    @classmethod
    def for_model(cls, payload: object) -> "StateEnvelope":
        for name, model_type in _MODEL_TYPES.items():
            if isinstance(payload, model_type):
                return cls(CURRENT_SCHEMA_VERSION, name, payload)
        raise TypeError(f"payload type {type(payload).__name__} is not a supported domain model")

    def to_dict(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "schema_version": self.schema_version,
            "model_type": self.model_type,
            "payload": _to_json_value(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StateEnvelope":
        data = _require_mapping(data, "StateEnvelope")
        _reject_unknown(data, frozenset({"schema_version", "model_type", "payload"}), "StateEnvelope")
        if "schema_version" not in data:
            raise SchemaVersionError("StateEnvelope is missing required field 'schema_version'")
        schema_version = _validate_schema_version(_required(data, "schema_version", "StateEnvelope"))
        model_name = _require_string(_required(data, "model_type", "StateEnvelope"), "model_type", nonempty=True)
        payload_type = _MODEL_TYPES.get(model_name)
        if payload_type is None:
            raise ModelError(f"model_type {model_name!r} is not supported")
        payload = _model_from_dict(_required(data, "payload", "StateEnvelope"), payload_type)
        return cls(schema_version, model_name, payload)


# This alias is a naming convenience only; it does not introduce another state
# model or any unimplemented Tombstone/Orphan/PointerState fields.
VersionedState = StateEnvelope


_MODEL_TYPES: dict[str, type] = {
    "RowMeta": RowMeta,
    "TestRow": TestRow,
    "CellValue": CellValue,
    "ExcelColumnKey": ExcelColumnKey,
    "LogicalArgumentKey": LogicalArgumentKey,
    "ArgumentState": ArgumentState,
    "StubParameter": StubParameter,
    "StubState": StubState,
    "MergeConflict": MergeConflict,
    "DisplayVersion": DisplayVersion,
    "AnalysisRef": AnalysisRef,
    "Tombstone": Tombstone,
    "BaseSnapshot": BaseSnapshot,
    "WorkingSnapshot": WorkingSnapshot,
    "FunctionIterationState": FunctionIterationState,
    "ModuleManifest": ModuleManifest,
    "RootManifest": RootManifest,
    "BundleManifest": BundleManifest,
}


def _validate_schema_version(value: Any) -> int:
    if type(value) is not int:
        raise SchemaVersionError("schema_version must be an integer")
    if value != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"schema_version {value} is unsupported; only {CURRENT_SCHEMA_VERSION} is accepted"
        )
    return value


def _to_intent_dict(intent: Any) -> dict[str, Any]:
    if type(intent) is not dict:
        raise _type_error("intent", "a dict", intent)
    output: dict[str, Any] = {}
    for key, value in intent.items():
        if type(key) is int:
            encoded_key = str(key)
        else:
            raise TypeError("intent keys must be integers")
        if encoded_key in output:
            raise ModelError(f"intent keys collide after JSON encoding: {encoded_key!r}")
        output[encoded_key] = _to_json_value(value)
    return output


def _test_row_to_dict(row: TestRow) -> dict[str, Any]:
    if type(row.values) is not dict:
        raise _type_error("values", "a dict", row.values)
    if type(row.kind) is not str:
        raise _type_error("kind", "a string", row.kind)
    if type(row.description) is not str:
        raise _type_error("description", "a string", row.description)
    if type(row.expected) is not dict:
        raise _type_error("expected", "a dict", row.expected)
    if row.meta is not None and not isinstance(row.meta, RowMeta):
        raise _type_error("meta", "a RowMeta or null", row.meta)
    obligation_keys = getattr(row, "obligation_keys", ())
    if isinstance(obligation_keys, str) or not isinstance(obligation_keys, (tuple, list, set, frozenset)):
        raise _type_error("obligation_keys", "a collection of strings", obligation_keys)
    if any(type(value) is not str or not value for value in obligation_keys):
        raise ModelError("TestRow.obligation_keys must contain non-empty strings")
    runtime_obligations = tuple(sorted(set(obligation_keys)))
    if row.meta is not None:
        # RowMeta is the persistent authority.  TestRow.obligation_keys is only
        # a generator-time staging field; when a row already has identity metadata
        # a non-empty staging set must agree with the metadata rather than creating
        # a second, conflicting persisted source.  An empty staging set is treated
        # as "not supplied" for backwards-compatible callers that construct a
        # persisted row positionally.
        metadata_obligations = tuple(row.meta.obligation_keys)
        if runtime_obligations and frozenset(runtime_obligations) != frozenset(metadata_obligations):
            raise ModelError(
                "TestRow.obligation_keys conflicts with authoritative RowMeta.obligation_keys"
            )
    result = {
        "values": _to_json_value(row.values),
        "kind": row.kind,
        "description": row.description,
        "intent": _to_intent_dict(row.intent),
        "expected": _to_json_value(row.expected),
        "meta": None if row.meta is None else row.meta.to_dict(),
    }
    # Empty provenance is omitted to keep the PR01-10 JSON wire shape byte-for-byte
    # compatible.  Once a generator objective exists without persistent identity it
    # is serialized deterministically and can be copied into RowMeta when identity is
    # allocated.  Rows with metadata persist only the nested RowMeta field.
    if row.meta is None and runtime_obligations:
        result["obligation_keys"] = list(runtime_obligations)
    return result


def _model_to_dict(value: _SerializableModel) -> dict[str, Any]:
    value.__post_init__()
    result: dict[str, Any] = {}
    for field in fields(value):
        result[field.name] = _to_json_value(getattr(value, field.name))
    return result


def _frozenset_sort_key(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _to_json_value(value: Any) -> Any:
    if isinstance(value, TestRow):
        return _test_row_to_dict(value)
    if isinstance(value, _SerializableModel):
        return value.to_dict()
    if value is None or type(value) in (bool, str, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not valid canonical JSON")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be strings")
        result: dict[str, Any] = {}
        for key in sorted(value):
            result[key] = _to_json_value(value[key])
        return result
    if type(value) is list or type(value) is tuple:
        return [_to_json_value(item) for item in value]
    if type(value) is frozenset:
        values = [_to_json_value(item) for item in value]
        return sorted(values, key=_frozenset_sort_key)
    if is_dataclass(value):
        raise TypeError(f"unsupported dataclass {type(value).__name__}; use a public iteration model")
    raise TypeError(f"unsupported non-JSON value {type(value).__name__}")


def _model_from_dict(data: Mapping[str, Any], model_type: type):
    if model_type is StateEnvelope:
        return StateEnvelope.from_dict(data)
    data = _require_mapping(data, model_type.__name__)
    if model_type is RowMeta:
        allowed = frozenset({
            "row_id", "case_name", "external_test_case_id", "origin", "status",
            "generation_created", "generation_modified", "function_key", "obligation_keys",
        })
        _reject_unknown(data, allowed, "RowMeta")
        return RowMeta(
            _required(data, "row_id", "RowMeta"),
            _required(data, "case_name", "RowMeta"),
            _required(data, "external_test_case_id", "RowMeta"),
            _required(data, "origin", "RowMeta"),
            _required(data, "status", "RowMeta"),
            _required(data, "generation_created", "RowMeta"),
            _required(data, "generation_modified", "RowMeta"),
            _required(data, "function_key", "RowMeta"),
            _tuple_from_json(data.get("obligation_keys", []), "RowMeta.obligation_keys"),
        )
    if model_type is CellValue:
        allowed = frozenset({"kind", "raw_text", "semantic_value"})
        _reject_unknown(data, allowed, "CellValue")
        return CellValue(
            _required(data, "kind", "CellValue"),
            _required(data, "raw_text", "CellValue"),
            _validate_json_value(
                _required(data, "semantic_value", "CellValue"),
                "CellValue.semantic_value",
            ),
        )
    if model_type is ExcelColumnKey:
        allowed = frozenset({"canonical_name", "channel", "element_path", "occurrence"})
        _reject_unknown(data, allowed, "ExcelColumnKey")
        return ExcelColumnKey(
            _required(data, "canonical_name", "ExcelColumnKey"),
            _required(data, "channel", "ExcelColumnKey"),
            _tuple_from_json(_required(data, "element_path", "ExcelColumnKey"), "ExcelColumnKey.element_path"),
            _required(data, "occurrence", "ExcelColumnKey"),
        )
    if model_type is LogicalArgumentKey:
        allowed = frozenset({"canonical_xml_name", "occurrence"})
        _reject_unknown(data, allowed, "LogicalArgumentKey")
        return LogicalArgumentKey(
            _required(data, "canonical_xml_name", "LogicalArgumentKey"),
            _required(data, "occurrence", "LogicalArgumentKey"),
        )
    if model_type is ArgumentState:
        allowed = frozenset({
            "key", "name", "direction", "fmt", "xml_len", "pointer", "testmethod",
            "dimensions", "range_encoding", "output_encoding", "source", "metadata_status",
            "original_order",
        })
        _reject_unknown(data, allowed, "ArgumentState")
        return ArgumentState(
            _model_from_dict(_required(data, "key", "ArgumentState"), LogicalArgumentKey),
            _required(data, "name", "ArgumentState"),
            _required(data, "direction", "ArgumentState"),
            _required(data, "fmt", "ArgumentState"),
            _required(data, "xml_len", "ArgumentState"),
            _required(data, "pointer", "ArgumentState"),
            _required(data, "testmethod", "ArgumentState"),
            _tuple_from_json(_required(data, "dimensions", "ArgumentState"), "ArgumentState.dimensions"),
            _required(data, "range_encoding", "ArgumentState"),
            _required(data, "output_encoding", "ArgumentState"),
            _required(data, "source", "ArgumentState"),
            _required(data, "metadata_status", "ArgumentState"),
            _required(data, "original_order", "ArgumentState"),
        )
    if model_type is StubParameter:
        allowed = frozenset({"type_name", "name"})
        _reject_unknown(data, allowed, "StubParameter")
        return StubParameter(
            _required(data, "type_name", "StubParameter"),
            _required(data, "name", "StubParameter"),
        )
    if model_type is StubState:
        allowed = frozenset({
            "stub_key", "function_name", "return_type", "return_storage_name", "parameters",
            "body_raw", "owners", "origin", "status", "metadata_status",
        })
        _reject_unknown(data, allowed, "StubState")
        return StubState(
            _required(data, "stub_key", "StubState"),
            _required(data, "function_name", "StubState"),
            _required(data, "return_type", "StubState"),
            _required(data, "return_storage_name", "StubState"),
            tuple(
                _model_from_dict(item, StubParameter)
                for item in _require_json_array(_required(data, "parameters", "StubState"), "StubState.parameters")
            ),
            _required(data, "body_raw", "StubState"),
            frozenset(_require_json_array(_required(data, "owners", "StubState"), "StubState.owners")),
            _required(data, "origin", "StubState"),
            _required(data, "status", "StubState"),
            _required(data, "metadata_status", "StubState"),
        )
    if model_type is MergeConflict:
        allowed = frozenset({
            "code", "severity", "entity_type", "function_key", "row_id", "case_name",
            "logical_argument_key", "stub_key", "base", "user", "new", "message",
            "allowed_resolutions",
        })
        _reject_unknown(data, allowed, "MergeConflict")
        return MergeConflict(
            _required(data, "code", "MergeConflict"),
            _required(data, "severity", "MergeConflict"),
            _required(data, "entity_type", "MergeConflict"),
            _required(data, "function_key", "MergeConflict"),
            _required(data, "row_id", "MergeConflict"),
            _required(data, "case_name", "MergeConflict"),
            _required(data, "logical_argument_key", "MergeConflict"),
            _required(data, "stub_key", "MergeConflict"),
            _validate_json_value(_required(data, "base", "MergeConflict"), "MergeConflict.base"),
            _validate_json_value(_required(data, "user", "MergeConflict"), "MergeConflict.user"),
            _validate_json_value(_required(data, "new", "MergeConflict"), "MergeConflict.new"),
            _required(data, "message", "MergeConflict"),
            _tuple_from_json(
                _required(data, "allowed_resolutions", "MergeConflict"),
                "MergeConflict.allowed_resolutions",
            ),
        )
    if model_type is DisplayVersion:
        allowed = frozenset({"major", "minor", "updated_at"})
        _reject_unknown(data, allowed, "DisplayVersion")
        return DisplayVersion(
            _required(data, "major", "DisplayVersion"),
            _required(data, "minor", "DisplayVersion"),
            _required(data, "updated_at", "DisplayVersion"),
        )
    if model_type is AnalysisRef:
        allowed = frozenset({
            "analysis_id", "db_schema_version", "analysis_content_sha256",
            "db_file_sha256", "source_sha256", "analysis_fingerprint",
        })
        _reject_unknown(data, allowed, "AnalysisRef")
        return AnalysisRef(
            _required(data, "analysis_id", "AnalysisRef"),
            _required(data, "db_schema_version", "AnalysisRef"),
            _required(data, "analysis_content_sha256", "AnalysisRef"),
            _required(data, "db_file_sha256", "AnalysisRef"),
            _required(data, "source_sha256", "AnalysisRef"),
            data.get("analysis_fingerprint"),
        )
    if model_type is Tombstone:
        allowed = frozenset({"entity_type", "entity_key", "generation_deleted"})
        _reject_unknown(data, allowed, "Tombstone")
        return Tombstone(
            _required(data, "entity_type", "Tombstone"),
            _required(data, "entity_key", "Tombstone"),
            _required(data, "generation_deleted", "Tombstone"),
        )
    if model_type is BaseSnapshot:
        allowed = frozenset({
            "snapshot_id", "generation", "analysis_id", "function_fingerprint",
            "dependency_fingerprint", "rows", "origin",
        })
        _reject_unknown(data, allowed, "BaseSnapshot")
        return BaseSnapshot(
            _required(data, "snapshot_id", "BaseSnapshot"),
            _required(data, "generation", "BaseSnapshot"),
            _required(data, "analysis_id", "BaseSnapshot"),
            _required(data, "function_fingerprint", "BaseSnapshot"),
            _required(data, "dependency_fingerprint", "BaseSnapshot"),
            tuple(
                _model_from_dict(item, TestRow)
                for item in _require_json_array(_required(data, "rows", "BaseSnapshot"), "BaseSnapshot.rows")
            ),
            _required(data, "origin", "BaseSnapshot"),
        )
    if model_type is WorkingSnapshot:
        allowed = frozenset({
            "snapshot_id", "generation", "analysis_id", "row_order", "rows",
            "arguments", "tombstones",
        })
        _reject_unknown(data, allowed, "WorkingSnapshot")
        return WorkingSnapshot(
            _required(data, "snapshot_id", "WorkingSnapshot"),
            _required(data, "generation", "WorkingSnapshot"),
            _required(data, "analysis_id", "WorkingSnapshot"),
            _tuple_from_json(_required(data, "row_order", "WorkingSnapshot"), "WorkingSnapshot.row_order"),
            tuple(
                _model_from_dict(item, TestRow)
                for item in _require_json_array(_required(data, "rows", "WorkingSnapshot"), "WorkingSnapshot.rows")
            ),
            tuple(
                _model_from_dict(item, ArgumentState)
                for item in _require_json_array(
                    _required(data, "arguments", "WorkingSnapshot"), "WorkingSnapshot.arguments"
                )
            ),
            tuple(
                _model_from_dict(item, Tombstone)
                for item in _require_json_array(
                    _required(data, "tombstones", "WorkingSnapshot"),
                    "WorkingSnapshot.tombstones",
                )
            ),
        )
    if model_type is FunctionIterationState:
        allowed = frozenset({
            "function_key", "source_file", "function", "next_case_number",
            "accepted_analysis_id", "display_version", "base", "working", "schema_version",
        })
        _reject_unknown(data, allowed, "FunctionIterationState")
        if "schema_version" not in data:
            raise SchemaVersionError("FunctionIterationState is missing required field 'schema_version'")
        return FunctionIterationState(
            _required(data, "function_key", "FunctionIterationState"),
            _required(data, "source_file", "FunctionIterationState"),
            _required(data, "function", "FunctionIterationState"),
            _required(data, "next_case_number", "FunctionIterationState"),
            _required(data, "accepted_analysis_id", "FunctionIterationState"),
            _model_from_dict(_required(data, "display_version", "FunctionIterationState"), DisplayVersion),
            _model_from_dict(_required(data, "base", "FunctionIterationState"), BaseSnapshot),
            _model_from_dict(_required(data, "working", "FunctionIterationState"), WorkingSnapshot),
            _required(data, "schema_version", "FunctionIterationState"),
        )
    if model_type is ModuleManifest:
        allowed = frozenset({
            "module_key", "source_file", "accepted_generation", "base_snapshot_id",
            "working_snapshot_id", "generator_version", "config_fingerprint", "tessy_dialect",
            "analysis", "function_versions", "revision", "revision_id",
        })
        _reject_unknown(data, allowed, "ModuleManifest")
        raw_versions = _require_mapping(
            _required(data, "function_versions", "ModuleManifest"),
            "ModuleManifest.function_versions",
        )
        versions = {
            key: _model_from_dict(item, DisplayVersion)
            for key, item in raw_versions.items()
        }
        return ModuleManifest(
            _required(data, "module_key", "ModuleManifest"),
            _required(data, "source_file", "ModuleManifest"),
            _required(data, "accepted_generation", "ModuleManifest"),
            _required(data, "base_snapshot_id", "ModuleManifest"),
            _required(data, "working_snapshot_id", "ModuleManifest"),
            _required(data, "generator_version", "ModuleManifest"),
            _required(data, "config_fingerprint", "ModuleManifest"),
            _required(data, "tessy_dialect", "ModuleManifest"),
            _model_from_dict(_required(data, "analysis", "ModuleManifest"), AnalysisRef),
            versions,
            _required(data, "revision", "ModuleManifest"),
            _required(data, "revision_id", "ModuleManifest"),
        )
    if model_type is RootManifest:
        allowed = frozenset({"schema_version", "modules"})
        _reject_unknown(data, allowed, "RootManifest")
        if "schema_version" not in data:
            raise SchemaVersionError("RootManifest is missing required field 'schema_version'")
        raw_modules = _require_mapping(_required(data, "modules", "RootManifest"), "RootManifest.modules")
        modules = {
            key: _model_from_dict(item, ModuleManifest)
            for key, item in raw_modules.items()
        }
        return RootManifest(
            _required(data, "schema_version", "RootManifest"),
            modules,
        )
    if model_type is BundleManifest:
        allowed = frozenset({
            "schema_version", "module_key", "revision_id", "revision", "parent_revision_id",
            "accepted_generation", "base_snapshot_id", "working_snapshot_id", "analysis",
            "artifact_hashes", "parent_revision", "artifact_kinds",
        })
        _reject_unknown(data, allowed, "BundleManifest")
        return BundleManifest(
            _required(data, "schema_version", "BundleManifest"),
            _required(data, "module_key", "BundleManifest"),
            _required(data, "revision_id", "BundleManifest"),
            _required(data, "revision", "BundleManifest"),
            _required(data, "parent_revision_id", "BundleManifest"),
            _required(data, "accepted_generation", "BundleManifest"),
            _required(data, "base_snapshot_id", "BundleManifest"),
            _required(data, "working_snapshot_id", "BundleManifest"),
            _model_from_dict(_required(data, "analysis", "BundleManifest"), AnalysisRef),
            _require_mapping(
                _required(data, "artifact_hashes", "BundleManifest"),
                "BundleManifest.artifact_hashes",
            ),
            data.get("parent_revision"),
            _require_mapping(data.get("artifact_kinds", {}), "BundleManifest.artifact_kinds"),
        )
    if model_type is TestRow:
        allowed = frozenset({"values", "kind", "description", "intent", "expected", "meta", "obligation_keys"})
        _reject_unknown(data, allowed, "TestRow")
        values = _validate_json_mapping(_required(data, "values", "TestRow"), "TestRow.values")
        kind = _require_string(_required(data, "kind", "TestRow"), "kind")
        description = _require_string(_required(data, "description", "TestRow"), "description")
        raw_intent = _require_mapping(_required(data, "intent", "TestRow"), "TestRow.intent")
        intent: dict[int, Any] = {}
        for key, value in raw_intent.items():
            if _INTENT_INTEGER_KEY.fullmatch(key) is None:
                raise ModelError(
                    f"TestRow.intent key {key!r} is not a canonical integer Decision ID"
                )
            converted = int(key)
            if converted in intent:
                raise ModelError(f"TestRow.intent key collision after integer restoration: {key!r}")
            intent[converted] = _validate_json_value(value, f"TestRow.intent.{key}")
        expected = _validate_json_mapping(_required(data, "expected", "TestRow"), "TestRow.expected")
        raw_meta = _required(data, "meta", "TestRow")
        meta = None if raw_meta is None else _model_from_dict(raw_meta, RowMeta)
        raw_obligations = data.get("obligation_keys", ())
        if isinstance(raw_obligations, (str, bytes, bytearray)) or not isinstance(raw_obligations, (tuple, list, set, frozenset)):
            raise _type_error("obligation_keys", "a collection of strings", raw_obligations)
        obligations = tuple(sorted(set(_require_string(value, "TestRow.obligation_keys item", nonempty=True) for value in raw_obligations)))
        if meta is not None:
            # A legacy/transitional payload may contain both fields.  Accept it
            # only when both sources describe the same set.  Do not hydrate the
            # runtime staging field from RowMeta: the field is intentionally an
            # ephemeral generator-side value, and leaving it empty preserves
            # equality for old positional TestRow(meta=...) records after a
            # canonical persistence roundtrip.
            if obligations and frozenset(obligations) != frozenset(meta.obligation_keys):
                raise ModelError(
                    "TestRow.obligation_keys conflicts with authoritative RowMeta.obligation_keys"
                )
        return TestRow(values, kind, description, intent, expected, meta, obligations)
    raise TypeError(f"unsupported model type {model_type!r}")


def to_dict(value: Any) -> dict[str, Any]:
    """Convert one supported domain model to a strict JSON-compatible dict."""

    if isinstance(value, TestRow):
        return _test_row_to_dict(value)
    if isinstance(value, _SerializableModel):
        return value.to_dict()
    raise TypeError(f"to_dict expects a supported domain model, got {type(value).__name__}")


T = TypeVar("T")


def from_dict(data: Mapping[str, Any] | type[T], model_type: type[T] | Mapping[str, Any] | None = None) -> T:
    """Strictly parse a dict into a supported model.

    Both ``from_dict(data, Model)`` and ``from_dict(Model, data)`` are accepted
    to keep call sites explicit without introducing a second serializer API.
    """

    if isinstance(data, type):
        data, model_type = model_type, data
    if not isinstance(model_type, type):
        raise TypeError("from_dict requires a model type")
    return _model_from_dict(data, model_type)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r} is not accepted")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a model/native value with the exact PR02 canonical JSON rules."""

    native = _to_json_value(value)
    encoded = json.dumps(
        native,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if encoded.startswith(b"\xef\xbb\xbf") or encoded.endswith(b"\n"):
        raise AssertionError("canonical JSON unexpectedly contains BOM or trailing newline")
    return encoded


def pretty_json_bytes(value: Any) -> bytes:
    """Encode a model/native value as deterministic, human-readable JSON."""

    native = _to_json_value(value)
    encoded = json.dumps(
        native,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
        allow_nan=False,
    ).encode("utf-8")
    if encoded.startswith(b"\xef\xbb\xbf") or encoded.endswith(b"\n"):
        raise AssertionError("pretty JSON unexpectedly contains BOM or trailing newline")
    return encoded


def is_canonical_json_bytes(value: Any, data: bytes) -> bool:
    """Accept valid JSON formatting when the canonical content is unchanged."""

    try:
        return canonical_json_bytes(loads(data)) == canonical_json_bytes(value)
    except (TypeError, UnicodeError, ValueError):
        return False


def loads(
    data: str | bytes | bytearray,
    model_type: type[T] | None = None,
    *,
    cls: type[T] | None = None,
) -> Any:
    """Parse JSON and optionally strictly restore a selected domain model."""

    if model_type is not None and cls is not None:
        raise TypeError("provide either model_type or cls, not both")
    selected_type = model_type or cls
    if isinstance(data, bytearray):
        data = bytes(data)
    if isinstance(data, bytes):
        if data.startswith(b"\xef\xbb\xbf"):
            raise ValueError("UTF-8 BOM is not accepted")
        text = data.decode("utf-8")
    elif type(data) is str:
        if data.startswith("\ufeff"):
            raise ValueError("UTF-8 BOM is not accepted")
        text = data
    else:
        raise _type_error("data", "UTF-8 JSON text or bytes", data)
    parsed = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_json_constant,
    )
    if selected_type is None:
        return parsed
    return from_dict(parsed, selected_type)


def migrate_state(
    data: Mapping[str, Any],
    from_version: int,
    to_version: int = CURRENT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Reserved pure migration hook; no legacy v1 format is accepted in PR02."""

    _require_mapping(data, "state")
    if type(from_version) is not int or type(to_version) is not int:
        raise SchemaVersionError("migration schema versions must be integers")
    if from_version != CURRENT_SCHEMA_VERSION or to_version != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"migration {from_version}->{to_version} is unavailable; only v2 is supported"
        )
    return copy.deepcopy(dict(data))


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "ArgumentState",
    "BaseSnapshot",
    "BundleManifest",
    "CellValue",
    "DisplayVersion",
    "AnalysisRef",
    "ExcelColumnKey",
    "FunctionIterationState",
    "LogicalArgumentKey",
    "MergeConflict",
    "ModuleManifest",
    "ModelError",
    "RowMeta",
    "RootManifest",
    "SchemaVersionError",
    "StateEnvelope",
    "StubParameter",
    "StubState",
    "Tombstone",
    "WorkingSnapshot",
    "UnknownFieldError",
    "UnsupportedSchemaError",
    "VersionedState",
    "canonical_json_bytes",
    "is_canonical_json_bytes",
    "pretty_json_bytes",
    "from_dict",
    "loads",
    "migrate_state",
    "to_dict",
]
