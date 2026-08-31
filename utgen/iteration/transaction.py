"""Candidate validation and atomic publication primitives for PR05.

This module deliberately contains no merge policy and no XML/workbook parser.
Callers provide a fully-built candidate plus validators for external artifacts;
the transaction only verifies and publishes that candidate as one immutable
revision.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
from typing import Any, Callable, Iterable, Mapping, Protocol

from .identity import IdentityError, validate_case_name, validate_case_names, validate_row_id
from .model import (
    CURRENT_SCHEMA_VERSION,
    AnalysisRef,
    BaseSnapshot,
    BundleManifest,
    FunctionIterationState,
    MergeConflict,
    ModuleManifest,
    RowMeta,
    RootManifest,
    StubState,
    UnknownFieldError,
    ModelError,
    _validate_portable_component,
    _validate_portable_relative_path,
    canonical_json_bytes,
    from_dict,
    loads,
)


_PERSISTENT_ID = {
    "snap": re.compile(r"snap_[0-9a-f]{32}\Z"),
    "rev": re.compile(r"rev_[0-9a-f]{32}\Z"),
    "op": re.compile(r"op_[0-9a-f]{32}\Z"),
}
_REQUIRED_VALIDATOR_KINDS = frozenset({"xml", "value_xml", "stub_xml", "workbook", "xlsx"})


class TransactionError(ValueError):
    """Base error for candidate transaction failures."""

    code = "STATE_TRANSACTION_ERROR"


class CandidateValidationError(TransactionError):
    code = "VALIDATION_CANDIDATE_INVALID"


class TransactionStateError(TransactionError):
    code = "STATE_TRANSACTION_STATE"


class WriterLockError(TransactionError):
    code = "STATE_WRITER_LOCKED"


class ConcurrentStateUpdate(TransactionError):
    code = "CONCURRENT_STATE_UPDATE"


class AtomicCommitError(TransactionError):
    code = "STATE_ATOMIC_COMMIT_FAILED"


class PublishedCommitDurabilityError(AtomicCommitError):
    """The root pointer was swapped, but its directory could not be fsynced."""

    code = "STATE_POINTER_PUBLISHED_DURABILITY_UNCERTAIN"


class PublishedCommitLoadError(AtomicCommitError):
    """The root pointer was swapped, but the accepted revision could not reload."""

    code = "STATE_POINTER_PUBLISHED_LOAD_FAILED"


class UnsafeArtifactPath(CandidateValidationError):
    code = "VALIDATION_UNSAFE_ARTIFACT_PATH"


class ArtifactRoundtripError(CandidateValidationError):
    code = "VALIDATION_ROUNDTRIP_FAILED"


FailureInjector = Callable[[str], None]
ArtifactValidator = Callable[[bytes], Any]


FAILURE_STAGES = (
    "lock_acquired",
    "begin_cas_verified",
    "staging_created",
    "artifact_write",
    "candidate_reparsed",
    "candidate_validated",
    "files_fsynced",
    "revision_finalized",
    "prepublish_load",
    "pointer_lock_acquired",
    "pointer_cas_verified",
    "before_pointer_swap",
    "after_pointer_swap",
    "parent_fsynced",
)

_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    errno_value
    for errno_value in (
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOTTY", None),
    )
    if errno_value is not None
)


def _validate_id(value: object, prefix: str) -> str:
    if type(value) is not str or _PERSISTENT_ID[prefix].fullmatch(value) is None:
        raise ValueError(f"{prefix}_id must be {prefix}_ plus 32 lowercase UUID4 hex digits")
    return value


def new_persistent_id(prefix: str, uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> str:
    """Create one persistent ID and reject non-RFC4122 UUID4 factories."""

    if prefix not in _PERSISTENT_ID:
        raise ValueError(f"unsupported persistent ID prefix: {prefix!r}")
    if not callable(uuid_factory):
        raise TypeError("uuid_factory must be callable")
    value = uuid_factory()
    if type(value) is not uuid.UUID:
        raise TypeError("uuid_factory must return uuid.UUID")
    if value.version != 4 or value.variant != uuid.RFC_4122:
        raise ValueError("uuid_factory must return an RFC4122 UUID4")
    result = f"{prefix}_{value.hex}"
    _validate_id(result, prefix)
    return result


def validate_persistent_id(value: object, prefix: str) -> str:
    return _validate_id(value, prefix)


def validate_relative_path(value: object) -> str:
    """Validate a portable relative path used below an immutable revision."""

    try:
        return _validate_portable_relative_path(value, "artifact path")
    except (ModelError, TypeError, ValueError) as exc:
        raise UnsafeArtifactPath(str(exc)) from exc


def validate_path_component(value: object, field_name: str = "path component") -> str:
    try:
        return _validate_portable_component(value, field_name)
    except (ModelError, TypeError, ValueError) as exc:
        raise UnsafeArtifactPath(str(exc)) from exc


@dataclass(frozen=True)
class ArtifactRecord:
    """One candidate artifact; ``data`` is never read from accepted files."""

    relative_path: str
    kind: str
    data: bytes
    validator: ArtifactValidator | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "relative_path", validate_relative_path(self.relative_path))
        if type(self.kind) is not str or not self.kind:
            raise CandidateValidationError("artifact kind must be a non-empty string")
        if type(self.data) is not bytes:
            raise TypeError("artifact data must be bytes")
        if self.validator is not None and not callable(self.validator):
            raise TypeError("artifact validator must be callable")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class CandidateBundle:
    """Strict candidate input for one module transaction.

    ``module_manifest`` carries the candidate metadata.  The transaction
    replaces only its revision number and revision ID with the accepted values;
    all source/user state and artifact bytes remain caller-owned immutable
    inputs.
    """

    module_key: str
    module_manifest: ModuleManifest
    function_states: tuple[FunctionIterationState, ...]
    artifacts: tuple[ArtifactRecord, ...]
    module_stub_state: tuple[StubState, ...] = ()
    conflicts: tuple[MergeConflict, ...] = ()
    operation_id: str | None = None

    def __post_init__(self) -> None:
        validate_path_component(self.module_key, "module_key")
        if not isinstance(self.module_manifest, ModuleManifest):
            raise TypeError("module_manifest must be a ModuleManifest")
        if self.module_manifest.module_key != self.module_key:
            raise CandidateValidationError("candidate module_key does not match module_manifest")
        if type(self.function_states) is not tuple:
            raise TypeError("function_states must be a tuple")
        if not self.function_states:
            raise CandidateValidationError("candidate must contain at least one function state")
        if not all(isinstance(state, FunctionIterationState) for state in self.function_states):
            raise TypeError("function_states must contain only FunctionIterationState records")
        function_keys = [state.function_key for state in self.function_states]
        if len(function_keys) != len(set(function_keys)):
            raise CandidateValidationError("candidate contains duplicate function states")
        if type(self.artifacts) is not tuple:
            raise TypeError("artifacts must be a tuple")
        if not all(isinstance(artifact, ArtifactRecord) for artifact in self.artifacts):
            raise TypeError("artifacts must contain only ArtifactRecord records")
        paths = [artifact.relative_path for artifact in self.artifacts]
        if len(paths) != len(set(paths)):
            raise CandidateValidationError("candidate contains duplicate artifact paths")
        if type(self.module_stub_state) is not tuple:
            raise TypeError("module_stub_state must be a tuple")
        if not all(isinstance(stub, StubState) for stub in self.module_stub_state):
            raise TypeError("module_stub_state must contain only StubState records")
        if type(self.conflicts) is not tuple:
            raise TypeError("conflicts must be a tuple")
        if not all(isinstance(conflict, MergeConflict) for conflict in self.conflicts):
            raise TypeError("conflicts must contain only MergeConflict records")
        if self.operation_id is not None:
            validate_persistent_id(self.operation_id, "op")


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def valid(self) -> bool:
        return self.ok


_CASE_NUMBER_SUFFIX = re.compile(r"\.c([0-9]+)\Z")


def candidate_identity_issues(candidate: CandidateBundle) -> tuple[ValidationIssue, ...]:
    """Validate the cross-file identity invariants of one candidate bundle.

    The domain model validates each record in isolation.  This deliberately
    small second pass validates relationships which must remain true when the
    BASE/WORKING state files, module manifest, and function rows are published
    together.  The repository reuses it when loading an immutable revision.
    """

    issues: list[ValidationIssue] = []
    module = candidate.module_manifest
    states = tuple(sorted(candidate.function_states, key=lambda item: (item.function_key, item.function)))

    function_keys = [state.function_key for state in states]
    function_names = [state.function for state in states]
    if len(function_keys) != len(set(function_keys)):
        issues.append(ValidationIssue("IDENTITY_DUPLICATE_FUNCTION_KEY", "function keys must be unique"))
    if len(function_names) != len(set(function_names)):
        issues.append(ValidationIssue("IDENTITY_DUPLICATE_FUNCTION_NAME", "function names must be unique"))
    expected_functions = set(function_names)
    if set(module.function_versions) != expected_functions:
        issues.append(
            ValidationIssue(
                "IDENTITY_FUNCTION_VERSION_KEYS",
                "module function_versions keys must exactly match function state names",
            )
        )

    row_owners: dict[str, str] = {}
    for state in states:
        state_path = f"function_states.{state.function_key}"
        try:
            validate_path_component(state.function_key, "function_key")
        except CandidateValidationError as exc:
            issues.append(ValidationIssue(exc.code, str(exc), state_path))
        if state.source_file != module.source_file:
            issues.append(
                ValidationIssue(
                    "IDENTITY_SOURCE_FILE_MISMATCH",
                    f"{state_path} source_file must match module manifest",
                    state_path,
                )
            )
        if state.accepted_analysis_id != module.analysis.analysis_id:
            issues.append(
                ValidationIssue(
                    "IDENTITY_ANALYSIS_ID_MISMATCH",
                    f"{state_path} accepted_analysis_id must match module analysis",
                    state_path,
                )
            )
        if state.function in module.function_versions and module.function_versions[state.function] != state.display_version:
            issues.append(
                ValidationIssue(
                    "IDENTITY_DISPLAY_VERSION_MISMATCH",
                    f"{state_path} display_version disagrees with module function_versions",
                    state_path,
                )
            )
        if state.base.snapshot_id != module.base_snapshot_id:
            issues.append(
                ValidationIssue(
                    "IDENTITY_BASE_SNAPSHOT_MISMATCH",
                    f"{state_path} BASE snapshot must match module manifest",
                    state_path,
                )
            )
        if state.working.snapshot_id != module.working_snapshot_id:
            issues.append(
                ValidationIssue(
                    "IDENTITY_WORKING_SNAPSHOT_MISMATCH",
                    f"{state_path} WORKING snapshot must match module manifest",
                    state_path,
                )
            )
        if state.base.generation != state.working.generation or state.base.generation != module.accepted_generation:
            issues.append(
                ValidationIssue(
                    "IDENTITY_GENERATION_MISMATCH",
                    f"{state_path} BASE/WORKING generation must match module accepted_generation",
                    state_path,
                )
            )
        for tombstone_index, tombstone in enumerate(state.working.tombstones):
            if (
                tombstone.generation_deleted > state.working.generation
                or tombstone.generation_deleted > module.accepted_generation
            ):
                issues.append(
                    ValidationIssue(
                        "IDENTITY_TOMBSTONE_GENERATION",
                        "working tombstone generation_deleted must not exceed its snapshot or module generation",
                        f"{state_path}.working.tombstones[{tombstone_index}]",
                    )
                )

        snapshot_rows: dict[str, dict[str, RowMeta]] = {}
        for slot, snapshot in (("base", state.base), ("working", state.working)):
            seen_ids: set[str] = set()
            seen_cases: set[str] = set()
            rows_by_id: dict[str, RowMeta] = {}
            row_metas: list[RowMeta] = []
            for index, row in enumerate(snapshot.rows):
                path = f"{state_path}.{slot}.rows[{index}]"
                meta = getattr(row, "meta", None)
                if not isinstance(meta, RowMeta):
                    issues.append(
                        ValidationIssue(
                            "IDENTITY_ROW_META_REQUIRED",
                            "every accepted BASE/WORKING TestRow must carry RowMeta",
                            path,
                        )
                    )
                    continue
                try:
                    validate_row_id(meta.row_id)
                except (IdentityError, TypeError, ValueError) as exc:
                    issues.append(
                        ValidationIssue(
                            "IDENTITY_ROW_ID_FORMAT",
                            str(exc),
                            path,
                        )
                    )
                try:
                    validate_case_name(meta.case_name)
                except (IdentityError, TypeError, ValueError) as exc:
                    issues.append(
                        ValidationIssue(
                            "IDENTITY_CASE_NAME_FORMAT",
                            str(exc),
                            path,
                        )
                    )
                if meta.function_key != state.function_key:
                    issues.append(
                        ValidationIssue(
                            "IDENTITY_ROW_FUNCTION_KEY",
                            "row metadata function_key must match its function state",
                            path,
                        )
                    )
                row_metas.append(meta)
                if meta.row_id in seen_ids:
                    issues.append(ValidationIssue("IDENTITY_DUPLICATE_ROW_ID", "row_id must be unique per snapshot", path))
                if meta.case_name in seen_cases:
                    issues.append(
                        ValidationIssue("IDENTITY_DUPLICATE_CASE_NAME", "case_name must be unique per snapshot", path)
                    )
                seen_ids.add(meta.row_id)
                seen_cases.add(meta.case_name)
                rows_by_id[meta.row_id] = meta
                owner = row_owners.setdefault(meta.row_id, state.function_key)
                if owner != state.function_key:
                    issues.append(
                        ValidationIssue(
                            "IDENTITY_CROSS_FUNCTION_ROW_ID",
                            f"row_id {meta.row_id!r} is shared by multiple functions",
                            path,
                        )
                    )
            snapshot_rows[slot] = rows_by_id
            try:
                # PR03's validator rejects both duplicate labels and reuse of
                # one numeric case suffix under another source name.  Sorting
                # makes the resulting diagnostic deterministic.
                validate_case_names(tuple(sorted(meta.case_name for meta in row_metas)))
            except (IdentityError, TypeError, ValueError) as exc:
                issues.append(
                    ValidationIssue(
                        "IDENTITY_CASE_NUMBER_REUSE",
                        str(exc),
                        f"{state_path}.{slot}.rows",
                    )
                )

        working_ids = tuple(meta.row_id for row in snapshot_rows["working"].values())
        # ``rows_by_id`` preserves row insertion order, so this is the exact
        # order of the decoded WORKING rows rather than a set comparison.
        if working_ids != state.working.row_order:
            issues.append(
                ValidationIssue(
                    "IDENTITY_ROW_ORDER_MISMATCH",
                    "WorkingSnapshot.row_order must exactly equal its row IDs in row order",
                    f"{state_path}.working.row_order",
                )
            )
        if set(snapshot_rows["base"]) & set(snapshot_rows["working"]):
            for row_id in set(snapshot_rows["base"]) & set(snapshot_rows["working"]):
                if snapshot_rows["base"][row_id].case_name != snapshot_rows["working"][row_id].case_name:
                    issues.append(
                        ValidationIssue(
                            "IDENTITY_CASE_RENAME",
                            f"row_id {row_id!r} changed case_name between BASE and WORKING",
                            state_path,
                        )
                    )
        all_case_numbers: list[int] = []
        case_number_owners: dict[int, set[str]] = {}
        for snapshot in (state.base, state.working):
            for row in snapshot.rows:
                meta = getattr(row, "meta", None)
                if isinstance(meta, RowMeta):
                    match = _CASE_NUMBER_SUFFIX.search(meta.case_name)
                    if match is not None:
                        number = int(match.group(1))
                        all_case_numbers.append(number)
                        case_number_owners.setdefault(number, set()).add(meta.row_id)
        for number in sorted(case_number_owners):
            owners = sorted(case_number_owners[number])
            if len(owners) > 1:
                issues.append(
                    ValidationIssue(
                        "IDENTITY_CASE_NUMBER_REUSE",
                        f"case number {number} is assigned to multiple row IDs: {', '.join(owners)}",
                        state_path,
                    )
                )
        if all_case_numbers and state.next_case_number <= max(all_case_numbers):
            issues.append(
                ValidationIssue(
                    "IDENTITY_NEXT_CASE_NUMBER",
                    "next_case_number must be greater than every BASE/WORKING case number",
                    state_path,
                )
            )

    return tuple(sorted(issues, key=lambda issue: (issue.code, issue.path or "", issue.message)))


_RESERVED_ARTIFACT_NAMES = frozenset(
    {"bundle_manifest.json", "module_manifest.json", "module_stub_state.json", "conflicts.json"}
)


def candidate_artifact_structure_issues(candidate: CandidateBundle) -> tuple[ValidationIssue, ...]:
    """Reject user artifacts which overlap repository-owned state paths."""

    issues: list[ValidationIssue] = []
    candidate_paths = [artifact.relative_path for artifact in candidate.artifacts]
    generated_paths: list[str] = list(_RESERVED_ARTIFACT_NAMES)
    for state in candidate.function_states:
        # Function keys are checked separately; using the raw value here keeps
        # malformed keys visible as validation issues instead of constructing a
        # path which could escape the revision.
        generated_paths.extend(
            (f"base/{state.function_key}/state.json", f"working/{state.function_key}/state.json")
        )
    all_paths = candidate_paths + generated_paths
    for artifact in candidate.artifacts:
        path = artifact.relative_path
        if path in _RESERVED_ARTIFACT_NAMES:
            issues.append(
                ValidationIssue(
                    "VALIDATION_RESERVED_ARTIFACT_PATH",
                    f"artifact path {path!r} is reserved for repository-generated state",
                    path,
                )
            )
        if (
            (path.startswith("base/") and path.endswith("/state.json"))
            or (path.startswith("working/") and path.endswith("/state.json"))
        ):
            issues.append(
                ValidationIssue(
                    "VALIDATION_RESERVED_ARTIFACT_PATH",
                    f"artifact path {path!r} is reserved for repository-generated state",
                    path,
                )
            )

    for index, left in enumerate(sorted(all_paths)):
        for right in sorted(all_paths)[index + 1 :]:
            if right == left or right.startswith(left + "/") or left.startswith(right + "/"):
                # Generated/generated entries are known to be disjoint.  Any
                # collision involving a caller-owned artifact is invalid.
                if left in candidate_paths or right in candidate_paths:
                    path = left if left in candidate_paths else right
                    issues.append(
                        ValidationIssue(
                            "VALIDATION_ARTIFACT_PREFIX_COLLISION",
                            f"artifact path prefixes another repository path: {left!r}, {right!r}",
                            path,
                        )
                    )
    # Keep report deterministic even when the same reserved path also has a
    # prefix collision.
    return tuple(issues)


class _LockHandle(Protocol):
    def acquire(self) -> None: ...
    def release(self) -> None: ...


class Transaction(Protocol):
    """Stable public transaction contract implemented by IterationTransaction."""

    def stage(self, candidate: CandidateBundle) -> None: ...
    def validate(self) -> ValidationReport: ...
    def commit(self) -> Any: ...
    def abort(self) -> None: ...


class AdvisoryFileLock:
    """A non-blocking OS advisory lock held by one transaction."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._stream: Any = None

    def acquire(self) -> None:
        if self._stream is not None:
            raise WriterLockError("writer lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise WriterLockError(f"writer lock path is a symlink: {self.path}")
        try:
            stream = self.path.open("a+b")
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError) as exc:
            try:
                stream.close()  # type: ignore[union-attr]
            except (UnboundLocalError, AttributeError, OSError):
                pass
            raise WriterLockError(f"{WriterLockError.code}: writer lock unavailable: {self.path}") from exc
        self._stream = stream

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    @property
    def held(self) -> bool:
        return self._stream is not None


