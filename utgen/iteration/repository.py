"""Versioned state repository and recovery surface for PR05.

The repository owns only persistence and publication.  It does not discover
legacy projects, parse TESSY workbooks, or perform Merge A/Merge B decisions.
Callers must provide an already validated :class:`CandidateBundle`.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
from typing import Any, Callable, Iterable, Mapping, Protocol

from .model import (
    BundleManifest,
    CURRENT_SCHEMA_VERSION,
    FunctionIterationState,
    MergeConflict,
    ModuleManifest,
    RootManifest,
    StubState,
    UnknownFieldError,
    from_dict,
    is_canonical_json_bytes,
    loads,
    pretty_json_bytes,
)
from .analysis_store import ContentAddressedAnalysisStore, LatestAnalysisStore
from .transaction import (
    AdvisoryFileLock,
    ArtifactRecord,
    ArtifactRoundtripError,
    CandidateBundle,
    CandidateValidationError,
    candidate_identity_issues,
    ConcurrentStateUpdate,
    IterationTransaction,
    PublishedCommitDurabilityError,
    PublishedCommitLoadError,
    Transaction,
    TransactionError,
    UnsafeArtifactPath,
    ValidationIssue,
    ValidationReport,
    WriterLockError,
    new_persistent_id,
    parse_collection_bytes,
    validate_path_component,
    validate_persistent_id,
    validate_relative_path,
)


class RepositoryError(ValueError):
    """Base error for repository loading/recovery failures."""

    code = "STATE_REPOSITORY_ERROR"


class RepositoryNotInitialized(RepositoryError):
    code = "STATE_NOT_INITIALIZED"


class RepositoryIntegrityError(RepositoryError):
    code = "STATE_INTEGRITY_ERROR"


class RepositorySchemaError(RepositoryIntegrityError):
    code = "STATE_SCHEMA_UNSUPPORTED"


class IterationRepository(Protocol):
    """Stable repository contract used by later orchestration layers."""

    def load(self, module_key: str) -> AcceptedBundle: ...
    def begin(self, module_key: str, expected_revision: int) -> Transaction: ...


@dataclass(frozen=True)
class AcceptedBundle:
    """One coherent accepted module revision and its decoded state."""

    root_manifest: RootManifest
    module_manifest: ModuleManifest
    bundle_manifest: Any
    revision_path: Path
    function_states: tuple[FunctionIterationState, ...]
    module_stub_state: tuple[StubState, ...]
    conflicts: tuple[MergeConflict, ...]
    artifacts: tuple[ArtifactRecord, ...]

    @property
    def revision(self) -> int:
        return self.module_manifest.revision

    @property
    def revision_id(self) -> str:
        return self.module_manifest.revision_id

    @property
    def module_key(self) -> str:
        return self.module_manifest.module_key


@dataclass(frozen=True)
class RecoveryReport:
    """Read-only startup inspection result.

    Recovery never promotes or deletes any path.  Callers must explicitly pass
    paths from this report to :meth:`FileSystemIterationRepository.cleanup`.
    """

    incomplete_staging: tuple[Path, ...] = ()
    unreferenced_revisions: tuple[Path, ...] = ()
    invalid_revisions: tuple[Path, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.incomplete_staging or self.unreferenced_revisions or self.invalid_revisions)


class FileSystemIterationRepository:
    """A project-local, multi-module manifest and immutable revision store."""

    def __init__(
        self,
        root: Path,
        *,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        failure_injector: Callable[[str], None] | None = None,
        artifact_validators: Mapping[str, Callable[[bytes], Any]] | None = None,
        analysis_store: Any | None = None,
        current_only: bool = False,
    ) -> None:
        self.root = Path(root)
        self.utgen_root = self.root if self.root.name == ".utgen" else self.root / ".utgen"
        self.iteration_root = self.utgen_root / "iteration"
        self.manifest_path = self.iteration_root / "manifest.json"
        self.revision_root = self.iteration_root / "revisions"
        self.modules_root = self.iteration_root / "modules"
        self.staging_root = self.iteration_root / ".staging"
        self.locks_root = self.iteration_root / "locks"
        self.pointer_lock_path = self.iteration_root / ".pointer.lock"
        if type(current_only) is not bool:
            raise TypeError("current_only must be a boolean")
        self.current_only = current_only
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        self.uuid_factory = uuid_factory
        self.failure_injector = failure_injector
        self.artifact_validators = dict(artifact_validators or {})
        # Every production repository transaction must validate and resolve
        # its AnalysisRef.  ``None`` therefore means "use the project-local
        # content-addressed store", never "skip analysis validation".
        self.analysis_store = (
            analysis_store
            if analysis_store is not None
            else (LatestAnalysisStore(self.root) if self.current_only else ContentAddressedAnalysisStore(self.root))
        )

    def _inject(self, stage: str) -> None:
        if self.failure_injector is not None:
            self.failure_injector(stage)

    @staticmethod
    def _ensure_directory(path: Path, *, create: bool = False) -> None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                raise RepositoryIntegrityError(f"repository path is not a regular directory: {path}")
            return
        if not create:
            raise RepositoryNotInitialized(f"repository directory does not exist: {path}")
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if path.is_symlink() or not path.is_dir():
                raise RepositoryIntegrityError(f"repository path is not a regular directory: {path}")
        except OSError as exc:
            raise RepositoryError(f"cannot create repository directory: {path}") from exc

    def _ensure_iteration_layout(self) -> None:
        self._ensure_directory(self.utgen_root, create=True)
        self._ensure_directory(self.iteration_root, create=True)
        self._ensure_directory(self.locks_root, create=True)
        self._ensure_directory(self.revision_root, create=True)
        if self.current_only:
            self._ensure_directory(self.modules_root, create=True)
        self._ensure_directory(self.staging_root, create=True)

    def _read_root(self, *, missing_ok: bool = True) -> RootManifest:
        if self.manifest_path.is_symlink():
            raise RepositoryIntegrityError(f"root manifest is a symlink: {self.manifest_path}")
        if not self.manifest_path.exists():
            if missing_ok:
                return RootManifest(CURRENT_SCHEMA_VERSION, {})
            raise RepositoryNotInitialized(f"root manifest does not exist: {self.manifest_path}")
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise RepositoryIntegrityError(f"root manifest is not a regular file: {self.manifest_path}")
        try:
            raw = self.manifest_path.read_bytes()
            parsed = loads(raw, RootManifest)
        except (OSError, TypeError, ValueError, UnknownFieldError) as exc:
            raise RepositorySchemaError(f"root manifest failed strict v2 parsing: {self.manifest_path}") from exc
        if not is_canonical_json_bytes(parsed, raw):
            raise RepositoryIntegrityError("root manifest is not canonical UTF-8 JSON")
        return parsed

    @staticmethod
    def _safe_revision_path(root: Path, module_key: str, revision_id: str) -> Path:
        validate_path_component(module_key, "module_key")
        validate_persistent_id(revision_id, "rev")
        return root / module_key / revision_id

    @staticmethod
    def _relative_files(root: Path) -> set[str]:
        if root.is_symlink() or not root.is_dir():
            raise RepositoryIntegrityError(f"revision is not a regular directory: {root}")
        result: set[str] = set()
        for directory, directories, files in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            for name in directories:
                child = directory_path / name
                if child.is_symlink():
                    raise RepositoryIntegrityError(f"revision contains a symlink directory: {child}")
                relative_directory = child.relative_to(root).as_posix()
                try:
                    validate_relative_path(relative_directory)
                except Exception as exc:
                    raise RepositoryIntegrityError(
                        f"revision contains an unsafe directory path: {relative_directory}"
                    ) from exc
            for name in files:
                child = directory_path / name
                if child.is_symlink():
                    raise RepositoryIntegrityError(f"revision contains a symlink file: {child}")
                relative = child.relative_to(root).as_posix()
                try:
                    validate_relative_path(relative)
                except Exception as exc:
                    raise RepositoryIntegrityError(
                        f"revision contains an unsafe file path: {relative}"
                    ) from exc
                result.add(relative)
        return result

    @staticmethod
    def _state_paths(root: Path, slot: str) -> list[str]:
        prefix = f"{slot}/"
        return sorted(
            path
            for path in FileSystemIterationRepository._relative_files(root)
            if path.startswith(prefix) and path.endswith("/state.json")
        )

    def _load_revision(
        self,
        module_key: str,
        revision_id: str,
        root_manifest: RootManifest,
        *,
        revision_path: Path | None = None,
        analysis_store: Any | None = None,
    ) -> AcceptedBundle:
        revision_path = (
            self._safe_revision_path(self.revision_root, module_key, revision_id)
            if revision_path is None
            else Path(revision_path)
        )
        if revision_path.is_symlink() or not revision_path.is_dir():
            raise RepositoryIntegrityError(f"revision path is missing or unsafe: {revision_path}")
        bundle_path = revision_path / "bundle_manifest.json"
        if bundle_path.is_symlink() or not bundle_path.is_file():
            raise RepositoryIntegrityError(f"bundle manifest is missing or unsafe: {bundle_path}")
        try:
            bundle_raw = bundle_path.read_bytes()
            bundle_manifest = loads(bundle_raw, BundleManifest)
        except (OSError, TypeError, ValueError, UnknownFieldError) as exc:
            raise RepositorySchemaError(f"bundle manifest failed strict v2 parsing: {bundle_path}") from exc
        if not is_canonical_json_bytes(bundle_manifest, bundle_raw):
            raise RepositoryIntegrityError(f"bundle manifest is not canonical: {bundle_path}")
        if bundle_manifest.module_key != module_key or bundle_manifest.revision_id != revision_id:
            raise RepositoryIntegrityError("bundle manifest does not match its path")

        actual_files = self._relative_files(revision_path)
        expected_files = set(bundle_manifest.artifact_hashes)
        if actual_files != expected_files | {"bundle_manifest.json"}:
            raise RepositoryIntegrityError(
                f"revision file set differs from bundle manifest: {revision_path}"
            )
        artifacts: list[ArtifactRecord] = []
        for relative in sorted(expected_files):
            path = revision_path / Path(*PurePosixPath(relative).parts)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise RepositoryIntegrityError(f"revision artifact cannot be read: {path}") from exc
            if (
                ArtifactRecord(relative, bundle_manifest.artifact_kinds[relative], data).sha256
                != bundle_manifest.artifact_hashes[relative]
            ):
                raise RepositoryIntegrityError(f"revision artifact hash mismatch: {relative}")
            kind = bundle_manifest.artifact_kinds[relative]
            expected_kind = {
                "module_manifest.json": "manifest",
                "module_stub_state.json": "module_stub_state",
                "conflicts.json": "conflicts",
            }.get(relative)
            if relative.endswith("/state.json"):
                expected_kind = "state"
            if expected_kind is not None and kind != expected_kind:
                raise RepositoryIntegrityError(
                    f"repository-generated artifact kind mismatch: {relative}"
                )
            artifacts.append(ArtifactRecord(relative, kind, data))

        module_manifest_path = revision_path / "module_manifest.json"
        if "module_manifest.json" not in expected_files:
            raise RepositoryIntegrityError("bundle manifest does not include module_manifest.json")
        try:
            module_raw = module_manifest_path.read_bytes()
            module_manifest = loads(module_raw, ModuleManifest)
        except (OSError, TypeError, ValueError, UnknownFieldError) as exc:
            raise RepositorySchemaError("module manifest failed strict v2 parsing") from exc
        if not is_canonical_json_bytes(module_manifest, module_raw):
            raise RepositoryIntegrityError("module manifest is not canonical")
        if module_manifest.module_key != module_key:
            raise RepositoryIntegrityError("module manifest module_key mismatch")
        if module_manifest.revision_id != revision_id:
            raise RepositoryIntegrityError("module manifest revision_id mismatch")
        for field_name in ("revision", "accepted_generation", "base_snapshot_id", "working_snapshot_id", "analysis"):
            if getattr(module_manifest, field_name) != getattr(bundle_manifest, field_name):
                raise RepositoryIntegrityError(f"module/bundle manifest mismatch: {field_name}")
        if module_manifest.analysis.analysis_fingerprint is not None:
            raise RepositoryIntegrityError("PR05 analysis_fingerprint must remain None")

        base_paths = self._state_paths(revision_path, "base")
        working_paths = self._state_paths(revision_path, "working")
        if len(base_paths) != len(working_paths):
            raise RepositoryIntegrityError("BASE and WORKING function state sets differ")
        function_states: list[FunctionIterationState] = []
        for base_path in base_paths:
            relative_function = base_path[len("base/") : -len("/state.json")]
            working_path = f"working/{relative_function}/state.json"
            if working_path not in working_paths:
                raise RepositoryIntegrityError(f"WORKING state is missing for {relative_function}")
            base_data = (revision_path / Path(*PurePosixPath(base_path).parts)).read_bytes()
            working_data = (revision_path / Path(*PurePosixPath(working_path).parts)).read_bytes()
            if base_data != working_data:
                raise RepositoryIntegrityError(f"BASE/WORKING state bytes differ for {relative_function}")
            try:
                function_state = loads(base_data, FunctionIterationState)
            except (TypeError, ValueError, UnknownFieldError) as exc:
                raise RepositorySchemaError(f"function state failed strict v2 parsing: {base_path}") from exc
            if not is_canonical_json_bytes(function_state, base_data):
                raise RepositoryIntegrityError(f"function state is not canonical: {base_path}")
            function_states.append(function_state)
            if function_states[-1].function_key != relative_function:
                raise RepositoryIntegrityError(
                    f"function state key does not match its repository path: {base_path}"
                )

        stub_path = revision_path / "module_stub_state.json"
        conflicts_path = revision_path / "conflicts.json"
        if not stub_path.is_file() or not conflicts_path.is_file():
            raise RepositoryIntegrityError("module stub/conflict state is incomplete")
        try:
            module_stub_state = parse_collection_bytes(stub_path.read_bytes(), StubState)
            conflicts = parse_collection_bytes(conflicts_path.read_bytes(), MergeConflict)
        except (TypeError, ValueError, ArtifactRoundtripError) as exc:
            raise RepositorySchemaError("module stub/conflict state failed strict parsing") from exc

        # Re-run the same cross-artifact identity checks used before commit so
        # a tampered immutable revision can never be accepted merely because
        # each individual JSON file still parses.
        loaded_candidate = CandidateBundle(
            module_key,
            module_manifest,
            tuple(function_states),
            tuple(artifacts),
            tuple(module_stub_state),
            tuple(conflicts),
        )
        identity_issues = candidate_identity_issues(loaded_candidate)
        if identity_issues:
            details = "; ".join(issue.message for issue in identity_issues)
            raise RepositoryIntegrityError(f"revision identity invariants failed: {details}")

        current_module = root_manifest.modules.get(module_key)
        if current_module is not None:
            if current_module.revision_id == revision_id:
                if current_module != module_manifest:
                    raise RepositoryIntegrityError("root/module manifest mismatch")
            elif root_manifest is not None and current_module.revision < bundle_manifest.revision:
                # Historical loads use a synthetic root; it is valid for the
                # current root to point at a newer revision.
                pass

        effective_analysis_store = self.analysis_store if analysis_store is None else analysis_store
        if effective_analysis_store is not None:
            try:
                resolved = effective_analysis_store.resolve(bundle_manifest.analysis.analysis_id)
            except Exception as exc:
                raise RepositoryIntegrityError("accepted AnalysisRef cannot be resolved") from exc
            if resolved.ref != bundle_manifest.analysis:
                raise RepositoryIntegrityError("resolved AnalysisRef differs from bundle manifest")

        return AcceptedBundle(
            root_manifest,
            module_manifest,
            bundle_manifest,
            revision_path,
            tuple(function_states),
            tuple(module_stub_state),
            tuple(conflicts),
            tuple(artifacts),
        )

    def _current_bundle_path(self, module_key: str) -> Path:
        validate_path_component(module_key, "module_key")
        return self.modules_root / module_key

    def _current_manifest_matches(self, module_key: str, revision_id: str) -> bool:
        path = self._current_bundle_path(module_key)
        if not path.exists():
            return False
        if path.is_symlink() or not path.is_dir():
            raise RepositoryIntegrityError(f"current module path is missing or unsafe: {path}")
        manifest_path = path / "module_manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RepositoryIntegrityError(f"current module manifest is missing or unsafe: {manifest_path}")
        try:
            raw = manifest_path.read_bytes()
            manifest = loads(raw, ModuleManifest)
        except (OSError, TypeError, ValueError, UnknownFieldError) as exc:
            raise RepositorySchemaError(
                f"current module manifest failed strict v2 parsing: {manifest_path}"
            ) from exc
        if not is_canonical_json_bytes(manifest, raw):
            raise RepositoryIntegrityError(f"current module manifest is not canonical: {manifest_path}")
        return manifest.module_key == module_key and manifest.revision_id == revision_id

    @staticmethod
    def _format_json_file(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise RepositoryIntegrityError(f"JSON path is missing or unsafe: {path}")
        try:
            parsed = loads(path.read_bytes())
            path.write_bytes(pretty_json_bytes(parsed))
        except (OSError, TypeError, UnicodeError, ValueError) as exc:
            raise RepositoryError(f"could not format JSON file: {path}") from exc

    @classmethod
    def _format_json_files(cls, root: Path) -> None:
        for path in sorted(root.rglob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise RepositoryIntegrityError(f"JSON path is missing or unsafe: {path}")
            try:
                parsed = loads(path.read_bytes())
            except (OSError, TypeError, UnicodeError, ValueError):
                continue
            path.write_bytes(pretty_json_bytes(parsed))

    def _copy_current_bundle(self, source: Path, module_key: str) -> Path:
        """Copy one validated bundle into a private staging directory."""

        self._ensure_iteration_layout()
        operation_id = new_persistent_id("op", self.uuid_factory)
        staged = self.staging_root / f"current.{module_key}.{operation_id}"
        if staged.exists() or staged.is_symlink():
            raise RepositoryIntegrityError(f"current staging path already exists: {staged}")
        try:
            shutil.copytree(source, staged, symlinks=False)
            self._format_json_files(staged)
        except OSError as exc:
            if staged.exists() and staged.is_dir() and not staged.is_symlink():
                shutil.rmtree(staged)
            raise RepositoryError(f"could not stage current module bundle: {source}") from exc
        return staged

    def _swap_current_bundle(self, staged: Path, module_key: str) -> Path:
        """Replace a module directory and restore the old one if the swap fails."""

        current = self._current_bundle_path(module_key)
        operation_id = new_persistent_id("op", self.uuid_factory)
        backup = self.modules_root / f".{module_key}.{operation_id}.backup"
        if backup.exists() or backup.is_symlink():
            raise RepositoryIntegrityError(f"current backup path already exists: {backup}")
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise RepositoryIntegrityError(f"current module path is unsafe: {current}")
            os.replace(current, backup)
        try:
            os.replace(staged, current)
        except OSError:
            if backup.exists() and not current.exists():
                os.replace(backup, current)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return current

    def _remove_module_revisions(self, module_key: str) -> None:
        module_root = self.revision_root / module_key
        if not module_root.exists():
            return
        if module_root.is_symlink() or not module_root.is_dir():
            raise RepositoryIntegrityError(f"revision module root is unsafe: {module_root}")
        shutil.rmtree(module_root)

    def _remove_legacy_analysis(self) -> None:
        analysis_root = self.utgen_root / "analysis"
        if not analysis_root.exists():
            return
        if analysis_root.is_symlink() or not analysis_root.is_dir():
            raise RepositoryIntegrityError(f"analysis root is unsafe: {analysis_root}")
        pattern = re.compile(r"analysis_v1_[0-9a-f]{64}\Z")
        for path in sorted(analysis_root.iterdir()):
            if not pattern.fullmatch(path.name):
                continue
            if path.is_symlink() or not path.is_dir():
                raise RepositoryIntegrityError(f"legacy analysis path is unsafe: {path}")
            shutil.rmtree(path)

    def _remove_all_revisions(self) -> None:
        if not self.revision_root.exists():
            return
        if self.revision_root.is_symlink() or not self.revision_root.is_dir():
            raise RepositoryIntegrityError(f"revision root is unsafe: {self.revision_root}")
        for path in sorted(self.revision_root.iterdir()):
            if path.is_symlink() or not path.is_dir():
                raise RepositoryIntegrityError(f"legacy revision path is unsafe: {path}")
            validate_path_component(path.name, "module_key")
            shutil.rmtree(path)

    def compact_current(self, module_key: str) -> AcceptedBundle:
        """Publish the accepted revision into the current-only module path."""

        if not self.current_only:
            raise RepositoryError("compact_current requires current_only=True")
        module_key = validate_path_component(module_key, "module_key")
        root_manifest = self._read_root(missing_ok=False)
        module_manifest = root_manifest.modules.get(module_key)
        if module_manifest is None:
            raise RepositoryNotInitialized(f"module has no accepted revision: {module_key}")
        self._ensure_iteration_layout()
        current_path = self._current_bundle_path(module_key)
        if self._current_manifest_matches(module_key, module_manifest.revision_id):
            source_bundle = self._load_revision(
                module_key,
                module_manifest.revision_id,
                root_manifest,
                revision_path=current_path,
            )
        else:
            source_bundle = self._load_revision(module_key, module_manifest.revision_id, root_manifest)
        staged = self._copy_current_bundle(source_bundle.revision_path, module_key)
        try:
            self._load_revision(
                module_key,
                module_manifest.revision_id,
                root_manifest,
                revision_path=staged,
            )
            self._swap_current_bundle(staged, module_key)
            activate = getattr(self.analysis_store, "activate", None)
            if callable(activate):
                activate(module_manifest.analysis.analysis_id)
            self._remove_module_revisions(module_key)
            return self._load_revision(
                module_key,
                module_manifest.revision_id,
                root_manifest,
                revision_path=current_path,
            )
        except Exception:
            if staged.exists() or staged.is_symlink():
                if staged.is_symlink() or not staged.is_dir():
                    raise RepositoryIntegrityError(f"current staging path is unsafe: {staged}")
                shutil.rmtree(staged)
            raise

    def migrate_current(self) -> None:
        """Migrate accepted legacy bundles and DBs before deleting old paths."""

        if not self.current_only:
            return
        if not self.manifest_path.exists():
            return
        root_manifest = self._read_root(missing_ok=False)
        legacy_store = ContentAddressedAnalysisStore(self.root)
        importer = getattr(self.analysis_store, "import_snapshot", None)
        migrated_staging: list[Path] = []
        try:
            for module_key, module_manifest in sorted(root_manifest.modules.items()):
                current_path = self._current_bundle_path(module_key)
                if self._current_manifest_matches(module_key, module_manifest.revision_id):
                    bundle = self._load_revision(
                        module_key,
                        module_manifest.revision_id,
                        root_manifest,
                        revision_path=current_path,
                    )
                else:
                    effective_store = self.analysis_store
                    staged_available = False
                    if callable(importer):
                        try:
                            self.analysis_store.resolve(module_manifest.analysis.analysis_id)
                            staged_available = True
                        except Exception:
                            effective_store = legacy_store
                    bundle = self._load_revision(
                        module_key,
                        module_manifest.revision_id,
                        root_manifest,
                        analysis_store=effective_store,
                    )
                    if callable(importer) and not staged_available:
                        snapshot = legacy_store.resolve(module_manifest.analysis.analysis_id)
                        imported = importer(snapshot)
                        if imported != module_manifest.analysis:
                            raise RepositoryIntegrityError(
                                f"migrated AnalysisRef differs for module {module_key!r}"
                            )
                staged = self._copy_current_bundle(bundle.revision_path, module_key)
                migrated_staging.append(staged)
                self._load_revision(
                    module_key,
                    module_manifest.revision_id,
                    root_manifest,
                    revision_path=staged,
                )
                self._swap_current_bundle(staged, module_key)
                activate = getattr(self.analysis_store, "activate", None)
                if callable(activate):
                    activate(module_manifest.analysis.analysis_id)
                migrated_staging.remove(staged)
            if callable(importer):
                self._remove_legacy_analysis()
            self._remove_all_revisions()
            self._format_json_file(self.manifest_path)
        except Exception as exc:
            for staged in migrated_staging:
                if staged.exists() and staged.is_dir() and not staged.is_symlink():
                    shutil.rmtree(staged)
            if isinstance(exc, RepositoryError):
                raise
            raise RepositoryIntegrityError("current-only migration failed; legacy state was retained") from exc

    def load(self, module_key: str) -> AcceptedBundle:
        module_key = validate_path_component(module_key, "module_key")
        root_manifest = self._read_root(missing_ok=False)
        module_manifest = root_manifest.modules.get(module_key)
        if module_manifest is None:
            raise RepositoryNotInitialized(f"module has no accepted revision: {module_key}")
        if self.current_only and self._current_manifest_matches(module_key, module_manifest.revision_id):
            return self._load_revision(
                module_key,
                module_manifest.revision_id,
                root_manifest,
                revision_path=self._current_bundle_path(module_key),
            )
        return self._load_revision(module_key, module_manifest.revision_id, root_manifest)

    def load_revision(self, module_key: str, revision_id: str) -> AcceptedBundle:
        module_key = validate_path_component(module_key, "module_key")
        validate_persistent_id(revision_id, "rev")
        bundle = self._load_revision(
            module_key,
            revision_id,
            RootManifest(CURRENT_SCHEMA_VERSION, {}),
        )
        return bundle

    def _validated_parent_chain(
        self,
        module_key: str,
        current: ModuleManifest,
        root_manifest: RootManifest,
    ) -> tuple[tuple[Path, AcceptedBundle], ...]:
        """Load a current revision and every contiguous accepted parent."""

        chain: list[tuple[Path, AcceptedBundle]] = []
        seen: set[str] = set()
        revision_id = current.revision_id
        expected_revision = current.revision
        first = True
        while True:
            if revision_id in seen:
                raise RepositoryIntegrityError("history parent chain contains a cycle")
            seen.add(revision_id)
            bundle = self._load_revision(
                module_key,
                revision_id,
                root_manifest if first else RootManifest(CURRENT_SCHEMA_VERSION, {}),
            )
            if bundle.module_manifest.module_key != module_key:
                raise RepositoryIntegrityError("history revision module_key mismatch")
            if bundle.bundle_manifest.module_key != module_key:
                raise RepositoryIntegrityError("history bundle module_key mismatch")
            if bundle.revision != expected_revision:
                raise RepositoryIntegrityError(
                    f"history revision is not contiguous: expected {expected_revision}, found {bundle.revision}"
                )
            chain.append((bundle.revision_path, bundle))
            if expected_revision == 1:
                if bundle.bundle_manifest.parent_revision_id is not None or bundle.bundle_manifest.parent_revision is not None:
                    raise RepositoryIntegrityError("revision 1 must not have a parent")
                break
            parent_id = bundle.bundle_manifest.parent_revision_id
            parent_revision = bundle.bundle_manifest.parent_revision
            if parent_id is None or parent_revision is None:
                raise RepositoryIntegrityError("accepted revision is missing its parent reference")
            if parent_revision != expected_revision - 1:
                raise RepositoryIntegrityError("history parent revision is not contiguous")
            revision_id = parent_id
            expected_revision -= 1
            first = False
        return tuple(chain)

    def history(self, module_key: str) -> tuple[AcceptedBundle, ...]:
        """Return prior accepted bundles, newest parent first."""

        if self.current_only:
            self.load(module_key)
            return ()
        current = self.load(module_key)
        chain = self._validated_parent_chain(module_key, current.module_manifest, current.root_manifest)
        return tuple(bundle for _, bundle in chain[1:])

    def begin(self, module_key: str, expected_revision: int) -> IterationTransaction:
        module_key = validate_path_component(module_key, "module_key")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        self._ensure_iteration_layout()
        lock = AdvisoryFileLock(self.locks_root / f"{module_key}.lock")
        try:
            lock.acquire()
            self._inject("lock_acquired")
            root_manifest = self._read_root(missing_ok=True)
            current = root_manifest.modules.get(module_key)
            actual_revision = 0 if current is None else current.revision
            if actual_revision != expected_revision:
                raise ConcurrentStateUpdate(
                    f"{ConcurrentStateUpdate.code}: expected revision {expected_revision}, "
                    f"found {actual_revision} for module {module_key!r}"
                )
            self._inject("begin_cas_verified")
            return IterationTransaction(
                module_key=module_key,
                expected_revision=expected_revision,
                current_root=root_manifest,
                iteration_root=self.iteration_root,
                manifest_path=self.manifest_path,
                revision_root=self.revision_root,
                staging_root=self.staging_root,
                module_lock=lock,
                pointer_lock_path=self.pointer_lock_path,
                uuid_factory=self.uuid_factory,
                failure_injector=self.failure_injector,
                artifact_validators=self.artifact_validators,
                load_accepted=lambda key: self.load(key),
                load_revision=lambda key, revision_id: self.load_revision(key, revision_id),
                read_root=lambda: self._read_root(missing_ok=True),
                validate_analysis=(None if self.analysis_store is None else self.analysis_store.validate),
                pretty_json=self.current_only,
            )
        except Exception:
            lock.release()
            raise

    def recover(self) -> RecoveryReport:
        """Inspect incomplete/unreferenced paths without changing them."""

        if not self.iteration_root.exists():
            return RecoveryReport()
        root_manifest = self._read_root(missing_ok=True)
        incomplete: list[Path] = []
        if self.staging_root.exists():
            if self.staging_root.is_symlink() or not self.staging_root.is_dir():
                raise RepositoryIntegrityError(f"staging root is unsafe: {self.staging_root}")
            incomplete.extend(sorted(self.staging_root.iterdir()))
        reachable: set[Path] = set()
        for module_key, current in root_manifest.modules.items():
            if self.current_only:
                current_path = self._current_bundle_path(module_key)
                if self._current_manifest_matches(module_key, current.revision_id):
                    self._load_revision(
                        module_key,
                        current.revision_id,
                        root_manifest,
                        revision_path=current_path,
                    )
                    reachable.add(current_path.resolve())
                else:
                    legacy_path = self._safe_revision_path(
                        self.revision_root,
                        module_key,
                        current.revision_id,
                    )
                    self._load_revision(module_key, current.revision_id, root_manifest)
                    reachable.add(legacy_path.resolve())
            else:
                for revision_path, _ in self._validated_parent_chain(module_key, current, root_manifest):
                    reachable.add(revision_path.resolve())

        unreferenced: list[Path] = []
        invalid: list[Path] = []
        if self.revision_root.exists():
            if self.revision_root.is_symlink() or not self.revision_root.is_dir():
                raise RepositoryIntegrityError(f"revision root is unsafe: {self.revision_root}")
            for module_dir in sorted(self.revision_root.iterdir()):
                if module_dir.is_symlink() or not module_dir.is_dir():
                    invalid.append(module_dir)
                    continue
                module_key = module_dir.name
                try:
                    validate_path_component(module_key, "module_key")
                except Exception:
                    invalid.append(module_dir)
                    continue
                for revision_dir in sorted(module_dir.iterdir()):
                    if revision_dir.is_symlink() or not revision_dir.is_dir():
                        invalid.append(revision_dir)
                        continue
                    try:
                        validate_persistent_id(revision_dir.name, "rev")
                    except Exception:
                        invalid.append(revision_dir)
                        continue
                    if revision_dir.resolve() in reachable:
                        continue
                    unreferenced.append(revision_dir)
        return RecoveryReport(tuple(incomplete), tuple(unreferenced), tuple(invalid))

    inspect_recovery = recover

    def cleanup(self, paths: Iterable[Path]) -> None:
        """Explicitly remove only reported staging/unreferenced directories."""

        root = self.iteration_root.resolve()
        report = self.recover()
        allowed = {
            path.resolve()
            for path in (
                *report.incomplete_staging,
                *report.unreferenced_revisions,
                *report.invalid_revisions,
            )
        }
        for supplied in paths:
            path = Path(supplied)
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise RepositoryError(f"refusing to clean path outside iteration root: {path}") from exc
            if resolved not in allowed:
                raise RepositoryError(f"cleanup path is not an exact recovery target: {path}")
            if path.is_symlink() or not path.is_dir():
                raise RepositoryError(f"cleanup target is not a regular directory: {path}")
            shutil.rmtree(path)

    cleanup_recovery = cleanup


FilesystemIterationRepository = FileSystemIterationRepository


__all__ = [
    "AcceptedBundle",
    "FileSystemIterationRepository",
    "FilesystemIterationRepository",
    "IterationRepository",
    "RecoveryReport",
    "RepositoryError",
    "RepositoryIntegrityError",
    "RepositoryNotInitialized",
    "RepositorySchemaError",
    "PublishedCommitDurabilityError",
    "PublishedCommitLoadError",
]
