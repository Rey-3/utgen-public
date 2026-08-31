"""Value-XML reader, logical normalizer, and renderer for PR06.

The legacy value XML format is deliberately small: one ``<argument>`` holds
one logical argument and its comma/pipe encoded value streams.  This adapter
keeps that logical shape intact.  In particular, it does not confuse an XML
``len`` attribute with an array extent, and it never converts wire lexemes
(``0x00``, enum symbols, or ``NULL``) to a different spelling merely because
they look numeric.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree as ET

from ._xml_common import (
    LegacyXmlMetadataError,
    XmlAdapterError,
    XmlMetadata,
    XmlMetadataError,
    XmlParseError,
    VersionMetadataMismatchError,
    array_element_paths,
    clone_element,
    coerce_metadata,
    expand_array_name,
    is_null_token,
    is_semantic_zero,
    join_stream,
    local_attributes,
    parse_dimensions,
    parse_metadata,
    parse_pointer,
    parse_xml_root,
    semantic_token,
    serialize_xml,
    set_root_metadata,
    split_stream,
    stream_encoding,
)
from ..model import DisplayVersion


_MISSING = object()


class ValueXmlError(XmlAdapterError):
    """Malformed or semantically unsupported value XML."""


class ValueXmlMetadataError(XmlMetadataError, ValueXmlError):
    """Missing, invalid, or incomplete value-XML version metadata."""


@dataclass
class ValueArgument:
    """One logical value-XML argument.

    ``range`` and ``output`` are the exact raw XML attribute lexemes.  The
    normalized ``range_streams``/``output_streams`` views are available as
    properties, but are never used as the authoritative serialized value.
    ``attributes`` contains every original compatibility attribute, including
    attributes unknown to this version of UTGen.
    """

    name: str
    xml_len: str | None = None
    fmt: str | None = None
    direction: str = "in"
    pointer: bool | None = None
    range: str | None = None
    output: str | None = None
    testmethod: str | None = None
    dimensions: tuple[int, ...] = ()
    range_encoding: str | None = None
    output_encoding: str | None = None
    original_order: int = 0
    attributes: Mapping[str, str] = field(default_factory=dict)
    internal: bool = False
    synthetic_pointer_control: bool = False
    _raw_element: ET.Element | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueXmlError("value argument name must be a non-empty string")
        if type(self.direction) is not str:
            # Accept the existing ``Direction`` enum without making the
            # adapter depend on the generator model's enum implementation.
            value = getattr(self.direction, "value", None)
            if type(value) is str:
                self.direction = value
        if self.direction not in {"in", "out", "inout"}:
            raise ValueXmlError(f"unsupported argument direction {self.direction!r}")
        if self.xml_len is not None and type(self.xml_len) is not str:
            self.xml_len = str(self.xml_len)
        if self.fmt is not None and type(self.fmt) is not str:
            self.fmt = str(self.fmt)
        if self.range is not None and type(self.range) is not str:
            self.range = str(self.range)
        if self.output is not None and type(self.output) is not str:
            self.output = str(self.output)
        if self.testmethod is not None and type(self.testmethod) is not str:
            self.testmethod = str(self.testmethod)
        self.pointer = parse_pointer(self.pointer)
        if not isinstance(self.dimensions, tuple):
            self.dimensions = tuple(self.dimensions or ())
        if not self.dimensions:
            self.dimensions = parse_dimensions(self.name)
        if any(type(item) is not int or item < 0 for item in self.dimensions):
            raise ValueXmlError("array dimensions must be non-negative integers")
        self.attributes = dict(self.attributes)
        # If a caller constructs a logical argument directly, retain all
        # supplied known fields as attributes so render() can be lossless too.
        # Parsed records deliberately do not grow absent compatibility attrs;
        # preserving an omitted ``pointer``/``len`` is important when reporting
        # unresolved metadata to a later merge stage.
        if self._raw_element is None:
            self.attributes.setdefault("name", self.name)
            if self.xml_len is not None:
                self.attributes.setdefault("len", self.xml_len)
            if self.fmt is not None:
                self.attributes.setdefault("format", self.fmt)
            self.attributes.setdefault("direction", self.direction)
            if self.pointer is not None:
                self.attributes.setdefault("pointer", _pointer_text(self.pointer))
            if self.range is not None:
                self.attributes.setdefault("range", self.range)
            if self.testmethod is not None:
                self.attributes.setdefault("testmethod", self.testmethod)
            if self.output is not None:
                self.attributes.setdefault("output", self.output)
        if type(self.original_order) is not int or self.original_order < 0:
            raise ValueXmlError("original_order must be a non-negative integer")
        raw_range = (
            self._raw_element.attrib.get("range")
            if self._raw_element is not None
            else _MISSING
        )
        raw_output = (
            self._raw_element.attrib.get("output")
            if self._raw_element is not None
            else _MISSING
        )
        if self.range_encoding is None or (
            raw_range is not _MISSING and self.range != raw_range
        ):
            self.range_encoding = stream_encoding(self.range, self.dimensions)
        if self.output_encoding is None or (
            raw_output is not _MISSING and self.output != raw_output
        ):
            self.output_encoding = stream_encoding(self.output, self.dimensions, output=True)
        if self.range_encoding not in {"scalar", "shared", "expanded"}:
            raise ValueXmlError(f"unsupported range encoding {self.range_encoding!r}")
        if self.output_encoding not in {"absent", "scalar", "shared", "expanded"}:
            raise ValueXmlError(f"unsupported output encoding {self.output_encoding!r}")

    @property
    def range_streams(self) -> tuple[tuple[str, ...], ...] | None:
        return split_stream(self.range)

    @property
    def output_streams(self) -> tuple[tuple[str, ...], ...] | None:
        return split_stream(self.output)

    def expanded_range_streams(self) -> tuple[tuple[str, ...], ...] | None:
        """Return one logical stream per array element without changing encoding."""

        groups = self.range_streams
        if groups is None:
            return None
        if not self.dimensions:
            return groups
        if len(groups) == 1:
            return groups * self.element_count
        if len(groups) != self.element_count:
            raise ValueXmlError(
                f"range stream count {len(groups)} does not match array element count {self.element_count}"
            )
        return groups

    def expanded_output_streams(self) -> tuple[tuple[str, ...], ...] | None:
        groups = self.output_streams
        if groups is None:
            return None
        if not self.dimensions:
            return groups
        if len(groups) == 1:
            return groups * self.element_count
        if len(groups) != self.element_count:
            raise ValueXmlError(
                f"output stream count {len(groups)} does not match array element count {self.element_count}"
            )
        return groups

    def element_range_stream(self, element: int | tuple[int, ...] = 0) -> tuple[str, ...] | None:
        groups = self.expanded_range_streams()
        if groups is None:
            return None
        if isinstance(element, tuple):
            try:
                element = self.element_paths.index(element)
            except ValueError:
                return None
        try:
            return groups[element]
        except IndexError:
            return None

    def element_output_stream(self, element: int | tuple[int, ...] = 0) -> tuple[str, ...] | None:
        groups = self.expanded_output_streams()
        if groups is None:
            return None
        if isinstance(element, tuple):
            try:
                element = self.element_paths.index(element)
            except ValueError:
                return None
        try:
            return groups[element]
        except IndexError:
            return None

    def with_element_value(
        self,
        element: int | tuple[int, ...],
        testcase: int,
        value: str,
        *,
        output: bool = False,
    ) -> "ValueArgument":
        """Return a copy after editing one logical array/scalar cell.

        This helper is intentionally stream-oriented: it changes only the
        selected lexeme, then chooses shared versus expanded encoding from the
        resulting element streams.  It never performs substring replacement on
        a serialized pipe/comma value.
        """

        if type(value) is not str:
            raise ValueXmlError("array cell value must retain a string lexeme")
        if type(testcase) is not int or testcase < 0:
            raise ValueXmlError("testcase index must be non-negative")
        paths = self.element_paths
        if isinstance(element, tuple):
            try:
                element_index = paths.index(element)
            except ValueError as exc:
                raise ValueXmlError("array element path is outside dimensions") from exc
        else:
            element_index = element
        if type(element_index) is not int or element_index < 0 or element_index >= len(paths):
            raise ValueXmlError("array element index is outside dimensions")
        groups = (
            self.expanded_output_streams() if output else self.expanded_range_streams()
        )
        if groups is None:
            raise ValueXmlError("the selected stream is absent")
        mutable = [list(group) for group in groups]
        if testcase >= len(mutable[element_index]):
            raise ValueXmlError("testcase index is outside the selected stream")
        mutable[element_index][testcase] = value
        changed_groups = tuple(tuple(group) for group in mutable)
        shared = bool(changed_groups) and all(group == changed_groups[0] for group in changed_groups[1:])
        if not self.dimensions:
            encoded = join_stream(changed_groups)
            encoding = "scalar"
        elif shared:
            encoded = join_stream((changed_groups[0],))
            encoding = "shared"
        else:
            encoded = join_stream(changed_groups)
            encoding = "expanded"
        if output:
            attributes = dict(self.attributes)
            attributes["output"] = encoded
            return replace(self, output=encoded, output_encoding=encoding, attributes=attributes)
        attributes = dict(self.attributes)
        attributes["range"] = encoded
        return replace(self, range=encoded, range_encoding=encoding, attributes=attributes)

    @property
    def is_array(self) -> bool:
        return bool(self.dimensions)

    @property
    def element_count(self) -> int:
        count = 1
        for size in self.dimensions:
            count *= size
        return count

    @property
    def element_paths(self) -> tuple[tuple[int, ...], ...]:
        return array_element_paths(self.dimensions)

    @property
    def expanded_names(self) -> tuple[str, ...]:
        return tuple(expand_array_name(self.name))

    @property
    def extra_attributes(self) -> dict[str, str]:
        return {
            key: value
            for key, value in self.attributes.items()
            if key not in {"name", "len", "format", "direction", "pointer", "range", "output", "testmethod"}
        }

    @property
    def raw_attributes(self) -> dict[str, str]:
        return dict(self.attributes)

    @property
    def range_lexemes(self) -> tuple[str, ...]:
        return tuple(token for group in (self.range_streams or ()) for token in group)

    @property
    def output_lexemes(self) -> tuple[str, ...]:
        return tuple(token for group in (self.output_streams or ()) for token in group)

    def range_token(self, element: int = 0, testcase: int = 0) -> str | None:
        groups = self.range_streams
        if groups is None or element < 0 or testcase < 0:
            return None
        try:
            return groups[element][testcase]
        except IndexError:
            return None

    def output_token(self, element: int = 0, testcase: int = 0) -> str | None:
        groups = self.output_streams
        if groups is None or element < 0 or testcase < 0:
            return None
        try:
            return groups[element][testcase]
        except IndexError:
            return None

    def input_is_null(self, testcase: int = 0) -> bool:
        """Return whether a pointer's input stream carries a NULL lexeme."""

        if not self.pointer:
            return False
        return is_null_token(self.range_token(0, testcase))

    def semantic_input(self, testcase: int = 0) -> str | None:
        return semantic_token(self.range_token(0, testcase))

    def semantic_output(self, testcase: int = 0) -> str | None:
        return semantic_token(self.output_token(0, testcase))

    @property
    def payload(self) -> dict[str, Any]:
        """A stable semantic/payload projection useful to callers and tests."""

        return {
            "name": self.name,
            "len": self.xml_len,
            "format": self.fmt,
            "direction": self.direction,
            "pointer": self.pointer,
            "range": self.range,
            "output": self.output,
            "testmethod": self.testmethod,
            "dimensions": self.dimensions,
            "range_encoding": self.range_encoding,
            "output_encoding": self.output_encoding,
            "original_order": self.original_order,
            "attributes": dict(self.attributes),
        }

    def with_values(self, *, range: str | None = None, output: str | None = None) -> "ValueArgument":  # noqa: A002
        """Return a copy with wire lexemes changed explicitly."""

        attributes = dict(self.attributes)
        if range is None:
            attributes.pop("range", None)
        else:
            attributes["range"] = range
        if output is None:
            attributes.pop("output", None)
        else:
            attributes["output"] = output
        return replace(
            self,
            range=range,
            output=output,
            range_encoding=stream_encoding(range, self.dimensions),
            output_encoding=stream_encoding(output, self.dimensions, output=True),
            attributes=attributes,
        )