def _safe_remove_tree(path: Path, root: Path) -> None:
    """Remove only an exact descendant known to be a transaction directory."""

    path = Path(path)
    root = Path(root)
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise AtomicCommitError(f"refusing to clean unsafe transaction path: {path}") from exc
    if resolved_path == resolved_root or not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise AtomicCommitError(f"refusing to clean non-directory transaction path: {path}")
    shutil.rmtree(path)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def fsync_directory(path: Path) -> None:
    """Fsync a directory where the platform exposes directory descriptors."""

    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        os.close(descriptor)


def _collection_bytes(schema_name: str, values: Iterable[Any]) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "items": [value.to_dict() for value in values],
        }
    )


def parse_collection_bytes(data: bytes, model_type: type) -> tuple[Any, ...]:
    parsed = loads(data)
    if type(parsed) is not dict or set(parsed) != {"schema_version", "items"}:
        raise ArtifactRoundtripError("collection state must contain only schema_version and items")
    if canonical_json_bytes(parsed) != data:
        raise ArtifactRoundtripError("collection state is not canonical UTF-8 JSON")
    if parsed["schema_version"] != CURRENT_SCHEMA_VERSION:
        raise ArtifactRoundtripError("collection state has unsupported schema_version")
    if type(parsed["items"]) is not list:
        raise ArtifactRoundtripError("collection state items must be an array")
    try:
        return tuple(from_dict(item, model_type) for item in parsed["items"])
    except (TypeError, ValueError, KeyError, UnknownFieldError) as exc:
        raise ArtifactRoundtripError("collection state item failed strict parsing") from exc


