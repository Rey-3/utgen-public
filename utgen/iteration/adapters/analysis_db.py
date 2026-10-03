"""Read, validate, and metadata-render Analysis DB XML.

The adapter keeps source-analysis payload separate from mutable iteration state.
It uses the existing UTGen XML serializer for canonical content-addressing while
retaining unknown source-analysis attributes and elements.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePath
import re
import tempfile
import xml.etree.ElementTree as ET

DB_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ANALYSIS_ID = re.compile(r"analysis_v1_[0-9a-f]{64}\Z")
_SAFE_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_CANONICAL_TIMESTAMP = re.compile(
    r"^(?P<local>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})\Z"
)
_METADATA_ATTRIBUTES = frozenset({"db_schema_version", "analysis_id", "generated_at"})
_REQUIRED_ROOT_ATTRIBUTES = frozenset({"file", "sha256", "tool"})
# These names are normalized before comparison so state cannot be smuggled in
# through spelling/case variations such as ``Working_Snapshot``.  ``Base`` is
# intentionally absent: it is a valid expression-tree node in this XML schema.
_FORBIDDEN_STATE_TAGS = frozenset({
    "testcase", "testcases", "testrow", "testrows", "testcaserow",
    "row", "rows", "rowmeta", "rowmetadata", "generatedtestcase",
    "generatedtestcases", "expectedoutput", "expectedoutputs",
    "expectedoutputrecord", "expectedoutputrecords", "expectedrecord",
    "expectedrecords", "working", "workingsnapshot", "workingsnapshots",
    "workingstate", "workingstates", "base", "basesnapshot",
    "basesnapshots", "basestate", "basestates", "stubbody", "stubbodies",
    "description", "descriptions", "tombstone", "tombstones", "orphan", "orphans",
    "conflict", "conflicts",
    "displayversion", "displayversions", "versionmirror", "versionmirrors",
    "versionmetadata", "acceptedversion", "utgenversion", "utgenupdatedat",
})
_FORBIDDEN_STATE_ATTRIBUTES = frozenset({
    "testcase", "testcaseid", "testrow", "rowid", "casename", "rowmeta",
    "expectedoutput", "expectedoutputrecord", "working", "workingsnapshot",
    "workingstate", "base", "basesnapshot", "basestate", "stubbody",
    "bodyraw", "description", "tombstone", "orphan", "conflict", "displayversion",
    "versionmirror", "versionmetadata", "utgenversion", "utgenupdatedat",
})


def _canonicalize_timestamp(value: object) -> str:
    """Validate and normalize the second-precision timestamp used by the DB."""

    if type(value) is not str:
        raise ValueError("timestamp must be a string")
    match = _CANONICAL_TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError(
            "timestamp must use YYYY-MM-DDTHH:MM:SS±HH:MM or UTC Z form "
            "without fractional seconds"
        )
    local, zone = match.group("local"), match.group("zone")
    try:
        datetime.strptime(local, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"timestamp is not valid: {value!r}") from exc
    if zone == "Z":
        return f"{local}Z"
    hours, minutes = int(zone[1:3]), int(zone[4:6])
    if hours > 23 or minutes > 59:
        raise ValueError(f"timestamp has an invalid timezone offset: {value!r}")
    return f"{local}{'Z' if hours == 0 and minutes == 0 else zone}"


class AnalysisDbError(ValueError):
    """Base error for invalid or unusable Analysis DB XML."""


class AnalysisDbValidationError(AnalysisDbError):
    """Raised when DB XML violates the source-analysis boundary."""


@dataclass(frozen=True)
class AnalysisDbMetadata:
    """Validated root metadata; absent fields represent an upgradeable legacy DB."""

    source_file: str
    source_sha256: str
    tool: str
    db_schema_version: int | None
    analysis_id: str | None
    generated_at: str | None

    @property
    def has_complete_metadata(self) -> bool:
        return (
            self.db_schema_version is not None
            and self.analysis_id is not None
            and self.generated_at is not None
        )


def _read_input(value: Path | bytes | bytearray) -> bytes:
    if isinstance(value, Path):
        try:
            return value.read_bytes()
        except OSError as exc:
            raise AnalysisDbValidationError(f"cannot read Analysis DB: {value}") from exc
    if type(value) is bytes:
        return value
    if type(value) is bytearray:
        return bytes(value)
    raise TypeError("Analysis DB input must be a pathlib.Path or bytes")


def _parse_root(data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError) as exc:
        raise AnalysisDbValidationError("Analysis DB XML is not well formed") from exc
    if root.tag != "SourceAnalysis":
        raise AnalysisDbValidationError(
            f"Analysis DB root must be SourceAnalysis, got {root.tag!r}"
        )
    return root


def _nonempty_attribute(root: ET.Element, name: str) -> str:
    value = root.get(name)
    if value is None or not value:
        raise AnalysisDbValidationError(f"SourceAnalysis is missing non-empty attribute {name!r}")
    return value


def _validate_iteration_boundary(root: ET.Element) -> None:
    for element in root.iter():
        tag = element.tag
        if not isinstance(tag, str):
            continue
        local_tag = tag.rsplit("}", 1)[-1]
        local_name = _normalized_name(tag)
        # ``Base`` is the existing expression-tree node.  A bare all-caps
        # ``BASE`` is instead treated as an iteration snapshot marker.
        is_expression_base = local_name == "base" and local_tag == "Base"
        if local_name in _FORBIDDEN_STATE_TAGS and not is_expression_base:
            raise AnalysisDbValidationError(
                f"iteration-state element {element.tag!r} is not allowed in Analysis DB"
            )
        for attribute in element.attrib:
            attribute_name = _normalized_name(attribute)
            if attribute_name in _FORBIDDEN_STATE_ATTRIBUTES:
                raise AnalysisDbValidationError(
                    f"iteration-state attribute {attribute!r} is not allowed in Analysis DB"
                )
        if local_name == "stub":
            if element.text and element.text.strip():
                raise AnalysisDbValidationError(
                    "arbitrary Stub Body text is not allowed in Analysis DB"
                )
            if any(_normalized_name(attribute) == "body" for attribute in element.attrib):
                raise AnalysisDbValidationError(
                    "arbitrary Stub Body attributes are not allowed in Analysis DB"
                )
            if any(_normalized_name(child.tag) == "body" for child in element if isinstance(child.tag, str)):
                raise AnalysisDbValidationError(
                    "arbitrary Stub Body elements are not allowed in Analysis DB"
                )


def _normalized_name(value: str) -> str:
    local_name = value.rsplit("}", 1)[-1]
    return "".join(character.lower() for character in local_name if character.isalnum())


def _metadata(root: ET.Element, *, require_metadata: bool) -> AnalysisDbMetadata:
    for name in _REQUIRED_ROOT_ATTRIBUTES:
        _nonempty_attribute(root, name)
    source_sha256 = root.get("sha256", "")
    if _SHA256.fullmatch(source_sha256) is None:
        raise AnalysisDbValidationError("SourceAnalysis.sha256 must be 64 lowercase hexadecimal digits")

    present = _METADATA_ATTRIBUTES & root.attrib.keys()
    if present and present != _METADATA_ATTRIBUTES:
        missing = sorted(_METADATA_ATTRIBUTES - present)
        raise AnalysisDbValidationError(
            "Analysis DB metadata must contain db_schema_version, analysis_id, and generated_at "
            f"together; missing {', '.join(missing)}"
        )
    if not present:
        if require_metadata:
            raise AnalysisDbValidationError("stored Analysis DB is missing required metadata")
        metadata = AnalysisDbMetadata(
            root.get("file", ""), source_sha256, root.get("tool", ""), None, None, None
        )
        _validate_iteration_boundary(root)
        return metadata

    if root.get("db_schema_version") != str(DB_SCHEMA_VERSION):
        raise AnalysisDbValidationError(
            f"only db_schema_version={DB_SCHEMA_VERSION} is supported"
        )
    analysis_id = root.get("analysis_id", "")
    if _ANALYSIS_ID.fullmatch(analysis_id) is None:
        raise AnalysisDbValidationError(
            "analysis_id must match analysis_v1_ followed by 64 lowercase hexadecimal digits"
        )
    try:
        generated_at = _canonicalize_timestamp(root.get("generated_at", ""))
    except ValueError as exc:
        raise AnalysisDbValidationError("generated_at is not a canonical timestamp") from exc
    metadata = AnalysisDbMetadata(
        root.get("file", ""), source_sha256, root.get("tool", ""),
        DB_SCHEMA_VERSION, analysis_id, generated_at,
    )
    _validate_iteration_boundary(root)
    return metadata


def _serialize_root(root: ET.Element) -> bytes:
    # The existing serializer is the single UTGen XML wire serializer.  Import
    # lazily so importing this domain adapter does not eagerly initialize clang.
    from utgen.db import _indent_xml, _serialize_xml

    _indent_xml(root, space="    ")
    payload = _serialize_xml(root)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + payload + b"\n"


def canonical_payload_bytes(value: Path | bytes | bytearray) -> bytes:
    """Serialize metadata-free source-analysis payload with the UTGen writer."""

    root = _parse_root(_read_input(value))
    _metadata(root, require_metadata=False)
    return _canonical_payload(root)


def _canonical_payload(root: ET.Element) -> bytes:
    root = deepcopy(root)
    for name in _METADATA_ATTRIBUTES:
        root.attrib.pop(name, None)
    return _serialize_root(root)


def analysis_content_hash(value: Path | bytes | bytearray) -> str:
    """Hash canonical XML after removing only generated DB metadata."""

    return hashlib.sha256(canonical_payload_bytes(value)).hexdigest()


def _validate_analysis_id(analysis_id: object) -> str:
    if type(analysis_id) is not str or _ANALYSIS_ID.fullmatch(analysis_id) is None:
        raise AnalysisDbValidationError(
            "analysis_id must match analysis_v1_ followed by 64 lowercase hexadecimal digits"
        )
    return analysis_id


def enrich_with_metadata(
    value: Path | bytes | bytearray,
    analysis_id: str,
    generated_at: str,
) -> bytes:
    """Return canonical XML with required metadata, preserving all payload data."""

    root = _parse_root(_read_input(value))
    _metadata(root, require_metadata=False)
    _validate_analysis_id(analysis_id)
    content_hash = hashlib.sha256(_canonical_payload(root)).hexdigest()
    if analysis_id != f"analysis_v1_{content_hash}":
        raise AnalysisDbValidationError(
            "analysis_id must be derived from the metadata-free source-analysis payload"
        )
    return _enrich_root(root, content_hash, generated_at)


def _enrich_root(root: ET.Element, content_hash: str, generated_at: str) -> bytes:
    try:
        generated_at = _canonicalize_timestamp(generated_at)
    except ValueError as exc:
        raise AnalysisDbValidationError("generated_at is not a canonical timestamp") from exc
    root.set("db_schema_version", str(DB_SCHEMA_VERSION))
    root.set("analysis_id", f"analysis_v1_{content_hash}")
    root.set("generated_at", generated_at)
    result = _serialize_root(root)
    rendered_root = _parse_root(result)
    _metadata(rendered_root, require_metadata=True)
    if hashlib.sha256(_canonical_payload(rendered_root)).hexdigest() != content_hash:
        raise AnalysisDbValidationError("metadata enrichment changed source-analysis payload")
    return result


class AnalysisDbAdapter:
    """Facade for strict DB metadata, content hashing, and legacy upgrades."""

    def inspect(
        self,
        value: Path | bytes | bytearray,
        *,
        require_metadata: bool = False,
    ) -> AnalysisDbMetadata:
        root = _parse_root(_read_input(value))
        return _metadata(root, require_metadata=require_metadata)

    def validate(
        self,
        value: Path | bytes | bytearray,
        *,
        require_metadata: bool = False,
    ) -> AnalysisDbMetadata:
        """Validate a DB and return its normalized root metadata."""

        return self.inspect(value, require_metadata=require_metadata)

    def canonical_payload(self, value: Path | bytes | bytearray) -> bytes:
        return canonical_payload_bytes(value)

    def content_hash(self, value: Path | bytes | bytearray) -> str:
        return analysis_content_hash(value)

    def enrich(self, value: Path | bytes | bytearray, analysis_id: str, generated_at: str) -> bytes:
        return enrich_with_metadata(value, analysis_id, generated_at)

    def source_stem(self, value: Path | bytes | bytearray) -> str:
        metadata = self.inspect(value, require_metadata=False)
        source_file = metadata.source_file.replace("\\", "/").rsplit("/", 1)[-1]
        stem = PurePath(source_file).stem
        if _SAFE_STEM.fullmatch(stem) is None:
            raise AnalysisDbValidationError(
                "source filename stem is not safe for content-addressed storage"
            )
        return stem

    def read_module(self, path: Path, *, require_metadata: bool = True):
        """Validate a DB and return metadata plus the legacy parsed model."""

        path = Path(path)
        metadata = self.inspect(path, require_metadata=require_metadata)
        try:
            from utgen.db import XmlDbReader

            analysis = XmlDbReader().read(path)
        except Exception as exc:
            raise AnalysisDbValidationError(f"cannot parse stored Analysis DB: {path}") from exc
        return metadata, analysis

class AnalysisDbWriter:
    """Render or publish a metadata-enriched DB without changing legacy writers."""

    def render(
        self,
        value: Path | bytes | bytearray,
        *,
        generated_at: str,
    ) -> bytes:
        """Return canonical DB bytes with content-derived ID and schema metadata."""

        root = _parse_root(_read_input(value))
        _metadata(root, require_metadata=False)
        content_hash = hashlib.sha256(_canonical_payload(root)).hexdigest()
        return _enrich_root(root, content_hash, generated_at)

    def write(
        self,
        value: Path | bytes | bytearray,
        path: Path,
        *,
        generated_at: str,
    ) -> AnalysisDbMetadata:
        """Write a validated DB file and return its normalized metadata.

        Publication uses exclusive creation so a writer cannot overwrite an
        existing fixture or accepted DB.  The input path is checked explicitly
        as well, producing a clear error before any filesystem mutation.
        """

        path = Path(path)
        if isinstance(value, Path):
            try:
                same_file = path.resolve() == value.resolve()
            except OSError as exc:
                raise AnalysisDbValidationError("cannot resolve Analysis DB paths") from exc
            if same_file:
                raise AnalysisDbValidationError("refusing to overwrite the Analysis DB input")
        rendered = self.render(value, generated_at=generated_at)
        metadata = _metadata(_parse_root(rendered), require_metadata=True)
        if metadata.analysis_id is None or metadata.generated_at is None:
            raise AnalysisDbValidationError("rendered Analysis DB is missing complete metadata")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AnalysisDbValidationError(
                f"cannot create Analysis DB output directory: {path.parent}"
            ) from exc
        try:
            with path.open("xb") as stream:
                stream.write(rendered)
        except FileExistsError as exc:
            raise AnalysisDbValidationError(f"refusing to overwrite existing Analysis DB: {path}") from exc
        except OSError as exc:
            raise AnalysisDbValidationError(f"cannot publish Analysis DB: {path}") from exc
        return metadata

    def write_atomic(
        self,
        value: Path | bytes | bytearray,
        path: Path,
        *,
        generated_at: str | None = None,
    ) -> AnalysisDbMetadata:
        """Validate and atomically replace the current DB publication.

        ``write`` intentionally retains its exclusive-create behaviour for
        fixtures and callers that accept a once-only publication.  Iteration
        needs a different primitive: the current project DB is replaced only
        after the complete metadata-enriched document has been rendered and
        validated in a sibling temporary file.
        """

        path = Path(path)
        if isinstance(value, Path):
            try:
                if path.resolve() == value.resolve():
                    raise AnalysisDbValidationError(
                        "refusing to overwrite the Analysis DB input"
                    )
            except OSError as exc:
                raise AnalysisDbValidationError(
                    "cannot resolve Analysis DB paths"
                ) from exc
        if generated_at is None:
            generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            if generated_at.endswith("+00:00"):
                generated_at = generated_at[:-6] + "Z"
        rendered = self.render(value, generated_at=generated_at)
        metadata = _metadata(_parse_root(rendered), require_metadata=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AnalysisDbValidationError(
                f"cannot create Analysis DB output directory: {path.parent}"
            ) from exc

        try:
            previous = path.read_bytes() if path.exists() else None
        except OSError as exc:
            raise AnalysisDbValidationError(f"cannot read existing Analysis DB: {path}") from exc

        def restore_previous(cause: Exception) -> None:
            if previous is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    return
                except OSError as exc:
                    raise AnalysisDbValidationError(
                        f"cannot remove failed Analysis DB publication: {path}: {exc}"
                    ) from cause
                return

            restore_temp = None
            retain_restore = False
            try:
                fd, restore_name = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".restore", dir=path.parent
                )
                restore_temp = Path(restore_name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(previous)
                    stream.flush()
                    os.fsync(stream.fileno())
                if restore_temp.read_bytes() != previous:
                    retain_restore = True
                    raise RuntimeError(
                        f"restore bytes do not match previous DB; recovery path: {restore_temp}"
                    )
                try:
                    os.replace(restore_temp, path)
                except OSError as exc:
                    try:
                        restored = path.read_bytes() == previous
                    except OSError as read_error:
                        retain_restore = True
                        raise RuntimeError(
                            f"cannot verify restored Analysis DB: {path}: {read_error}; "
                            f"recovery path: {restore_temp}"
                        ) from read_error
                    if restored:
                        return
                    retain_restore = True
                    raise RuntimeError(
                        f"cannot restore previous Analysis DB after replace failure: {path}: {exc}; "
                        f"recovery path: {restore_temp}"
                    ) from exc
                try:
                    restored = path.read_bytes() == previous
                except OSError as exc:
                    raise AnalysisDbValidationError(
                        f"cannot verify restored Analysis DB: {path}: {exc}"
                    ) from cause
                if not restored:
                    raise AnalysisDbValidationError(
                        f"restored Analysis DB does not match previous bytes: {path}"
                    ) from cause
            except Exception as exc:
                raise AnalysisDbValidationError(
                    f"cannot restore previous Analysis DB: {path}: {exc}"
                ) from cause
            finally:
                if restore_temp is not None and not retain_restore:
                    try:
                        restore_temp.unlink()
                    except FileNotFoundError:
                        pass

        temporary = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(temporary_name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            if temporary.read_bytes() != rendered:
                raise AnalysisDbValidationError(
                    f"temporary Analysis DB does not match rendered bytes: {temporary}"
                )
            AnalysisDbAdapter().read_module(temporary, require_metadata=True)
            try:
                os.replace(temporary, path)
            except OSError as exc:
                restore_previous(exc)
                raise AnalysisDbValidationError(f"cannot publish Analysis DB: {path}") from exc
            temporary = None
            try:
                # Atomic replacement publishes the already-validated model. Only
                # byte integrity needs checking here; rebuilding dataflow is redundant.
                if path.read_bytes() != rendered:
                    raise AnalysisDbValidationError(
                        f"published Analysis DB does not match rendered bytes: {path}"
                    )
            except Exception as exc:
                restore_previous(exc)
                raise
        except AnalysisDbValidationError:
            raise
        except OSError as exc:
            raise AnalysisDbValidationError(f"cannot publish Analysis DB: {path}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        return metadata


__all__ = [
    "DB_SCHEMA_VERSION",
    "AnalysisDbAdapter",
    "AnalysisDbError",
    "AnalysisDbMetadata",
    "AnalysisDbValidationError",
    "AnalysisDbWriter",
    "analysis_content_hash",
    "canonical_payload_bytes",
    "enrich_with_metadata",
]