@dataclass(frozen=True)
class LogicalValueRow:
    """A convenience per-testcase view; XML streams remain authoritative."""

    index: int
    values: Mapping[str, str | None]
    expected: Mapping[str, str | None]


@dataclass
class FunctionSnapshot:
    """Normalized value XML for one Test Object/function."""

    name: str
    file: str = ""
    arguments: tuple[ValueArgument, ...] = ()
    testmethod: str | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)
    display_version: DisplayVersion | None = None
    root_tag: str = "function"
    legacy_metadata: bool = False
    _raw_root: ET.Element | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueXmlError("function name must be a non-empty string")
        if type(self.file) is not str:
            self.file = str(self.file)
        self.arguments = tuple(self.arguments)
        if not all(isinstance(item, ValueArgument) for item in self.arguments):
            raise ValueXmlError("arguments must contain ValueArgument records")
        self.attributes = dict(self.attributes)
        if self._raw_root is None:
            self.attributes.setdefault("name", self.name)
            self.attributes.setdefault("file", self.file)
            if self.testmethod is not None:
                self.attributes.setdefault("testmethod", self.testmethod)
        if self.display_version is not None and not isinstance(self.display_version, DisplayVersion):
            # XmlMetadata is accepted as a friendly adapter-boundary input.
            if isinstance(self.display_version, XmlMetadata):
                self.display_version = self.display_version.display_version
            else:
                raise ValueXmlError("display_version must be a DisplayVersion")
        if self.root_tag != "function":
            raise ValueXmlError("value snapshots must use the <function> root")

    @property
    def metadata(self) -> XmlMetadata | None:
        return XmlMetadata(self.display_version) if self.display_version is not None else None

    @property
    def version(self) -> str | None:
        return self.metadata.version if self.metadata is not None else None

    @property
    def updated_at(self) -> str | None:
        return self.metadata.updated_at if self.metadata is not None else None

    @property
    def utgen_version(self) -> str | None:
        return self.version

    @property
    def utgen_updated_at(self) -> str | None:
        return self.updated_at

    @property
    def root_attributes(self) -> dict[str, str]:
        return dict(self.attributes)

    @property
    def attrs(self) -> dict[str, str]:
        return self.root_attributes

    @property
    def raw_root(self) -> ET.Element | None:
        return clone_element(self._raw_root)

    @property
    def raw_xml(self) -> bytes | None:
        return ET.tostring(self._raw_root, encoding="utf-8") if self._raw_root is not None else None

    @property
    def row_count(self) -> int:
        lengths = [
            len(group)
            for argument in self.arguments
            for streams in (argument.range_streams, argument.output_streams)
            if streams is not None
            for group in streams
        ]
        return max(lengths, default=0)

    @property
    def payload(self) -> tuple[dict[str, Any], ...]:
        return tuple(argument.payload for argument in self.arguments)

    def logical_rows(self) -> tuple[LogicalValueRow, ...]:
        """Build rows without converting or reformatting any token lexemes."""

        rows: list[LogicalValueRow] = []
        for index in range(self.row_count):
            values: dict[str, str | None] = {}
            expected: dict[str, str | None] = {}
            for argument in self.arguments:
                names = argument.expanded_names
                input_groups = argument.expanded_range_streams()
                output_groups = argument.expanded_output_streams()
                for element, name in enumerate(names):
                    values[name] = (
                        input_groups[element][index]
                        if input_groups is not None
                        and element < len(input_groups)
                        and index < len(input_groups[element])
                        else None
                    )
                    expected[name] = (
                        output_groups[element][index]
                        if output_groups is not None
                        and element < len(output_groups)
                        and index < len(output_groups[element])
                        else None
                    )
            rows.append(LogicalValueRow(index, values, expected))
        return tuple(rows)

    @property
    def rows(self) -> tuple[LogicalValueRow, ...]:
        return self.logical_rows()

    def with_metadata(self, display_version: DisplayVersion) -> "FunctionSnapshot":
        return replace(self, display_version=display_version, legacy_metadata=False)


