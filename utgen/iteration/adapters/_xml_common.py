"""Small, format-independent helpers for the PR06 XML adapters.

The value and stub XML files are two different wire formats.  They share only
the rules around safe parsing and the user-visible version metadata.  Keeping
those rules here prevents the two adapters from slowly acquiring subtly
different interpretations of ``utgen_version`` and ``utgen_updated_at``.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
import re
from itertools import product
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from ..model import DisplayVersion
from ..versioning import VersioningError, canonicalize_timestamp, format_display_version, parse_display_version


class XmlAdapterError(ValueError):
    """Base error raised by the PR06 XML adapters."""


class XmlParseError(XmlAdapterError):
    """The input is not a supported, safe XML document."""


class XmlMetadataError(XmlAdapterError):
    """The required user-visible version metadata is missing or invalid."""


class LegacyXmlMetadataError(XmlMetadataError):
    """Legacy XML was read without an explicit bootstrap/migration opt-in."""


class VersionMetadataMismatchError(XmlMetadataError):
    """Two accepted XML artifacts do not carry the same display metadata."""


_UNSAFE_XML_DECLARATION = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_DIMENSION = re.compile(r"\[([0-9]+)\]")
_METADATA_NAMES = frozenset({"utgen_version", "utgen_updated_at"})


@dataclass(frozen=True)
class XmlMetadata:
    """The strict pair mirrored on every accepted value/stub XML root."""

    display_version: DisplayVersion

    @property
    def version(self) -> str:
        return format_display_version(self.display_version)

    @property
    def updated_at(self) -> str:
        return self.display_version.updated_at

    @property
    def utgen_version(self) -> str:
        return self.version

    @property
    def utgen_updated_at(self) -> str:
        return self.updated_at


def read_xml_bytes(value: Any) -> bytes:
    """Read a path, bytes, or binary stream without following unsafe paths."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.is_symlink() or not path.is_file():
            raise XmlParseError(f"XML input is not a regular file: {path}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise XmlParseError(f"cannot read XML input: {path}") from exc
    read = getattr(value, "read", None)
    if callable(read):
        try:
            payload = read()
        except Exception as exc:  # pragma: no cover - defensive stream boundary
            raise XmlParseError("cannot read XML stream") from exc
        if isinstance(payload, str):
            return payload.encode("utf-8")
        if isinstance(payload, (bytes, bytearray, memoryview)):
            return bytes(payload)
    raise TypeError("XML input must be a path, bytes, or a readable binary stream")


def parse_xml_root(value: Any, expected_tag: str) -> ET.Element:
    """Parse one XML root with the deliberately small PR06 security surface."""

    payload = read_xml_bytes(value)
    if _UNSAFE_XML_DECLARATION.search(payload):
        raise XmlParseError("DOCTYPE and ENTITY declarations are not supported")
    try:
        # ElementTree does not resolve external entities, and declarations are
        # rejected above as an additional explicit guard at this boundary.
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError) as exc:
        raise XmlParseError("XML input is malformed") from exc
    if root.tag != expected_tag:
        raise XmlParseError(f"expected root <{expected_tag}>, got <{root.tag}>")
    return root


def parse_metadata(
    attributes: Mapping[str, str],
    *,
    allow_legacy: bool = False,
) -> XmlMetadata | None:
    """Validate the root metadata pair, optionally for explicit legacy reads."""

    has_version = "utgen_version" in attributes
    has_timestamp = "utgen_updated_at" in attributes
    if not has_version and not has_timestamp:
        if allow_legacy:
            return None
        raise LegacyXmlMetadataError(
            "legacy XML without utgen_version/utgen_updated_at requires explicit bootstrap or migration"
        )
    if has_version != has_timestamp:
        raise XmlMetadataError(
            "utgen_version and utgen_updated_at must be present together"
        )
    try:
        display = parse_display_version(
            attributes["utgen_version"], attributes["utgen_updated_at"]
        )
    except (KeyError, VersioningError) as exc:
        raise XmlMetadataError("invalid utgen_version or utgen_updated_at") from exc
    return XmlMetadata(display)


def coerce_metadata(
    existing: XmlMetadata | DisplayVersion | None,
    *,
    display_version: XmlMetadata | DisplayVersion | str | None = None,
    version: XmlMetadata | DisplayVersion | str | None = None,
    updated_at: str | None = None,
) -> XmlMetadata:
    """Resolve render-time metadata while requiring a complete valid pair."""

    if display_version is not None and version is not None:
        # Both spellings are accepted for compatibility, but disagreement is
        # almost certainly a caller bug and must not produce a mixed bundle.
        left = _coerce_display(display_version, updated_at)
        right = _coerce_display(version, updated_at)
        if left != right:
            raise XmlMetadataError("display_version and version disagree")
        return XmlMetadata(left)
    candidate = display_version if display_version is not None else version
    if candidate is None:
        if isinstance(existing, XmlMetadata):
            return existing
        if isinstance(existing, DisplayVersion):
            return XmlMetadata(existing)
        raise XmlMetadataError(
            "new XML rendering requires a valid DisplayVersion and acceptance timestamp"
        )
    return XmlMetadata(_coerce_display(candidate, updated_at))


def _coerce_display(value: XmlMetadata | DisplayVersion | str, updated_at: str | None) -> DisplayVersion:
    if isinstance(value, XmlMetadata):
        value = value.display_version
    if isinstance(value, DisplayVersion):
        if updated_at is None:
            return value
        try:
            timestamp = canonicalize_timestamp(updated_at)
        except VersioningError as exc:
            raise XmlMetadataError("updated_at is not a canonical timestamp") from exc
        return DisplayVersion(value.major, value.minor, timestamp)
    if type(value) is str:
        if updated_at is None:
            raise XmlMetadataError(
                "a string display version requires an explicit updated_at timestamp"
            )
        try:
            return parse_display_version(value, updated_at)
        except VersioningError as exc:
            raise XmlMetadataError("invalid display version metadata") from exc
    raise TypeError("display_version/version must be DisplayVersion or major.minor text")


