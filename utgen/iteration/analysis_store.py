"""Immutable content-addressed storage for Analysis DB snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
from typing import Callable, Protocol
import uuid

from utgen.model import ModuleAnalysis

from .adapters.analysis_db import (
    DB_SCHEMA_VERSION,
    AnalysisDbAdapter,
    AnalysisDbError,
    AnalysisDbWriter,
)
from .model import AnalysisRef
from .versioning import VersioningError, canonicalize_timestamp


_ANALYSIS_ID = re.compile(r"analysis_v1_[0-9a-f]{64}\Z")
Clock = Callable[[], str]


class AnalysisStoreError(ValueError):
    """Base error for content-addressed analysis storage failures."""


class AnalysisStoreIntegrityError(AnalysisStoreError):
    """Raised when a stored ID, payload, metadata, or file hash disagrees."""


@dataclass(frozen=True)
class AnalysisSnapshot:
    """One validated stored DB, including bytes and the parsed legacy model."""

    ref: AnalysisRef
    path: Path
    xml_bytes: bytes
    analysis: ModuleAnalysis

    @property
    def module_analysis(self) -> ModuleAnalysis:
        """Compatibility spelling for callers that prefer the domain type name."""

        return self.analysis


class AnalysisStore(Protocol):
    def put(self, db_xml: Path) -> AnalysisRef: ...

    def resolve(self, analysis_id: str) -> AnalysisSnapshot: ...

    def validate(self, analysis_id: str) -> None: ...


def _utc_clock() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_analysis_id(value: object) -> str:
    if type(value) is not str or _ANALYSIS_ID.fullmatch(value) is None:
        raise AnalysisStoreIntegrityError(
            "analysis_id must match analysis_v1_ followed by 64 lowercase hexadecimal digits"
        )
    return value


def _analysis_id(content_hash: str) -> str:
    return f"analysis_v1_{content_hash}"


def _file_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ContentAddressedAnalysisStore:
    """Store immutable DB XML under the project-local ``.utgen/analysis/<id>``."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock | None = None,
        adapter: AnalysisDbAdapter | None = None,
    ) -> None:
        self.root = Path(root)
        # Callers may pass either a project directory or its explicit ``.utgen``
        # directory.  Both forms resolve to the documented project-local path.
        self.utgen_root = self.root if self.root.name == ".utgen" else self.root / ".utgen"
        self.analysis_root = self.utgen_root / "analysis"
        self.clock = clock
        self.adapter = adapter or AnalysisDbAdapter()
        self.writer = AnalysisDbWriter()

    def _capture_timestamp(self) -> str:
        clock = self.clock or _utc_clock
        if not callable(clock):
            raise TypeError("clock must be callable")
        try:
            return canonicalize_timestamp(clock())
        except VersioningError as exc:
            raise AnalysisStoreError("analysis DB clock returned an invalid timestamp") from exc

    def _target(self, analysis_id: str, source_stem: str) -> Path:
        return self.analysis_root / analysis_id / f"db_{source_stem}.xml"

    def _validate_storage_root(self) -> None:
        for directory in (self.utgen_root, self.analysis_root):
            if not directory.exists() and not directory.is_symlink():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise AnalysisStoreIntegrityError(
                    f"analysis storage root is not a regular directory: {directory}"
                )

    def _validate_existing_directory(self, target: Path) -> None:
        directory = target.parent
        if not directory.exists():
            return
        if not directory.is_dir() or directory.is_symlink():
            raise AnalysisStoreIntegrityError(f"analysis storage path is not a directory: {directory}")
        entries = list(directory.iterdir())
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise AnalysisStoreIntegrityError(f"analysis storage target is not a regular file: {target}")
            extras = [entry for entry in entries if entry != target]
            if extras:
                raise AnalysisStoreIntegrityError(
                    f"analysis ID directory contains unexpected entries: {directory}"
                )
        elif entries:
            raise AnalysisStoreIntegrityError(
                f"analysis ID directory contains a colliding or damaged file: {directory}"
            )

    def _ref_from_stored(
        self,
        analysis_id: str,
        path: Path,
        expected_content_hash: str | None = None,
    ) -> AnalysisRef:
        if not path.exists() or not path.is_file() or path.is_symlink():
            raise AnalysisStoreIntegrityError(f"stored Analysis DB is missing or unsafe: {path}")
        try:
            xml_bytes = path.read_bytes()
        except OSError as exc:
            raise AnalysisStoreIntegrityError(f"stored Analysis DB cannot be read: {path}") from exc
        try:
            metadata = self.adapter.inspect(xml_bytes, require_metadata=True)
            content_hash = self.adapter.content_hash(xml_bytes)
        except AnalysisDbError as exc:
            raise AnalysisStoreIntegrityError(f"stored Analysis DB failed validation: {path}") from exc
        if metadata.analysis_id != analysis_id:
            raise AnalysisStoreIntegrityError(
                f"stored analysis ID does not match path: {metadata.analysis_id!r}"
            )
        if expected_content_hash is not None and content_hash != expected_content_hash:
            raise AnalysisStoreIntegrityError("stored analysis content hash does not match requested content")
        if _analysis_id(content_hash) != analysis_id:
            raise AnalysisStoreIntegrityError("stored analysis ID is not derived from its content hash")
        if self.adapter.source_stem(xml_bytes) != path.stem.removeprefix("db_"):
            raise AnalysisStoreIntegrityError("stored Analysis DB filename does not match source stem")
        try:
            canonical_stored = self.writer.render(
                xml_bytes,
                generated_at=metadata.generated_at or "",
            )
        except AnalysisDbError as exc:
            raise AnalysisStoreIntegrityError(
                f"stored Analysis DB could not be canonicalized: {path}"
            ) from exc
        if canonical_stored != xml_bytes:
            raise AnalysisStoreIntegrityError(
                f"stored Analysis DB bytes are not the immutable canonical representation: {path}"
            )
        return AnalysisRef(
            analysis_id,
            DB_SCHEMA_VERSION,
            content_hash,
            _file_hash(xml_bytes),
            metadata.source_sha256,
        )

    def put(self, db_xml: Path) -> AnalysisRef:
        """Upgrade and store a legacy/enriched DB without overwriting existing data."""

        db_xml = Path(db_xml)
        if not db_xml.is_file() or db_xml.is_symlink():
            raise AnalysisStoreError(f"Analysis DB input is not a regular file: {db_xml}")
        try:
            raw = db_xml.read_bytes()
            metadata, _ = self.adapter.read_module(db_xml, require_metadata=False)
            content_hash = self.adapter.content_hash(raw)
            source_stem = self.adapter.source_stem(raw)
        except (AnalysisDbError, OSError) as exc:
            raise AnalysisStoreError(f"Analysis DB input failed validation: {db_xml}") from exc
        expected_id = _analysis_id(content_hash)
        _validate_analysis_id(expected_id)
        self._validate_storage_root()
        target = self._target(expected_id, source_stem)
        self._validate_existing_directory(target)

        # Reuse the first immutable copy before reading the clock.  A repeated
        # put of a legacy source must preserve the original generated_at.
        if target.exists():
            existing_ref = self._ref_from_stored(
                expected_id,
                target,
                expected_content_hash=content_hash,
            )
            try:
                existing_bytes = target.read_bytes()
                existing_metadata = self.adapter.inspect(existing_bytes, require_metadata=True)
                expected_existing = self.writer.render(
                    raw,
                    generated_at=existing_metadata.generated_at or "",
                )
            except (AnalysisDbError, OSError) as exc:
                raise AnalysisStoreIntegrityError(
                    f"stored Analysis DB could not be compared with the requested payload: {target}"
                ) from exc
            if expected_existing != existing_bytes:
                raise AnalysisStoreIntegrityError(
                    "stored Analysis DB differs from the immutable payload or generated_at"
                )
            return existing_ref

        if metadata.has_complete_metadata:
            if metadata.analysis_id != expected_id:
                raise AnalysisStoreIntegrityError(
                    "input Analysis DB metadata analysis_id disagrees with its content hash"
                )
            generated_at = metadata.generated_at
        else:
            generated_at = self._capture_timestamp()
        if generated_at is None:
            raise AnalysisStoreIntegrityError("input Analysis DB has no usable generated_at")
        try:
            enriched = self.writer.render(raw, generated_at=generated_at)
            final_metadata = self.adapter.inspect(enriched, require_metadata=True)
            final_content_hash = self.adapter.content_hash(enriched)
        except AnalysisDbError as exc:
            raise AnalysisStoreError("could not produce a validated enriched Analysis DB") from exc
        if final_metadata.analysis_id != expected_id or final_content_hash != content_hash:
            raise AnalysisStoreIntegrityError("metadata enrichment changed Analysis DB identity")

        try:
            target.parent.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise AnalysisStoreIntegrityError(
                f"analysis ID directory appeared during immutable publication: {target.parent}"
            ) from exc
        except OSError as exc:
            raise AnalysisStoreIntegrityError(
                f"analysis storage path is not publishable: {target.parent}"
            ) from exc
        try:
            with target.open("xb") as stream:
                stream.write(enriched)
        except FileExistsError as exc:
            raise AnalysisStoreIntegrityError(f"analysis target appeared during publication: {target}") from exc
        except OSError as exc:
            raise AnalysisStoreError(f"could not publish Analysis DB: {target}") from exc
        return self._ref_from_stored(
            expected_id,
            target,
            expected_content_hash=content_hash,
        )

    def _stored_path(self, analysis_id: str) -> Path:
        analysis_id = _validate_analysis_id(analysis_id)
        self._validate_storage_root()
        directory = self.analysis_root / analysis_id
        if not directory.exists() or not directory.is_dir() or directory.is_symlink():
            raise AnalysisStoreError(f"analysis ID is not stored: {analysis_id}")
        entries = list(directory.iterdir())
        if len(entries) != 1 or not entries[0].is_file() or entries[0].is_symlink():
            raise AnalysisStoreIntegrityError(f"analysis ID directory is incomplete or ambiguous: {directory}")
        path = entries[0]
        if not path.name.startswith("db_") or path.suffix != ".xml":
            raise AnalysisStoreIntegrityError(f"analysis ID directory contains an invalid DB filename: {path}")
        return path

    def resolve(self, analysis_id: str) -> AnalysisSnapshot:
        """Load and validate one immutable DB snapshot."""

        analysis_id = _validate_analysis_id(analysis_id)
        path = self._stored_path(analysis_id)
        try:
            xml_bytes = path.read_bytes()
        except OSError as exc:
            raise AnalysisStoreIntegrityError(f"stored Analysis DB cannot be read: {path}") from exc
        ref = self._ref_from_stored(analysis_id, path)
        try:
            metadata, analysis = self.adapter.read_module(path, require_metadata=True)
        except AnalysisDbError as exc:
            raise AnalysisStoreIntegrityError(f"stored Analysis DB failed parse validation: {path}") from exc
        if metadata.analysis_id != analysis_id:
            raise AnalysisStoreIntegrityError("stored Analysis DB metadata changed during read")
        return AnalysisSnapshot(ref, path, xml_bytes, analysis)

    def validate(self, analysis_id: str) -> None:
        self.resolve(analysis_id)