@dataclass(frozen=True)
class PointerBinding:
    """One logical pointer-control to pointee-storage binding.

    The projection is deliberately independent from workbook coordinates or
    merge state.  ``argument_name`` is the XML logical argument; an array
    produces one ``element_name``/``element_path`` binding per element.
    ``grouping`` is either ``whole_struct`` (one base control for all pointer
    members) or ``member`` (a member-specific control).  ``storage_name`` is
    the actual pointee cell, while ``expected_storage_name`` is the exact
    non-NULL control alias (for a whole struct it is ``tessy_<base>``).
    """

    argument_name: str
    element_name: str
    control_name: str
    storage_name: str
    grouping: str
    element_path: tuple[int, ...] = ()
    expected_storage_name: str | None = None

    def __post_init__(self) -> None:
        if self.grouping not in {"whole_struct", "member"}:
            raise ValueXmlError(f"unsupported pointer grouping {self.grouping!r}")
        if not isinstance(self.element_path, tuple):
            object.__setattr__(self, "element_path", tuple(self.element_path))
        if self.expected_storage_name is None:
            object.__setattr__(self, "expected_storage_name", self.storage_name)

    @property
    def xml_name(self) -> str:
        return self.element_name

    @property
    def control(self) -> str:
        return self.control_name

    @property
    def storage(self) -> str:
        return self.storage_name

    @property
    def expected_storage(self) -> str:
        return self.expected_storage_name or self.storage_name

    @property
    def control_storage_name(self) -> str:
        return self.expected_storage

    @property
    def is_whole_struct(self) -> bool:
        return self.grouping == "whole_struct"

    def normalize(
        self,
        control: str | None,
        storage: str | int | float | None,
    ) -> str:
        """Normalize one TESSY cell pair using this binding's exact alias."""

        return normalize_pointer_binding(
            control,
            storage,
            expected_storage=self.expected_storage,
        )