def set_root_metadata(root: ET.Element, metadata: XmlMetadata) -> None:
    """Set the exact metadata names without disturbing unrelated attributes."""

    root.set("utgen_version", metadata.version)
    root.set("utgen_updated_at", metadata.updated_at)


def clone_element(element: ET.Element | None) -> ET.Element | None:
    return deepcopy(element) if element is not None else None


def parse_dimensions(name: str) -> tuple[int, ...]:
    return tuple(int(item) for item in _DIMENSION.findall(name))


def expand_array_name(name: str) -> list[str]:
    """Expand every ``[N]`` extent in declaration order.

    The product order intentionally matches the existing TESSY compatibility
    path: the first dimension is the outer loop and the last dimension varies
    fastest.
    """

    dimensions = parse_dimensions(name)
    if not dimensions:
        return [name]
    # ``re.split`` returns text and captured extents; rebuild from the literal
    # portions so names containing dots and local-static suffixes remain raw.
    literal_parts = re.split(r"\[[0-9]+\]", name)
    result: list[str] = []
    for indices in product(*(range(size) for size in dimensions)):
        pieces: list[str] = []
        for index, literal in enumerate(literal_parts):
            pieces.append(literal)
            if index < len(indices):
                pieces.append(f"[{indices[index]}]")
        result.append("".join(pieces))
    return result


def array_element_paths(dimensions: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    if not dimensions:
        return ((),)
    return tuple(product(*(range(size) for size in dimensions)))


def parse_pointer(value: str | bool | None) -> bool | None:
    if value is None or type(value) is bool:
        return value
    if type(value) is not str:
        raise XmlParseError("pointer metadata must be true, false, or absent")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise XmlParseError(f"unsupported pointer metadata {value!r}")


def split_stream(raw: str | None) -> tuple[tuple[str, ...], ...] | None:
    """Split XML's element-stream ``|`` and testcase `` , `` separators.

    Tokens are intentionally *not* stripped or converted.  Numeric lexemes,
    enum spellings, and even an explicitly empty token are wire data that must
    survive a read/render cycle.  Consumers that need semantic comparison can
    use ``semantic_token`` below.
    """

    if raw is None:
        return None
    return tuple(tuple(group.split(",")) for group in raw.split("|"))


def join_stream(groups: tuple[tuple[str, ...], ...] | None) -> str | None:
    if groups is None:
        return None
    return "|".join(",".join(tokens) for tokens in groups)


def stream_encoding(raw: str | None, dimensions: tuple[int, ...], *, output: bool = False) -> str:
    if raw is None and output:
        return "absent"
    if not dimensions:
        return "scalar"
    groups = split_stream(raw)
    if groups is not None and len(groups) > 1:
        return "expanded"
    return "shared"


def semantic_token(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def is_null_token(value: str | None) -> bool:
    token = semantic_token(value)
    return token is not None and token.upper() == "NULL"


def is_semantic_zero(value: str | int | float | None) -> bool:
    """Recognize numeric zero for pointer NULL validation without coercing lexemes."""

    if value is None or type(value) is bool:
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if type(value) is not str:
        return False
    token = value.strip()
    if not token or token.upper() == "NULL":
        return False
    try:
        # int(..., 0) preserves the distinction between a zero lexeme and a
        # symbolic enum while accepting decimal and C-style hexadecimal forms.
        return int(token, 0) == 0
    except ValueError:
        try:
            return float(token) == 0.0
        except ValueError:
            return False


def validate_metadata_pair(left: Any, right: Any) -> XmlMetadata:
    """Require value and stub snapshots to expose one exact accepted version."""

    left_meta = getattr(left, "metadata", None)
    right_meta = getattr(right, "metadata", None)
    if left_meta is None or right_meta is None:
        raise VersionMetadataMismatchError(
            "value XML and stub XML must both carry valid version metadata"
        )
    if left_meta != right_meta:
        raise VersionMetadataMismatchError(
            "value XML and stub XML carry different display versions or timestamps"
        )
    return left_meta


def serialize_xml(root: ET.Element, *, declaration: str = "utf-8") -> bytes:
    """Serialize with an XML declaration while retaining the existing tree order."""

    try:
        ET.indent(root, space="    ")
    except AttributeError:  # pragma: no cover - Python < 3.9 compatibility
        pass
    payload = ET.tostring(root, encoding="utf-8")
    return f"<?xml version=\"1.0\" encoding=\"{declaration}\"?>\n".encode("ascii") + payload + b"\n"


def local_attributes(element: ET.Element) -> dict[str, str]:
    """Copy attributes so snapshots cannot mutate the parsed XML tree."""

    return dict(element.attrib)


__all__ = [
    "LegacyXmlMetadataError",
    "XmlAdapterError",
    "XmlMetadata",
    "XmlMetadataError",
    "XmlParseError",
    "VersionMetadataMismatchError",
    "clone_element",
    "coerce_metadata",
    "array_element_paths",
    "expand_array_name",
    "is_null_token",
    "is_semantic_zero",
    "join_stream",
    "local_attributes",
    "parse_dimensions",
    "parse_metadata",
    "parse_pointer",
    "parse_xml_root",
    "read_xml_bytes",
    "semantic_token",
    "serialize_xml",
    "set_root_metadata",
    "split_stream",
    "stream_encoding",
    "validate_metadata_pair",
]