class IterationTransaction:
    """One stage/validate/commit/abort lifecycle under a module lock."""

    def __init__(
        self,
        *,
        module_key: str,
        expected_revision: int,
        current_root: RootManifest,
        iteration_root: Path,
        manifest_path: Path,
        revision_root: Path,
        staging_root: Path,
        module_lock: AdvisoryFileLock,
        pointer_lock_path: Path,
        uuid_factory: Callable[[], uuid.UUID],
        failure_injector: FailureInjector | None,
        artifact_validators: Mapping[str, ArtifactValidator],
        load_accepted: Callable[[str], Any],
        load_revision: Callable[[str, str], Any] | None,
        read_root: Callable[[], RootManifest],
        validate_analysis: Callable[[str], Any] | None = None,
    ) -> None:
        self.module_key = validate_path_component(module_key, "module_key")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        self.expected_revision = expected_revision
        self._current_root = current_root
        current_module = current_root.modules.get(self.module_key)
        self._expected_module_revision_id = None if current_module is None else current_module.revision_id
        self._iteration_root = Path(iteration_root)
        self._manifest_path = Path(manifest_path)
        self._revision_root = Path(revision_root)
        self._staging_root = Path(staging_root)
        self._module_lock = module_lock
        self._pointer_lock_path = Path(pointer_lock_path)
        self._uuid_factory = uuid_factory
        self._failure_injector = failure_injector
        self._artifact_validators = dict(artifact_validators)
        self._load_accepted = load_accepted
        self._load_revision = load_revision
        self._read_root = read_root
        self._validate_analysis = validate_analysis
        self.operation_id = new_persistent_id("op", uuid_factory)
        self.revision_id = new_persistent_id("rev", uuid_factory)
        self._candidate: CandidateBundle | None = None
        self._validated = False
        self._state = "open"
        self._staging_path: Path | None = None
        self._finalized_path: Path | None = None
        self._pointer_swap_published = False

    def _inject(self, stage: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(stage)

    @property
    def state(self) -> str:
        return self._state

    @property
    def candidate(self) -> CandidateBundle | None:
        return self._candidate

    def stage(self, candidate: CandidateBundle) -> None:
        if self._state != "open":
            raise TransactionStateError("stage is allowed exactly once while transaction is open")
        if not isinstance(candidate, CandidateBundle):
            raise TypeError("stage expects a CandidateBundle")
        if candidate.module_key != self.module_key:
            raise CandidateValidationError("candidate module does not match transaction module")
        self._candidate = candidate
        self._state = "staged"

    def _candidate_artifacts(self, candidate: CandidateBundle, published_manifest: ModuleManifest) -> tuple[ArtifactRecord, ...]:
        records = list(candidate.artifacts)
        reserved = {record.relative_path for record in records}

        def add(record: ArtifactRecord) -> None:
            if record.relative_path in reserved:
                raise CandidateValidationError(f"candidate artifact collides with generated path: {record.relative_path}")
            records.append(record)
            reserved.add(record.relative_path)

        for state in candidate.function_states:
            component = validate_path_component(state.function_key, "function_key")
            state_bytes = canonical_json_bytes(state)
            add(ArtifactRecord(f"base/{component}/state.json", "state", state_bytes))
            add(ArtifactRecord(f"working/{component}/state.json", "state", state_bytes))
        add(
            ArtifactRecord(
                "module_manifest.json",
                "manifest",
                canonical_json_bytes(published_manifest),
            )
        )
        add(
            ArtifactRecord(
                "module_stub_state.json",
                "module_stub_state",
                _collection_bytes("module_stub_state", candidate.module_stub_state),
            )
        )
        add(
            ArtifactRecord(
                "conflicts.json",
                "conflicts",
                _collection_bytes("conflicts", candidate.conflicts),
            )
        )
        return tuple(records)

    def _validate_candidate(self) -> ValidationReport:
        candidate = self._candidate
        if candidate is None:
            return ValidationReport((ValidationIssue("VALIDATION_NO_CANDIDATE", "no candidate has been staged"),))
        issues: list[ValidationIssue] = []
        try:
            candidate.__post_init__()
            candidate.module_manifest.__post_init__()
            issues.extend(candidate_identity_issues(candidate))
            issues.extend(candidate_artifact_structure_issues(candidate))
            if candidate.module_manifest.analysis.analysis_fingerprint is not None:
                issues.append(
                    ValidationIssue(
                        "ANALYSIS_FINGERPRINT_UNAVAILABLE",
                        "PR05 AnalysisRef.analysis_fingerprint must remain None",
                    )
                )
            if self._validate_analysis is None:
                issues.append(
                    ValidationIssue(
                        "ANALYSIS_DB_STORE_REQUIRED",
                        "a strict AnalysisStore.validate callback is required",
                    )
                )
            else:
                try:
                    self._validate_analysis(candidate.module_manifest.analysis.analysis_id)
                except Exception as exc:
                    issues.append(
                        ValidationIssue(
                            "ANALYSIS_DB_INTEGRITY_ERROR",
                            "candidate AnalysisRef cannot be validated by the configured AnalysisStore",
                        )
                    )
            if self.expected_revision == 0 and candidate.module_manifest.accepted_generation != 0:
                issues.append(
                    ValidationIssue(
                        "VALIDATION_INITIAL_GENERATION",
                        "the first accepted candidate must use accepted_generation=0",
                    )
                )
            for state in candidate.function_states:
                validate_path_component(state.function_key, "function_key")
                if state.accepted_analysis_id != candidate.module_manifest.analysis.analysis_id:
                    issues.append(
                        ValidationIssue(
                            "ANALYSIS_DB_MISMATCH",
                            f"function state {state.function_key!r} is bound to another analysis ID",
                        )
                    )
                if state.function in candidate.module_manifest.function_versions:
                    if candidate.module_manifest.function_versions[state.function] != state.display_version:
                        issues.append(
                            ValidationIssue(
                                "VERSION_METADATA_MISMATCH",
                                f"function state {state.function!r} disagrees with manifest version",
                            )
                        )
            for artifact in candidate.artifacts:
                validator = artifact.validator or self._artifact_validators.get(artifact.kind)
                if artifact.kind in _REQUIRED_VALIDATOR_KINDS and validator is None:
                    issues.append(
                        ValidationIssue(
                            "VALIDATION_MISSING_ARTIFACT_VALIDATOR",
                            f"artifact kind {artifact.kind!r} requires an injected semantic validator",
                            artifact.relative_path,
                        )
                    )
                try:
                    validate_relative_path(artifact.relative_path)
                except CandidateValidationError as exc:
                    issues.append(ValidationIssue(exc.code, str(exc), artifact.relative_path))
        except (TypeError, ValueError, CandidateValidationError) as exc:
            issues.append(ValidationIssue(getattr(exc, "code", "VALIDATION_CANDIDATE_INVALID"), str(exc)))
        return ValidationReport(tuple(issues))

    def validate(self) -> ValidationReport:
        if self._state not in {"staged", "validated"}:
            raise TransactionStateError("validate requires one staged candidate")
        report = self._validate_candidate()
        if report.ok:
            self._validated = True
            self._state = "validated"
        return report

    def _write_record(self, root: Path, record: ArtifactRecord) -> None:
        target = root / Path(*PurePosixPath(record.relative_path).parts)
        target_parent = target.parent
        target_parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            raise AtomicCommitError(f"refusing to overwrite staged artifact: {record.relative_path}")
        self._inject("artifact_write")
        try:
            with target.open("xb") as stream:
                stream.write(record.data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise AtomicCommitError(f"failed to write candidate artifact: {record.relative_path}") from exc

        validator = record.validator or self._artifact_validators.get(record.kind)
        if record.kind in _REQUIRED_VALIDATOR_KINDS:
            if validator is None:
                raise AtomicCommitError(
                    f"{CandidateValidationError.code}: missing semantic validator for {record.relative_path}"
                )
            try:
                validator(record.data)
            except Exception as exc:
                raise ArtifactRoundtripError(
                    f"semantic validator rejected candidate artifact: {record.relative_path}"
                ) from exc

    def _read_staged_records(self, root: Path, records: tuple[ArtifactRecord, ...]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for record in records:
            path = root / Path(*PurePosixPath(record.relative_path).parts)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ArtifactRoundtripError(f"candidate artifact cannot be reread: {record.relative_path}") from exc
            if data != record.data:
                raise ArtifactRoundtripError(f"candidate artifact changed during staging: {record.relative_path}")
            result[record.relative_path] = data
        return result

    @staticmethod
    def _validate_internal_record(record: ArtifactRecord, data: bytes) -> None:
        """Reparse repository-generated JSON instead of checking existence only."""

        try:
            if record.relative_path.endswith("/state.json"):
                loads(data, FunctionIterationState)
            elif record.relative_path == "module_manifest.json":
                loads(data, ModuleManifest)
            elif record.relative_path == "module_stub_state.json":
                parse_collection_bytes(data, StubState)
            elif record.relative_path == "conflicts.json":
                parse_collection_bytes(data, MergeConflict)
        except Exception as exc:
            raise ArtifactRoundtripError(
                f"repository-generated state failed strict round-trip: {record.relative_path}"
            ) from exc

    def _publish_module_manifest(self, candidate: CandidateBundle) -> ModuleManifest:
        manifest = replace(
            candidate.module_manifest,
            revision=self.expected_revision + 1,
            revision_id=self.revision_id,
        )
        manifest.__post_init__()
        return manifest

    def _new_root_after_pointer_lock(self, module_manifest: ModuleManifest) -> RootManifest:
        latest = self._read_root()
        current = latest.modules.get(self.module_key)
        current_revision = 0 if current is None else current.revision
        current_revision_id = None if current is None else current.revision_id
        if (
            current_revision != self.expected_revision
            or current_revision_id != self._expected_module_revision_id
        ):
            raise ConcurrentStateUpdate(
                f"{ConcurrentStateUpdate.code}: expected revision {self.expected_revision}, "
                f"found {current_revision} for module {self.module_key!r}"
            )
        modules = dict(latest.modules)
        modules[self.module_key] = module_manifest
        return RootManifest(CURRENT_SCHEMA_VERSION, modules)

    def _write_root_pointer(self, root_manifest: RootManifest) -> None:
        pointer_parent = self._manifest_path.parent
        pointer_parent.mkdir(parents=True, exist_ok=True)
        temporary = pointer_parent / f"manifest.json.{self.operation_id}.tmp"
        if temporary.exists() or temporary.is_symlink():
            raise AtomicCommitError(f"manifest temporary path already exists: {temporary}")
        data = canonical_json_bytes(root_manifest)
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._inject("before_pointer_swap")
            if self._manifest_path.is_symlink():
                raise AtomicCommitError("root manifest path is a symlink")
            os.replace(temporary, self._manifest_path)
            # This hook deliberately runs after the atomic rename.  An
            # injected failure here must be reported as post-pointer
            # uncertainty, never as a pre-publication abort.
            self._inject("after_pointer_swap")
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            # A mocked or unusual filesystem may report an error after the
            # rename has become visible.  Detect that state before deciding
            # the transaction failed before publication.
            try:
                if (
                    not self._manifest_path.is_symlink()
                    and self._manifest_path.is_file()
                    and self._manifest_path.read_bytes() == data
                ):
                    self._pointer_swap_published = True
                    raise PublishedCommitDurabilityError(
                        f"{PublishedCommitDurabilityError.code}: root pointer publication status is uncertain"
                    ) from exc
            except PublishedCommitDurabilityError:
                raise
            except OSError:
                pass
            if isinstance(exc, TransactionError):
                raise
            raise AtomicCommitError("failed to atomically replace root manifest pointer") from exc

    def commit(self) -> Any:
        if self._state == "committed":
            raise TransactionStateError("commit may be called only once")
        if self._state in {"aborted", "failed"}:
            raise TransactionStateError("cannot commit a terminal transaction")
        if self._state != "validated" or not self._validated:
            raise TransactionStateError("commit requires a successful validate() call")
        candidate = self._candidate
        assert candidate is not None
        staging_path = self._staging_root / f"{self.module_key}.{self.operation_id}"
        final_path = self._revision_root / self.module_key / self.revision_id
        self._staging_path = staging_path
        pointer_published = False
        try:
            published_manifest = self._publish_module_manifest(candidate)
            self._staging_root.mkdir(parents=True, exist_ok=True)
            if staging_path.exists() or staging_path.is_symlink():
                raise AtomicCommitError(f"staging path already exists: {staging_path}")
            staging_path.mkdir(parents=True)
            self._inject("staging_created")
            records = self._candidate_artifacts(candidate, published_manifest)
            for record in records:
                self._write_record(staging_path, record)
            self._inject("candidate_reparsed")
            staged_bytes = self._read_staged_records(staging_path, records)
            for record in records:
                if hashlib.sha256(staged_bytes[record.relative_path]).hexdigest() != record.sha256:
                    raise ArtifactRoundtripError(f"candidate hash changed: {record.relative_path}")
                self._validate_internal_record(record, staged_bytes[record.relative_path])
                validator = record.validator or self._artifact_validators.get(record.kind)
                if record.kind in _REQUIRED_VALIDATOR_KINDS:
                    if validator is None:
                        raise ArtifactRoundtripError(
                            f"missing semantic validator for {record.relative_path}"
                        )
                    try:
                        validator(staged_bytes[record.relative_path])
                    except Exception as exc:
                        raise ArtifactRoundtripError(
                            f"semantic validator rejected reread artifact: {record.relative_path}"
                        ) from exc
            self._inject("candidate_validated")

            hashes = {record.relative_path: record.sha256 for record in records}
            kinds = {record.relative_path: record.kind for record in records}
            bundle_manifest = BundleManifest(
                CURRENT_SCHEMA_VERSION,
                self.module_key,
                self.revision_id,
                self.expected_revision + 1,
                None if self.expected_revision == 0 else self._current_root.modules[self.module_key].revision_id,
                published_manifest.accepted_generation,
                published_manifest.base_snapshot_id,
                published_manifest.working_snapshot_id,
                published_manifest.analysis,
                hashes,
                None if self.expected_revision == 0 else self.expected_revision,
                kinds,
            )
            bundle_path = staging_path / "bundle_manifest.json"
            with bundle_path.open("xb") as stream:
                stream.write(canonical_json_bytes(bundle_manifest))
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(staging_path)
            self._inject("files_fsynced")

            final_parent = final_path.parent
            final_parent.mkdir(parents=True, exist_ok=True)
            if final_parent.is_symlink() or final_path.exists() or final_path.is_symlink():
                raise AtomicCommitError(f"immutable revision target already exists or is unsafe: {final_path}")
            os.replace(staging_path, final_path)
            self._finalized_path = final_path
            self._staging_path = None
            fsync_directory(final_parent)
            self._inject("revision_finalized")
            self._inject("parent_fsynced")

            if self._load_revision is not None:
                try:
                    self._load_revision(self.module_key, self.revision_id)
                except Exception as exc:
                    raise AtomicCommitError(
                        f"{AtomicCommitError.code}: finalized revision failed pre-publish load validation"
                    ) from exc
            self._inject("prepublish_load")

            pointer_lock = AdvisoryFileLock(self._pointer_lock_path)
            try:
                pointer_lock.acquire()
                self._inject("pointer_lock_acquired")
                new_root = self._new_root_after_pointer_lock(published_manifest)
                self._inject("pointer_cas_verified")
                self._write_root_pointer(new_root)
                pointer_published = True
                try:
                    fsync_directory(self._manifest_path.parent)
                except Exception as exc:
                    self._state = "committed"
                    self._module_lock.release()
                    raise PublishedCommitDurabilityError(
                        f"{PublishedCommitDurabilityError.code}: root pointer is published; reload is required"
                    ) from exc
            finally:
                pointer_lock.release()

            self._state = "committed"
            self._module_lock.release()
            try:
                return self._load_accepted(self.module_key)
            except Exception as exc:
                raise PublishedCommitLoadError(
                    f"{PublishedCommitLoadError.code}: root pointer is published but accepted revision reload failed"
                ) from exc
        except Exception as exc:
            if self._staging_path is not None:
                try:
                    _safe_remove_tree(self._staging_path, self._staging_root)
                except Exception:
                    pass
                self._staging_path = None
            if pointer_published or self._pointer_swap_published:
                # The root pointer is already visible; do not report a
                # successful publication as an abortable transaction.
                pointer_published = True
                self._state = "committed"
                if self._module_lock.held:
                    self._module_lock.release()
            elif self._state != "committed":
                # Every failure before the pointer swap is terminal.  This
                # prevents a caller from retrying after the module lock has
                # been released and accidentally publishing a different
                # transaction under stale assumptions.
                self._state = "failed"
            if self._module_lock.held and self._state != "committed":
                self._module_lock.release()
            if isinstance(exc, TransactionError):
                raise
            raise AtomicCommitError(f"{AtomicCommitError.code}: candidate publication failed") from exc

    def abort(self) -> None:
        if self._state == "committed":
            return
        if self._state in {"aborted", "failed"}:
            return
        if self._staging_path is not None:
            try:
                _safe_remove_tree(self._staging_path, self._staging_root)
            finally:
                self._staging_path = None
        self._state = "aborted"
        if self._module_lock.held:
            self._module_lock.release()


__all__ = [
    "AdvisoryFileLock",
    "ArtifactRecord",
    "ArtifactRoundtripError",
    "AtomicCommitError",
    "CandidateBundle",
    "CandidateValidationError",
    "ConcurrentStateUpdate",
    "FAILURE_STAGES",
    "IterationTransaction",
    "Transaction",
    "TransactionError",
    "TransactionStateError",
    "UnsafeArtifactPath",
    "ValidationIssue",
    "ValidationReport",
    "WriterLockError",
    "PublishedCommitDurabilityError",
    "PublishedCommitLoadError",
    "candidate_artifact_structure_issues",
    "candidate_identity_issues",
    "fsync_directory",
    "new_persistent_id",
    "parse_collection_bytes",
    "validate_path_component",
    "validate_persistent_id",
    "validate_relative_path",
]
