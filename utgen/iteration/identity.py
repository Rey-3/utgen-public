"""Pure row and TESSY case-name identity allocation.

This module deliberately owns only local identity mechanics.  It does not
persist counters, match bootstrap rows, or construct semantic source keys;
callers provide the accepted counter and existing identities explicitly.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import PurePath
import re
import uuid
from typing import Callable, Literal


ROW_ID_PREFIX = "r_"
_ROW_ID = re.compile(r"r_[0-9a-f]{32}\Z")
_CASE_NAME = re.compile(r"^(?P<source>[^\s]+)\.c(?P<number>0*[1-9][0-9]*)\Z")
CollisionPolicy = Literal["error", "skip"]
UUIDFactory = Callable[[], uuid.UUID]


class IdentityError(ValueError):
    """Base error for invalid or colliding local identities."""


class IdentityFormatError(IdentityError):
    """Raised when a row ID, case name, or allocator input is malformed."""


class IdentityCollisionError(IdentityError):
    """Raised when an identity would duplicate an existing identity."""


@dataclass(frozen=True)
class CaseNameAllocation:
    """Result of one pure case-name allocation.

    The caller owns persistence of ``next_case_number``.  Returning it beside
    the allocated name makes it explicit that deleted numbers are not reused
    when the caller carries the returned counter forward.
    """

    case_name: str
    next_case_number: int

    def __post_init__(self) -> None:
        source, number = _parse_case_name(self.case_name)
        del source
        if type(self.next_case_number) is not int or self.next_case_number != number + 1:
            raise IdentityFormatError(
                "next_case_number must be exactly one greater than the allocated case number"
            )


def _require_string(value: object, field_name: str, *, nonempty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if nonempty and not value:
        raise IdentityFormatError(f"{field_name} must not be empty")
    return value


def _require_collection(values: object, field_name: str) -> tuple[object, ...]:
    if isinstance(values, (str, bytes, bytearray)) or isinstance(values, Mapping):
        raise TypeError(f"{field_name} must be a collection of values, not a string or mapping")
    if not isinstance(values, Collection):
        raise TypeError(f"{field_name} must be a finite collection")
    return tuple(values)


def _source_basename(source_filename: object) -> str:
    source_filename = _require_string(source_filename, "source_filename", nonempty=True)
    # Normalize both separators so a persisted Windows path remains testable
    # if the pure helper is called from another host platform.
    leaf = source_filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not leaf or leaf in {".", ".."}:
        raise IdentityFormatError("source_filename must contain a filename")
    basename = PurePath(leaf).stem
    if not basename or any(character.isspace() for character in basename):
        raise IdentityFormatError("source_filename must have a non-empty, whitespace-free basename")
    return basename


def _parse_case_name(case_name: object) -> tuple[str, int]:
    case_name = _require_string(case_name, "case_name", nonempty=True)
    match = _CASE_NAME.fullmatch(case_name)
    if match is None or len(match.group("number")) < 3:
        raise IdentityFormatError(
            "case_name must use '<source>.cNNN' with at least three decimal digits"
        )
    return match.group("source"), int(match.group("number"))


def validate_row_id(row_id: object) -> str:
    """Validate and return the fixed ``r_`` plus 32 lowercase-hex form."""

    row_id = _require_string(row_id, "row_id", nonempty=True)
    if _ROW_ID.fullmatch(row_id) is None:
        raise IdentityFormatError("row_id must match r_ followed by 32 lowercase hexadecimal digits")
    return row_id


def validate_case_name(case_name: object) -> str:
    """Validate and return a TESSY-facing case name."""

    case_name = _require_string(case_name, "case_name", nonempty=True)
    _parse_case_name(case_name)
    return case_name


def _validate_unique_strings(values: object, field_name: str, validator) -> frozenset[str]:
    items = _require_collection(values, field_name)
    seen: set[str] = set()
    for item in items:
        value = validator(item)
        if value in seen:
            raise IdentityCollisionError(f"duplicate {field_name} value: {value!r}")
        seen.add(value)
    return frozenset(seen)


def validate_row_ids(row_ids: object) -> frozenset[str]:
    """Validate a collection of row IDs and reject duplicates."""

    return _validate_unique_strings(row_ids, "row_ids", validate_row_id)


def validate_case_names(case_names: object) -> frozenset[str]:
    """Validate a collection of case names and reject duplicates."""

    items = _require_collection(case_names, "case_names")
    seen_names: set[str] = set()
    seen_numbers: dict[int, str] = {}
    for item in items:
        name = validate_case_name(item)
        source, number = _parse_case_name(name)
        del source
        if name in seen_names:
            raise IdentityCollisionError(f"duplicate case_names value: {name!r}")
        if number in seen_numbers:
            raise IdentityCollisionError(
                "case number is already used in this function: "
                f"{number} ({seen_numbers[number]!r} and {name!r})"
            )
        seen_names.add(name)
        seen_numbers[number] = name
    return frozenset(seen_names)


def validate_identity_collisions(
    row_ids: object = (),
    case_names_by_function: object | None = None,
) -> None:
    """Validate row-ID uniqueness and per-function case-name uniqueness.

    Case names intentionally have function scope.  The same external label in
    two different functions is therefore not reported as a collision here.
    """

    validate_row_ids(row_ids)
    if case_names_by_function is None:
        return
    if not isinstance(case_names_by_function, Mapping):
        raise TypeError("case_names_by_function must be a mapping")
    for function_key, names in case_names_by_function.items():
        _require_string(function_key, "function_key", nonempty=True)
        validate_case_names(names)


def row_id_from_uuid(value: object) -> str:
    """Format one UUID as the canonical persistent row ID."""

    if type(value) is not uuid.UUID:
        raise TypeError("uuid value must be a uuid.UUID")
    if value.version != 4 or value.variant != uuid.RFC_4122:
        raise IdentityFormatError("new row IDs require an RFC4122 UUID4 value")
    return f"{ROW_ID_PREFIX}{value.hex}"


def allocate_row_id(
    existing_row_ids: object = (),
    *,
    uuid_factory: UUIDFactory = uuid.uuid4,
) -> str:
    """Allocate one non-colliding row ID without mutating caller state."""

    existing = validate_row_ids(existing_row_ids)
    if not callable(uuid_factory):
        raise TypeError("uuid_factory must be callable")
    candidate = row_id_from_uuid(uuid_factory())
    validate_row_id(candidate)
    if candidate in existing:
        raise IdentityCollisionError(f"row_id already exists: {candidate!r}")
    return candidate


def allocate_case_name(
    source_filename: object,
    next_case_number: object,
    existing_names: object = (),
    *,
    on_collision: CollisionPolicy = "error",
) -> CaseNameAllocation:
    """Allocate the next per-function case name from an explicit counter.

    ``on_collision='error'`` reports stale/corrupt counter state.  With
    ``'skip'``, occupied numbers are skipped and the returned counter advances
    past the selected number.  Neither policy persists anything itself.
    """

    source = _source_basename(source_filename)
    if type(next_case_number) is not int or next_case_number < 1:
        raise IdentityFormatError("next_case_number must be a positive integer")
    if on_collision not in {"error", "skip"}:
        raise IdentityFormatError("on_collision must be 'error' or 'skip'")
    existing = validate_case_names(existing_names)
    existing_numbers = {_parse_case_name(name)[1] for name in existing}
    number = next_case_number
    while True:
        candidate = f"{source}.c{number:03d}"
        if number not in existing_numbers:
            return CaseNameAllocation(candidate, number + 1)
        if on_collision == "error":
            raise IdentityCollisionError(f"case number already exists: {number}")
        number += 1


__all__ = [
    "CaseNameAllocation",
    "CollisionPolicy",
    "IdentityCollisionError",
    "IdentityError",
    "IdentityFormatError",
    "ROW_ID_PREFIX",
    "UUIDFactory",
    "allocate_case_name",
    "allocate_row_id",
    "row_id_from_uuid",
    "validate_case_name",
    "validate_case_names",
    "validate_identity_collisions",
    "validate_row_id",
    "validate_row_ids",
]