class LatestAnalysisStore(ContentAddressedAnalysisStore):
    """Keep one current DB per source stem, with staged atomic publication."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock | None = None,
        adapter: AnalysisDbAdapter | None = None,
    ) -> None:
        super().__init__(root, clock=clock, adapter=adapter)
        self.staging_root = self.analysis_root / ".staging"

    def _target(self, source_stem: str) -> Path:
        if not source_stem or Path(source_stem).name != source_stem or source_stem in {".", ".."}:
            raise AnalysisStoreIntegrityError("analysis source stem is unsafe")
        return self.analysis_root / f"db_{source_stem}.xml"

    def _staged_target(self, source_stem: str) -> Path:
        return self.staging_root / f"db_{source_stem}.xml"

    def _ensure_staging_root(self) -> None:
        self._validate_storage_root()
        if self.staging_root.exists() or self.staging_root.is_symlink():
            if self.staging_root.is_symlink() or not self.staging_root.is_dir():
                raise AnalysisStoreIntegrityError(
                    f"analysis staging root is not a regular directory: {self.staging_root}"
                )
            return
        try:
            self.staging_root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if self.staging_root.is_symlink() or not self.staging_root.is_dir():
                raise AnalysisStoreIntegrityError(
                    f"analysis staging root is not a regular directory: {self.staging_root}"
                )
        except OSError as exc:
            raise AnalysisStoreError(
                f"analysis staging path is not publishable: {self.staging_root}"
            ) from exc

    def _stage_bytes(self, expected_id: str, source_stem: str, data: bytes) -> AnalysisRef:
        self._ensure_staging_root()
        target = self._staged_target(source_stem)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise AnalysisStoreIntegrityError(f"analysis staging target is unsafe: {target}")
        temporary = self.staging_root / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise AnalysisStoreError(f"could not stage Analysis DB: {target}") from exc
        return self._ref_from_stored(expected_id, target)

    def put(self, db_xml: Path) -> AnalysisRef:
        """Validate and stage a DB without replacing the accepted current file."""

        db_xml = Path(db_xml)
        if not db_xml.is_file() or db_xml.is_symlink():
            raise AnalysisStoreError(f"Analysis DB input is not a regular file: {db_xml}")
        try:
            raw = db_xml.read_bytes()
            metadata, _ = self.adapter.read_module(db_xml, require_metadata=False)
            content_hash = self.adapter.content_hash(raw)
            source_stem = self.adapter.source_stem(raw)
        except (AnalysisDbError, OSError) as exc:
            raise AnalysisStoreError(f"Analysis DB input failed validation: {db_xml}") from exc
        expected_id = _analysis_id(content_hash)
        _validate_analysis_id(expected_id)
        self._validate_storage_root()

        for existing in (self._target(source_stem), self._staged_target(source_stem)):
            if not existing.exists():
                continue
            if existing.is_symlink() or not existing.is_file():
                raise AnalysisStoreIntegrityError(f"analysis storage target is not a regular file: {existing}")
            try:
                existing_metadata = self.adapter.inspect(existing.read_bytes(), require_metadata=True)
            except (AnalysisDbError, OSError) as exc:
                raise AnalysisStoreIntegrityError(
                    f"stored Analysis DB could not be inspected: {existing}"
                ) from exc
            if existing_metadata.analysis_id != expected_id:
                continue
            existing_ref = self._ref_from_stored(expected_id, existing, expected_content_hash=content_hash)
            try:
                expected_existing = self.writer.render(
                    raw,
                    generated_at=existing_metadata.generated_at or "",
                )
            except AnalysisDbError as exc:
                raise AnalysisStoreIntegrityError(
                    f"stored Analysis DB could not be compared with the requested payload: {existing}"
                ) from exc
            if expected_existing != existing.read_bytes():
                raise AnalysisStoreIntegrityError(
                    "stored Analysis DB differs from the requested payload or generated_at"
                )
            return existing_ref

        if metadata.has_complete_metadata:
            if metadata.analysis_id != expected_id:
                raise AnalysisStoreIntegrityError(
                    "input Analysis DB metadata analysis_id disagrees with its content hash"
                )
            generated_at = metadata.generated_at
        else:
            generated_at = self._capture_timestamp()
        if generated_at is None:
            raise AnalysisStoreIntegrityError("input Analysis DB has no usable generated_at")
        try:
            enriched = self.writer.render(raw, generated_at=generated_at)
            final_metadata = self.adapter.inspect(enriched, require_metadata=True)
            final_content_hash = self.adapter.content_hash(enriched)
        except AnalysisDbError as exc:
            raise AnalysisStoreError("could not produce a validated enriched Analysis DB") from exc
        if final_metadata.analysis_id != expected_id or final_content_hash != content_hash:
            raise AnalysisStoreIntegrityError("metadata enrichment changed Analysis DB identity")
        return self._stage_bytes(expected_id, source_stem, enriched)

    def _candidate_paths(self) -> tuple[Path, ...]:
        self._validate_storage_root()
        paths: list[Path] = []
        for directory in (self.analysis_root, self.staging_root):
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise AnalysisStoreIntegrityError(f"analysis storage path is unsafe: {directory}")
            for path in sorted(directory.glob("db_*.xml")):
                if path.is_symlink() or not path.is_file():
                    raise AnalysisStoreIntegrityError(f"analysis DB candidate is unsafe: {path}")
                paths.append(path)
        return tuple(paths)

    def resolve(self, analysis_id: str) -> AnalysisSnapshot:
        """Resolve a current or staged DB by its content-derived ID."""

        analysis_id = _validate_analysis_id(analysis_id)
        for path in self._candidate_paths():
            try:
                metadata = self.adapter.inspect(path.read_bytes(), require_metadata=True)
            except (AnalysisDbError, OSError) as exc:
                raise AnalysisStoreIntegrityError(f"stored Analysis DB failed validation: {path}") from exc
            if metadata.analysis_id != analysis_id:
                continue
            ref = self._ref_from_stored(analysis_id, path)
            try:
                xml_bytes = path.read_bytes()
                metadata, analysis = self.adapter.read_module(path, require_metadata=True)
            except (AnalysisDbError, OSError) as exc:
                raise AnalysisStoreIntegrityError(f"stored Analysis DB failed parse validation: {path}") from exc
            if metadata.analysis_id != analysis_id:
                raise AnalysisStoreIntegrityError("stored Analysis DB metadata changed during read")
            return AnalysisSnapshot(ref, path, xml_bytes, analysis)
        raise AnalysisStoreError(f"analysis ID is not stored: {analysis_id}")

    def activate(self, analysis_id: str) -> AnalysisRef:
        """Atomically replace the current DB with the staged DB for ``analysis_id``."""

        snapshot = self.resolve(analysis_id)
        if snapshot.path.parent == self.staging_root:
            target = self._target(self.adapter.source_stem(snapshot.xml_bytes))
            self.analysis_root.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise AnalysisStoreIntegrityError(f"analysis storage target is unsafe: {target}")
            try:
                os.replace(snapshot.path, target)
            except OSError as exc:
                raise AnalysisStoreError(f"could not activate Analysis DB: {target}") from exc
            ref = self._ref_from_stored(analysis_id, target)
            try:
                self.staging_root.rmdir()
            except OSError:
                pass
            return ref
        return snapshot.ref

    def import_snapshot(self, snapshot: AnalysisSnapshot) -> AnalysisRef:
        """Stage and activate one already validated legacy snapshot."""

        source_stem = self.adapter.source_stem(snapshot.xml_bytes)
        ref = self._stage_bytes(snapshot.ref.analysis_id, source_stem, snapshot.xml_bytes)
        if ref != snapshot.ref:
            raise AnalysisStoreIntegrityError("imported Analysis DB reference differs from the snapshot")
        return self.activate(snapshot.ref.analysis_id)


FilesystemAnalysisStore = ContentAddressedAnalysisStore


__all__ = [
    "AnalysisSnapshot",
    "AnalysisStore",
    "AnalysisStoreError",
    "AnalysisStoreIntegrityError",
    "ContentAddressedAnalysisStore",
    "FilesystemAnalysisStore",
    "LatestAnalysisStore",
]