@dataclass(frozen=True)
class PointerProjection:
    """Pure logical pointer projection for one value snapshot."""

    bindings: tuple[PointerBinding, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bindings", tuple(self.bindings))
        if not all(isinstance(item, PointerBinding) for item in self.bindings):
            raise ValueXmlError("pointer projections require PointerBinding records")

    @classmethod
    def from_snapshot(
        cls,
        snapshot: FunctionSnapshot,
        *,
        whole_struct_bases: Iterable[str] | Mapping[str, bool] | None = None,
    ) -> "PointerProjection":
        return project_pointer_bindings(
            snapshot,
            whole_struct_bases=whole_struct_bases,
        )

    @classmethod
    def from_arguments(
        cls,
        arguments: Iterable[ValueArgument],
        *,
        whole_struct_bases: Iterable[str] | Mapping[str, bool] | None = None,
    ) -> "PointerProjection":
        return project_pointer_bindings(
            arguments,
            whole_struct_bases=whole_struct_bases,
        )

    @property
    def controls(self) -> tuple[str, ...]:
        return _unique_names(binding.control_name for binding in self.bindings)

    @property
    def storages(self) -> tuple[str, ...]:
        return _unique_names(binding.storage_name for binding in self.bindings)

    @property
    def control_storages(self) -> tuple[str, ...]:
        return _unique_names(binding.expected_storage for binding in self.bindings)

    @property
    def pointer_bindings(self) -> tuple[PointerBinding, ...]:
        return self.bindings

    @property
    def by_argument(self) -> dict[str, tuple[PointerBinding, ...]]:
        result: dict[str, list[PointerBinding]] = {}
        for binding in self.bindings:
            result.setdefault(binding.argument_name, []).append(binding)
        return {name: tuple(items) for name, items in result.items()}

    def for_argument(self, argument_name: str) -> tuple[PointerBinding, ...]:
        return self.by_argument.get(argument_name, ())

    def binding_for(
        self,
        argument_name: str,
        element_path: tuple[int, ...] | None = None,
    ) -> PointerBinding | None:
        candidates = self.for_argument(argument_name)
        if element_path is None:
            return candidates[0] if candidates else None
        path = tuple(element_path)
        return next((item for item in candidates if item.element_path == path), None)

    def __iter__(self):
        return iter(self.bindings)

    def __len__(self) -> int:
        return len(self.bindings)


_POINTER_INDEX = re.compile(r"\[[0-9]+\]")


def _unique_names(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _pointer_base_key(name: str) -> str:
    return _POINTER_INDEX.sub("", name.split(".", 1)[0])


def _pointer_member_name(name: str) -> str:
    return _POINTER_INDEX.sub("", name.rsplit(".", 1)[-1])


def _requested_pointer_grouping(argument: ValueArgument) -> str | None:
    """Read an optional explicit grouping hint without adding a new wire attr."""

    for key in ("pointer_grouping", "pointer_group", "grouping"):
        value = argument.attributes.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"whole", "whole_struct", "struct"}:
            return "whole_struct"
        if normalized in {"member", "member_only", "member_pointer"}:
            return "member"
    return None


def _source_arguments(source: Any) -> tuple[ValueArgument, ...]:
    if isinstance(source, FunctionSnapshot):
        return source.arguments
    arguments = getattr(source, "arguments", source)
    try:
        result = tuple(arguments)
    except TypeError as exc:
        raise TypeError("pointer projection source must be a FunctionSnapshot or iterable") from exc
    if not all(isinstance(item, ValueArgument) for item in result):
        raise TypeError("pointer projection arguments must be ValueArgument records")
    return result


def project_pointer_bindings(
    source: FunctionSnapshot | Iterable[ValueArgument],
    *,
    whole_struct_bases: Iterable[str] | Mapping[str, bool] | None = None,
) -> PointerProjection:
    """Project XML pointer arguments into TESSY control/storage names.

    Whole-struct grouping follows ``tessy_parse.py``: a base is whole-struct
    only when every known argument under that base is marked ``pointer=true``.
    Array arguments use the member-specific ``tessy_<member>_array[index]``
    compatibility spelling unless an explicit grouping hint or override is
    supplied.  No workbook column, testcase, or merge policy is involved.
    """

    arguments = _source_arguments(source)
    # Legacy XML was emitted by the original parser, whose whole-struct
    # decision used only the members present in that XML.  Keep that exact
    # projection for explicitly migrated legacy snapshots so PR06 artifacts
    # remain stable.  New normalized/PR09 projections use the safer default
    # below: a lone dotted member is member-level unless callers explicitly
    # identify its base as whole-struct.
    legacy_whole_struct = isinstance(source, FunctionSnapshot) and source.legacy_metadata
    whole_flags: dict[str, bool] = {}
    pointer_member_counts: dict[str, int] = {}
    for argument in arguments:
        key = _pointer_base_key(argument.name)
        whole_flags[key] = whole_flags.get(key, True) and argument.pointer is True
        if argument.pointer is True and not argument.dimensions and "." in argument.name:
            pointer_member_counts[key] = pointer_member_counts.get(key, 0) + 1

    forced: dict[str, bool] = {}
    if whole_struct_bases is not None:
        if isinstance(whole_struct_bases, Mapping):
            forced = {
                _pointer_base_key(str(name)): bool(value)
                for name, value in whole_struct_bases.items()
            }
        else:
            if isinstance(whole_struct_bases, str):
                whole_struct_bases = (whole_struct_bases,)
            forced = {
                _pointer_base_key(str(name)): True
                for name in whole_struct_bases
            }

    bindings: list[PointerBinding] = []
    for argument in arguments:
        if (
            argument.pointer is not True
            or argument.internal
            or argument.synthetic_pointer_control
            or "@address" in argument.name
        ):
            continue
        key = _pointer_base_key(argument.name)
        requested = _requested_pointer_grouping(argument)
        if requested is not None:
            grouping = requested
        elif key in forced:
            grouping = "whole_struct" if forced[key] else "member"
        else:
            # A pointer array is a member-level storage array in the current
            # compatibility format.  A scalar dotted member defaults to the
            # member aliases when it is the only known pointer member.  The
            # whole-struct form is reserved for a base with multiple pointer
            # members (or an explicit whole_struct override).
            grouping = (
                "whole_struct"
                if not argument.dimensions
                and "." in argument.name
                and whole_flags.get(key, False)
                and (
                    legacy_whole_struct
                    or pointer_member_counts.get(key, 0) > 1
                )
                else "member"
            )

        names = argument.expanded_names
        paths = argument.element_paths
        for index, element_name in enumerate(names):
            element_path = paths[index] if index < len(paths) else (index,)
            if grouping == "whole_struct":
                control_name = _pointer_base_key(element_name)
                storage_name = f"tessy_{element_name}"
                expected_storage_name = f"tessy_{control_name}"
            else:
                control_name = element_name
                member = _pointer_member_name(element_name)
                if argument.dimensions:
                    storage_name = f"tessy_{member}_array[{index}]"
                else:
                    storage_name = f"tessy_{member}"
                expected_storage_name = storage_name
            bindings.append(
                PointerBinding(
                    argument_name=argument.name,
                    element_name=element_name,
                    control_name=control_name,
                    storage_name=storage_name,
                    grouping=grouping,
                    element_path=element_path,
                    expected_storage_name=expected_storage_name,
                )
            )
    return PointerProjection(tuple(bindings))


def _pointer_text(value: bool) -> str:
    return "true" if value else "false"


def _original_argument_value(argument: ValueArgument, key: str) -> object:
    raw = argument._raw_element
    if raw is None:
        return _MISSING
    if key in raw.attrib:
        value = raw.attrib[key]
        if key == "pointer":
            return parse_pointer(value)
        return value
    # XML's direction default is the only known field with a non-None
    # effective value when its attribute is absent.
    return "in" if key == "direction" else None


def _sync_argument_attribute(
    attrs: dict[str, str],
    argument: ValueArgument,
    key: str,
    value: object,
    serialized: str | None,
) -> None:
    """Make a known value attribute follow typed state without growing raw XML."""

    original = _original_argument_value(argument, key)
    raw = argument._raw_element
    if original is _MISSING:
        if serialized is None:
            attrs.pop(key, None)
        else:
            attrs[key] = serialized
        return
    if raw is not None and key not in raw.attrib:
        # Preserve an absent optional attribute until its effective typed
        # value is actually edited (direction defaults to ``in``).
        if value == original or value is None:
            attrs.pop(key, None)
        elif serialized is not None:
            attrs[key] = serialized
        return
    if value == original and raw is not None and key in raw.attrib:
        # Keep the original spelling (for example a non-canonical but valid
        # pointer boolean lexeme) when the accepted typed value was not edited.
        attrs[key] = raw.attrib[key]
        return
    if value is None or serialized is None:
        attrs.pop(key, None)
    else:
        attrs[key] = serialized


def normalize_pointer_binding(
    control: str | None,
    storage: str | int | float | None,
    *,
    expected_storage: str | None = None,
) -> str:
    """Convert a logical TESSY pointer control/storage pair to XML range.

    ``NULL`` is accepted only with a semantic zero storage value.  For a
    non-NULL control must exactly name the expected ``tessy_*`` storage alias,
    and the pointee's exact storage lexeme is returned unchanged.
    """

    control_token = semantic_token(control)
    if control_token is None:
        raise ValueXmlError("pointer control is missing")
    if is_null_token(control_token):
        if not is_semantic_zero(storage):
            raise ValueXmlError("NULL pointer control requires semantic-zero storage")
        return "NULL"
    # NULL is a semantic token, but a non-NULL binding is an exact TESSY
    # alias.  Do not strip whitespace or otherwise normalize the control here;
    # doing so would silently accept a binding to the wrong storage channel.
    control_lexeme = control if isinstance(control, str) else str(control)
    if expected_storage is None or control_lexeme != expected_storage:
        raise ValueXmlError("invalid pointer control binding")
    if storage is None or (isinstance(storage, str) and not storage.strip()):
        raise ValueXmlError("non-NULL pointer control requires storage")
    return str(storage)


def _render_argument(argument: ValueArgument) -> ET.Element:
    """Render one logical argument while retaining unknown/raw XML details."""

    element = clone_element(argument._raw_element)
    if element is None:
        element = ET.Element("argument")
    element.tag = "argument"
    attrs = dict(argument.attributes)
    # Typed fields are authoritative for known compatibility attributes; all
    # other attributes are copied unchanged from the parsed record.
    _sync_argument_attribute(attrs, argument, "name", argument.name, argument.name)
    _sync_argument_attribute(attrs, argument, "len", argument.xml_len, argument.xml_len)
    _sync_argument_attribute(attrs, argument, "format", argument.fmt, argument.fmt)
    _sync_argument_attribute(
        attrs, argument, "direction", argument.direction, argument.direction
    )
    pointer_text = _pointer_text(argument.pointer) if argument.pointer is not None else None
    _sync_argument_attribute(attrs, argument, "pointer", argument.pointer, pointer_text)
    _sync_argument_attribute(attrs, argument, "range", argument.range, argument.range)
    _sync_argument_attribute(attrs, argument, "output", argument.output, argument.output)
    _sync_argument_attribute(
        attrs, argument, "testmethod", argument.testmethod, argument.testmethod
    )
    element.attrib.clear()
    element.attrib.update(attrs)
    return element


def _legacy_opt_in(
    adapter_default: bool,
    allow_legacy: bool | None,
    *,
    bootstrap: bool = False,
    migration: bool = False,
    allow_legacy_metadata: bool = False,
    legacy: bool = False,
    mode: str | None = None,
) -> bool:
    if mode is not None and mode not in {"bootstrap", "migration", "legacy", "strict"}:
        raise ValueXmlError(f"unsupported XML read mode {mode!r}")
    return bool(
        adapter_default
        or allow_legacy
        or bootstrap
        or migration
        or allow_legacy_metadata
        or legacy
        or mode in {"bootstrap", "migration", "legacy"}
    )


def _validate_stream_shape(argument: ValueArgument) -> None:
    """Reject an ambiguous pipe stream for an array argument.

    A valid array is either encoded as one shared stream or as one stream for
    every Cartesian-product element.  ``len`` is deliberately absent from
    this calculation.
    """

    if not argument.dimensions:
        return
    expected = argument.element_count
    for label, groups in (("range", argument.range_streams), ("output", argument.output_streams)):
        if groups is None or len(groups) == 1:
            continue
        if len(groups) != expected:
            raise ValueXmlError(
                f"{label} stream count {len(groups)} does not match array element count {expected}"
            )
        lengths = {len(group) for group in groups}
        if len(lengths) > 1:
            raise ValueXmlError(
                f"{label} expanded streams have inconsistent testcase lengths"
            )


class ValueXmlAdapter:
    """Read and render logical value XML.

    Missing root metadata is rejected by default.  ``allow_legacy=True`` (or
    the explicit ``bootstrap=True``/``migration=True`` spelling) is the only
    opt-in path for old fixtures; rendering still always requires a valid
    DisplayVersion and timestamp.
    """

    def __init__(self, *, allow_legacy: bool = False, validate_shapes: bool = True) -> None:
        self.allow_legacy = bool(allow_legacy)
        self.validate_shapes = bool(validate_shapes)

    def read(
        self,
        value: Any,
        *,
        allow_legacy: bool | None = None,
        bootstrap: bool = False,
        migration: bool = False,
        allow_legacy_metadata: bool = False,
        legacy: bool = False,
        mode: str | None = None,
    ) -> FunctionSnapshot:
        legacy_ok = _legacy_opt_in(
            self.allow_legacy,
            allow_legacy,
            bootstrap=bootstrap,
            migration=migration,
            allow_legacy_metadata=allow_legacy_metadata,
            legacy=legacy,
            mode=mode,
        )
        try:
            root = parse_xml_root(value, "function")
        except XmlParseError as exc:
            raise ValueXmlError(str(exc)) from exc
        try:
            metadata = parse_metadata(root.attrib, allow_legacy=legacy_ok)
        except XmlMetadataError as exc:
            raise ValueXmlMetadataError(str(exc)) from exc
        name = root.attrib.get("name")
        if not name:
            raise ValueXmlError("value XML root is missing function name")
        arguments: list[ValueArgument] = []
        for order, child in enumerate(list(root)):
            if child.tag != "argument":
                continue
            attrs = local_attributes(child)
            argument_name = attrs.get("name")
            if not argument_name:
                raise ValueXmlError("value XML argument is missing name")
            # @address is an internal generator pseudo-leaf.  Treating it as
            # an external argument would leak implementation state on the next
            # render, so it is intentionally omitted from the logical model.
            if "@address" in argument_name:
                continue
            direction = attrs.get("direction", "in")
            pointer_raw = attrs.get("pointer")
            try:
                pointer = parse_pointer(pointer_raw)
            except XmlParseError as exc:
                raise ValueXmlError(str(exc)) from exc
            try:
                argument = ValueArgument(
                    name=argument_name,
                    xml_len=attrs.get("len"),
                    fmt=attrs.get("format"),
                    direction=direction,
                    pointer=pointer,
                    range=attrs.get("range"),
                    output=attrs.get("output"),
                    testmethod=attrs.get("testmethod"),
                    dimensions=parse_dimensions(argument_name),
                    original_order=order,
                    attributes=attrs,
                    _raw_element=clone_element(child),
                )
            except ValueXmlError:
                raise
            except Exception as exc:
                raise ValueXmlError(f"invalid value XML argument {argument_name!r}") from exc
            if self.validate_shapes:
                _validate_stream_shape(argument)
            arguments.append(argument)
        return FunctionSnapshot(
            name=name,
            file=root.attrib.get("file", ""),
            arguments=tuple(arguments),
            testmethod=root.attrib.get("testmethod"),
            attributes=local_attributes(root),
            display_version=metadata.display_version if metadata is not None else None,
            legacy_metadata=metadata is None,
            _raw_root=clone_element(root),
        )

    def read_legacy(self, value: Any, **kwargs: Any) -> FunctionSnapshot:
        """Explicit migration/bootstrap spelling for legacy XML."""

        kwargs["allow_legacy"] = True
        return self.read(value, **kwargs)

    def render(
        self,
        snapshot: FunctionSnapshot,
        *,
        display_version: DisplayVersion | XmlMetadata | str | None = None,
        version: DisplayVersion | XmlMetadata | str | None = None,
        updated_at: str | None = None,
        metadata: DisplayVersion | XmlMetadata | None = None,
    ) -> bytes:
        if not isinstance(snapshot, FunctionSnapshot):
            raise TypeError("snapshot must be a FunctionSnapshot")
        if metadata is not None:
            if display_version is not None or version is not None:
                raise ValueXmlMetadataError("metadata cannot be combined with display_version/version")
            display_version = metadata
        try:
            metadata = coerce_metadata(
                snapshot.metadata,
                display_version=display_version,
                version=version,
                updated_at=updated_at,
            )
        except (XmlMetadataError, TypeError) as exc:
            raise ValueXmlMetadataError(str(exc)) from exc

        # A snapshot can be assembled or edited without passing through
        # ``read``.  Validate pointer metadata and logical streams at the
        # render boundary, before any raw XML payload is copied.
        for argument in snapshot.arguments:
            if (
                argument.internal
                or argument.synthetic_pointer_control
                or "@address" in argument.name
            ):
                continue
            if argument._raw_element is None and argument.pointer is None:
                raise ValueXmlError(
                    f"pointer metadata is unresolved for new argument {argument.name!r}"
                )
            if self.validate_shapes:
                _validate_stream_shape(argument)

        root = clone_element(snapshot._raw_root)
        if root is None:
            root = ET.Element("function")
        root.tag = "function"
        # Snapshot attributes are the compatibility-attribute authority.  A
        # caller can therefore retain arbitrary attrs while changing the
        # function name/file through the typed fields.
        root.attrib.clear()
        root.attrib.update(dict(snapshot.attributes))
        root.set("name", snapshot.name)
        if snapshot._raw_root is None or "file" in snapshot.attributes:
            root.set("file", snapshot.file)
        if snapshot.testmethod is not None:
            root.set("testmethod", snapshot.testmethod)
        else:
            root.attrib.pop("testmethod", None)
        set_root_metadata(root, metadata)

        rendered_arguments = [
            _render_argument(argument)
            for argument in snapshot.arguments
            if not argument.internal
            and not argument.synthetic_pointer_control
            and "@address" not in argument.name
        ]
        # Replace known argument elements in their original slots.  Unknown
        # non-argument children retain order and payload.  Extra source
        # arguments are intentionally removed when they are not represented in
        # the normalized snapshot (the snapshot is the accepted state).
        children: list[ET.Element] = []
        argument_index = 0
        for child in list(root):
            if child.tag != "argument":
                children.append(child)
                continue
            child_name = child.attrib.get("name", "")
            if "@address" in child_name:
                continue
            if argument_index < len(rendered_arguments):
                children.append(rendered_arguments[argument_index])
                argument_index += 1
        children.extend(rendered_arguments[argument_index:])
        root[:] = children
        payload = serialize_xml(root)
        # This final guard protects against future changes that accidentally
        # add a pseudo-leaf through an unknown/raw compatibility path.
        try:
            check_root = ET.fromstring(payload)
            leaked = [
                element.attrib.get("name", "")
                for element in check_root.findall("argument")
                if "@address" in element.attrib.get("name", "")
            ]
        except ET.ParseError as exc:  # pragma: no cover - serializer invariant
            raise ValueXmlError("rendered value XML is malformed") from exc
        if leaked:
            raise ValueXmlError("internal @address argument leaked into value XML")
        return payload

    def write(
        self,
        snapshot: FunctionSnapshot,
        path: Any,
        *,
        display_version: DisplayVersion | XmlMetadata | str | None = None,
        version: DisplayVersion | XmlMetadata | str | None = None,
        updated_at: str | None = None,
        metadata: DisplayVersion | XmlMetadata | None = None,
    ) -> None:
        self.write_file(
            snapshot,
            path,
            display_version=display_version,
            version=version,
            updated_at=updated_at,
            metadata=metadata,
        )

    def write_file(
        self,
        snapshot: FunctionSnapshot,
        path: Any,
        *,
        display_version: DisplayVersion | XmlMetadata | str | None = None,
        version: DisplayVersion | XmlMetadata | str | None = None,
        updated_at: str | None = None,
        metadata: DisplayVersion | XmlMetadata | None = None,
    ) -> None:
        payload = self.render(
            snapshot,
            display_version=display_version,
            version=version,
            updated_at=updated_at,
            metadata=metadata,
        )
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def read_value_xml(value: Any, **kwargs: Any) -> FunctionSnapshot:
    return ValueXmlAdapter().read(value, **kwargs)


def read(value: Any, **kwargs: Any) -> FunctionSnapshot:
    return read_value_xml(value, **kwargs)


def render_value_xml(
    snapshot: FunctionSnapshot,
    *,
    display_version: DisplayVersion | XmlMetadata | str | None = None,
    version: DisplayVersion | XmlMetadata | str | None = None,
    updated_at: str | None = None,
    metadata: DisplayVersion | XmlMetadata | None = None,
) -> bytes:
    return ValueXmlAdapter().render(
        snapshot,
        display_version=display_version,
        version=version,
        updated_at=updated_at,
        metadata=metadata,
    )


def render(
    snapshot: FunctionSnapshot,
    *,
    display_version: DisplayVersion | XmlMetadata | str | None = None,
    version: DisplayVersion | XmlMetadata | str | None = None,
    updated_at: str | None = None,
    metadata: DisplayVersion | XmlMetadata | None = None,
) -> bytes:
    return render_value_xml(
        snapshot,
        display_version=display_version,
        version=version,
        updated_at=updated_at,
        metadata=metadata,
    )


__all__ = [
    "FunctionSnapshot",
    "LegacyXmlMetadataError",
    "LogicalValueRow",
    "PointerBinding",
    "PointerProjection",
    "ValueArgument",
    "ValueXmlAdapter",
    "ValueXmlError",
    "ValueXmlMetadataError",
    "VersionMetadataMismatchError",
    "XmlAdapterError",
    "XmlMetadata",
    "XmlMetadataError",
    "XmlParseError",
    "project_pointer_bindings",
    "read_value_xml",
    "render_value_xml",
    "read",
    "render",
    "split_stream",
    "join_stream",
    "is_null_token",
    "is_semantic_zero",
    "normalize_pointer_binding",
]
