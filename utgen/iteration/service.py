"""Iteration orchestration services and Merge A/B workflow policy.

This module intentionally retains the historical single-file service boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import uuid
from typing import Any, Callable, Literal
from .adapters.stub_xml import StubSnapshot
from .adapters.value_xml import FunctionSnapshot
from .model import DisplayVersion, MergeConflict
from .repository import AcceptedBundle
from .selective import PhaseMetrics
from .transaction import CandidateBundle, new_persistent_id
from .versioning import format_display_version
import re
from typing import Any, Mapping
from .model import MergeConflict
from .transaction import ArtifactRecord, ValidationReport
from copy import deepcopy
from dataclasses import replace
import inspect
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from .adapters.stub_xml import (
    CalledFunction,
    StubParameter as XmlStubParameter,
    StubSnapshot,
    StubXmlAdapter,
)
from .adapters.value_xml import FunctionSnapshot, ValueXmlAdapter
from .adapters._xml_common import join_stream
from .adapters.tessy_workbook import WorkbookSnapshot
from .identity import allocate_case_name
from .merge_a import (
    _argument_state_from_value_argument,
    _canonical_argument_name,
    _is_internal_argument_name,
)
from .merge_a import MergeAEngine, MergeAResult
from .model import (
    ArgumentState,
    BaseSnapshot,
    CellValue,
    DisplayVersion,
    FunctionIterationState,
    MergeConflict,
    RowMeta,
    StubParameter,
    StubState,
    Tombstone,
    WorkingSnapshot,
    canonical_json_bytes,
)
from .repository import AcceptedBundle, IterationRepository
from .resolution import (
    ResolutionContext,
    ResolutionDecision,
    ResolutionError,
    ResolutionConflictError,
    ResolutionValidationError,
    decision_for_conflict,
    resolution_context_for,
    stable_conflict_key,
)
from .transaction import (
    ArtifactRecord,
    CandidateBundle,
    ConcurrentStateUpdate,
    PublishedCommitDurabilityError,
    PublishedCommitLoadError,
    ValidationReport,
    new_persistent_id,
)
from .versioning import advance_merge_a
from utgen.model import TestRow
from .model import AnalysisRef, MergeConflict, StubParameter, StubState
from .adapters.value_xml import FunctionSnapshot, ValueArgument, ValueXmlAdapter
from .merge_b import _obligations, _row_meta
from .model import (
    ArgumentState,
    CellValue,
    DisplayVersion,
    FunctionIterationState,
    StubParameter,
    StubState,
    canonical_json_bytes,
)
from .transaction import ArtifactRecord
from .merge_b import (
    StubLifecycleResult,
    _case_name,
    _case_number,
    _new_meta,
    _obligations,
    _row_id,
    _row_id_from_factory,
    _row_meta,
    _source_prefix,
    _with_meta,
    merge_stub_states,
)
from .model import (
    AnalysisRef,
    ArgumentState,
    BaseSnapshot,
    DisplayVersion,
    FunctionIterationState,
    MergeConflict,
    StubState,
    TestRow,
    WorkingSnapshot,
)
from .transaction import CandidateBundle
from .merge_b import (
    MergeBEngine,
    MergeBResult,
    StubLifecycleResult,
    _case_name,
    _case_number,
    _new_meta,
    _obligations,
    _row_id,
    _row_id_from_factory,
    _row_meta,
    _source_prefix,
    _user_changed,
    _with_meta,
    merge_stub_states,
)
from .model import (
    AnalysisRef,
    ArgumentState,
    BaseSnapshot,
    CellValue,
    DisplayVersion,
    FunctionIterationState,
    MergeConflict,
    RowMeta,
    StubParameter,
    StubState,
    Tombstone,
    WorkingSnapshot,
    canonical_json_bytes,
)
from .versioning import advance_merge_b, canonicalize_timestamp, initial_display_version

Mode = Literal["off", "observe", "enforce"]


# A projection can distinguish a missing row field from an explicit empty
# value. ``None`` is a valid state value, so keep private sentinels for the
# former while rebuilding value-XML streams from merged WORKING rows.
_PROJECTION_MISSING = object()
_PROJECTION_INVALID = object()


class MergeAServiceError(ValueError):
    """Invalid orchestration input or a service-level publication failure."""


class MergeBProjectionError(MergeAServiceError):
    """A value/stub artifact cannot be proven to represent accepted state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuditEvent:
    """One machine-readable Merge A operation report.

    The report is intentionally independent from the persisted state models:
    an operational no-op or an observe run can be reported without creating a
    new accepted revision.
    """

    operation_id: str
    operation: str
    mode: Mode
    module_key: str
    function_key: str
    accepted_revision: int | None
    commit_revision: int | None
    previous_version: DisplayVersion | None
    next_version: DisplayVersion | None
    previous_timestamp: str | None
    acceptance_timestamp: str | None
    semantic_change: bool
    outcome: str
    reason: str
    delta_count: int
    conflict_codes: tuple[str, ...] = ()
    validation_ok: bool | None = None

    def __post_init__(self) -> None:
        if type(self.operation_id) is not str or not self.operation_id:
            raise TypeError("operation_id must be a non-empty string")
        if self.operation not in {"merge_a", "merge_b"}:
            raise ValueError("AuditEvent.operation must be 'merge_a' or 'merge_b'")
        if self.mode not in {"off", "observe", "enforce"}:
            raise ValueError("AuditEvent.mode must be off, observe, or enforce")
        if type(self.module_key) is not str or not self.module_key:
            raise TypeError("module_key must be a non-empty string")
        if type(self.function_key) is not str or not self.function_key:
            raise TypeError("function_key must be a non-empty string")
        if type(self.semantic_change) is not bool:
            raise TypeError("semantic_change must be a boolean")
        if type(self.delta_count) is not int or self.delta_count < 0:
            raise TypeError("delta_count must be a non-negative integer")
        if type(self.conflict_codes) is not tuple or not all(
            type(code) is str and code for code in self.conflict_codes
        ):
            raise TypeError("conflict_codes must be a tuple of non-empty strings")

    @property
    def old_version(self) -> DisplayVersion | None:
        return self.previous_version

    @property
    def new_version(self) -> DisplayVersion | None:
        return self.next_version

    def to_dict(self) -> dict[str, Any]:
        def version(value: DisplayVersion | None) -> str | None:
            return None if value is None else format_display_version(value)

        return {
            "operation_id": self.operation_id,
            "operation": self.operation,
            "mode": self.mode,
            "module_key": self.module_key,
            "function_key": self.function_key,
            "accepted_revision": self.accepted_revision,
            "commit_revision": self.commit_revision,
            "previous_version": version(self.previous_version),
            "next_version": version(self.next_version),
            "previous_timestamp": self.previous_timestamp,
            "acceptance_timestamp": self.acceptance_timestamp,
            "semantic_change": self.semantic_change,
            "outcome": self.outcome,
            "reason": self.reason,
            "delta_count": self.delta_count,
            "conflict_codes": list(self.conflict_codes),
            "validation_ok": self.validation_ok,
        }


@dataclass(frozen=True)
class MergeOutcome:
    """Pure result/report returned by :class:`MergeAService`."""

    candidate: CandidateBundle | None
    semantic_change: bool
    conflicts: tuple[MergeConflict, ...]
    audit_events: tuple[AuditEvent, ...]
    mode: Mode
    committed: bool = False
    accepted: AcceptedBundle | None = None
    reason: str = ""
    metrics: PhaseMetrics | None = None

    @property
    def can_emit(self) -> bool:
        # A service candidate is an inspection/preview object until an
        # enforce-mode transaction has actually accepted it.  In particular,
        # observe, no-op, failed, and post-publication-unknown outcomes must
        # never be handed to a downstream writer as emit-ready state.
        return (
            self.mode == "enforce"
            and self.committed
            and self.accepted is not None
            and self.candidate is not None
            and not self.blocked
        )

    @property
    def blocked(self) -> bool:
        return any(conflict.severity == "blocking" for conflict in self.conflicts)

    @property
    def audit(self) -> tuple[AuditEvent, ...]:
        return self.audit_events

    @property
    def conflict_codes(self) -> tuple[str, ...]:
        return tuple(conflict.code for conflict in self.conflicts)

    @property
    def warnings(self) -> tuple[MergeConflict, ...]:
        """Non-blocking diagnostics carried by this candidate, if any."""

        return tuple(conflict for conflict in self.conflicts if conflict.severity != "blocking")

    @property
    def phase_metrics(self) -> PhaseMetrics | None:
        """Trusted internal service timings, when supplied by the service."""

        return self.metrics


def _operation_id(uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> str:
    """Allocate an operation identity through the caller's deterministic factory.

    Operation IDs are part of the audit surface.  Using the service factory here
    keeps golden/acceptance runs deterministic and prevents the audit record from
    being the one remaining source of process-global randomness.
    """

    return new_persistent_id("op", uuid_factory)


def _value_semantics(snapshot: FunctionSnapshot | None) -> object:
    if snapshot is None:
        return None
    arguments = []
    for argument in snapshot.arguments:
        arguments.append(
            (
                argument.name,
                argument.xml_len,
                argument.fmt,
                argument.direction,
                argument.pointer,
                argument.range,
                argument.output,
                argument.testmethod,
                argument.dimensions,
                argument.range_encoding,
                argument.output_encoding,
                tuple(sorted(argument.extra_attributes.items())),
                argument.internal,
                argument.synthetic_pointer_control,
            )
        )
    root_attributes = tuple(
        sorted(
            (key, value)
            for key, value in snapshot.attributes.items()
            if key not in {"utgen_version", "utgen_updated_at"}
        )
    )
    return (
        snapshot.name,
        snapshot.file,
        snapshot.testmethod,
        root_attributes,
        tuple(arguments),
    )


def _stub_semantics(snapshot: StubSnapshot | None) -> object:
    if snapshot is None:
        return None
    called = []
    for function in snapshot.called_functions:
        function_attributes = {
            key: value
            for key, value in getattr(function, "attributes", {}).items()
            if key not in {"name", "file", "line"}
        }
        called.append(
            (
                function.name,
                function.file,
                function.line,
                function.return_type,
                function.return_name,
                tuple(
                    (
                        argument.type_name,
                        argument.name,
                        tuple(sorted(argument.extra_attributes.items())),
                    )
                    for argument in function.arguments
                ),
                tuple(sorted(function_attributes.items())),
                tuple(sorted(function.return_attributes.items())),
            )
        )
    root_attributes = tuple(
        sorted(
            (key, value)
            for key, value in snapshot.attributes.items()
            if key not in {"utgen_version", "utgen_updated_at"}
        )
    )
    return (snapshot.function_name, snapshot.file, snapshot.line, root_attributes, tuple(called))


def _conflict(
    *,
    code: str,
    module_key: str,
    function_key: str,
    message: str,
    entity_type: str = "orchestration",
    base: object = None,
    user: object = None,
    new: object = None,
) -> MergeConflict:
    return MergeConflict(
        code,
        "blocking",
        entity_type,
        function_key,
        None,
        None,
        None,
        None,
        base,
        user,
        new,
        message,
        (),
    )


def _merge_b_conflict(
    *,
    module_key: str,
    function_key: str,
    code: str,
    message: str,
    entity_type: str = "orchestration",
    row_id: str | None = None,
    case_name: str | None = None,
    argument_name: str | None = None,
    logical_argument_key: str | None = None,
    stub_key: str | None = None,
    field_name: str | None = None,
    base: object = None,
    user: object = None,
    new: object = None,
    allowed_actions: tuple[str, ...] | list[str] | None = None,
    allowed_resolutions: tuple[str, ...] | list[str] | None = None,
) -> MergeConflict:
    """Create a deterministic blocking Merge B orchestration conflict."""

    del module_key
    if logical_argument_key is None:
        logical_argument_key = argument_name if argument_name is not None else field_name
    if allowed_actions is not None:
        resolved_actions = tuple(allowed_actions)
    elif allowed_resolutions is not None:
        resolved_actions = tuple(allowed_resolutions)
    elif code in {"USER_NEW_VALUE_CONFLICT", "DESCRIPTION_CONFLICT"} and entity_type in {
        "value",
        "expected",
        "description",
        "intent",
        "row",
    }:
        resolved_actions = ("keep_user", "keep_new", "cancel")
    else:
        resolved_actions = ()
    return MergeConflict(
        code,
        "blocking",
        entity_type,
        function_key,
        row_id,
        case_name,
        logical_argument_key,
        stub_key,
        base,
        user,
        new,
        message,
        resolved_actions,
    )


def _resolution_context_for_conflict(
    supplied: ResolutionContext | Mapping[str, ResolutionContext] | None,
    conflict: MergeConflict,
    *,
    module_key: str,
    base_snapshot_id: str,
    working_snapshot_id: str,
    new_snapshot_id: str | None,
    revision: int,
) -> ResolutionContext:
    """Select and validate the exact context used for one merge conflict."""

    expected = resolution_context_for(
        module_key,
        base_snapshot_id=base_snapshot_id,
        working_snapshot_id=working_snapshot_id,
        new_snapshot_id=new_snapshot_id or "__missing_new_snapshot__",
        revision=revision,
    )
    context = supplied
    if isinstance(supplied, Mapping):
        key = stable_conflict_key(conflict, module_key)
        context = supplied.get(key)
        if context is None:
            context = supplied.get(conflict.function_key)
        if context is None:
            context = supplied.get("*")
    if context is None:
        if new_snapshot_id is None:
            raise ResolutionConflictError("Merge A resolution requires an explicit NEW snapshot context")
        return expected
    if not isinstance(context, ResolutionContext):
        raise ResolutionValidationError("resolution_context must contain ResolutionContext records")
    checks = (
        ("module_key", context.module_key, module_key),
        ("base_snapshot_id", context.base_snapshot_id, base_snapshot_id),
        ("working_snapshot_id", context.working_snapshot_id, working_snapshot_id),
        ("revision", context.revision, revision),
    )
    if new_snapshot_id is not None:
        checks = (*checks, ("new_snapshot_id", context.new_snapshot_id, new_snapshot_id))
    for name, actual, wanted in checks:
        if actual != wanted:
            raise ResolutionConflictError(
                f"resolution context {name} does not match the merge boundary",
                details={"field": name, "expected": wanted, "actual": actual},
            )
    return context


def _resolution_conflict_code(error: ResolutionError) -> str:
    if isinstance(error, ResolutionValidationError):
        return "RESOLUTION_CONTEXT_MISMATCH"
    text = str(error).lower()
    if "action" in text or "allowed" in text:
        return "RESOLUTION_ACTION_MISMATCH"
    if "payload" in text or "conflict key" in text or "identity" in text:
        return "RESOLUTION_CONTEXT_MISMATCH"
    return "STALE_RESOLUTION"


def _snapshot_function_key(snapshot: BaseSnapshot, fallback: str | None = None) -> str | None:
    """Resolve a function key from a generation snapshot without guessing."""

    for name in ("function_key", "function", "name", "function_name"):
        value = getattr(snapshot, name, None)
        if isinstance(value, str) and value:
            return value
    for row in getattr(snapshot, "rows", ()) or ():
        meta = getattr(row, "meta", None)
        value = getattr(meta, "function_key", None)
        if isinstance(value, str) and value:
            return value
    return fallback


def _coerce_generation_snapshots(
    value: object | None,
    *,
    fallback_function_key: str | None = None,
) -> tuple[dict[str, BaseSnapshot], bool]:
    """Normalize the common single/mapping/sequence NEW snapshot forms."""

    if value is None:
        return {}, False
    if isinstance(value, BaseSnapshot):
        key = _snapshot_function_key(value, fallback_function_key)
        if key is None:
            raise TypeError("function_key is required when NEW has no function identity")
        return {key: value}, False
    if isinstance(value, Mapping):
        result: dict[str, BaseSnapshot] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if not key:
                raise TypeError("NEW snapshot mapping contains an empty function key")
            if isinstance(item, FunctionIterationState):
                item = item.base
            if not isinstance(item, BaseSnapshot):
                raise TypeError("NEW snapshot mapping values must be BaseSnapshot records")
            inferred = _snapshot_function_key(item, key)
            if inferred is not None and inferred != key and _snapshot_function_key(item) is not None:
                raise ValueError(
                    f"NEW snapshot function identity {inferred!r} disagrees with mapping key {key!r}"
                )
            result[key] = item
        return result, True
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("NEW snapshots must be BaseSnapshot records or a function mapping")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("NEW snapshots must be BaseSnapshot records or a function mapping") from exc
    result = {}
    for item in items:
        if isinstance(item, FunctionIterationState):
            item = item.base
        if not isinstance(item, BaseSnapshot):
            raise TypeError("NEW snapshot sequences must contain BaseSnapshot records")
        key = _snapshot_function_key(item)
        if key is None:
            raise TypeError("each NEW snapshot in a sequence needs a function identity")
        if key in result:
            raise ValueError(f"duplicate NEW snapshot for function {key!r}")
        result[key] = item
    return result, True


def _coerce_per_function(value: object | None, function_key: str) -> object | None:
    """Select one function's optional schema/projection value."""

    if isinstance(value, Mapping) and function_key in value:
        return value[function_key]
    return value


def _analysis_id_from(value: object | None) -> str | None:
    if isinstance(value, AnalysisRef):
        return value.analysis_id
    ref = getattr(value, "ref", None)
    if isinstance(ref, AnalysisRef):
        return ref.analysis_id
    candidate = getattr(value, "analysis_id", None)
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


def _published_commit_conflict(
    *,
    code: str,
    module_key: str,
    function_key: str,
    message: str,
) -> MergeConflict:
    """Build a diagnostic for a publication that is already visible.

    A post-pointer transaction error is not a merge conflict in the user
    data, but it must still travel through the existing conflict/reporting
    surface.  Keep it blocking so callers cannot treat the candidate as a
    second emit-ready write while the accepted revision is being reloaded.
    """

    return _conflict(
        code=code,
        module_key=module_key,
        function_key=function_key,
        entity_type="publication",
        message=message,
    )


def _resolution_failure_conflict(
    conflict: MergeConflict,
    code: str,
    message: str,
) -> MergeConflict:
    """Keep the original BASE/USER/NEW payload on a resolution diagnostic."""

    return replace(
        conflict,
        code=code,
        message=f"{message}: {conflict.message}",
    )


STATE_ARTIFACT = re.compile(r"^(?:base|working)/[^/]+/state\.json\Z")
INTERNAL_ARTIFACTS = frozenset(
    {"bundle_manifest.json", "module_manifest.json", "module_stub_state.json", "conflicts.json"}
)


def artifact_slot(record: ArtifactRecord) -> str | None:
    """Return the BASE/WORKING slot encoded by a standard artifact path."""

    first = record.relative_path.split("/", 1)[0]
    return first if first in {"base", "working"} else None


def artifact_matches_function(
    record: ArtifactRecord,
    function_key: str,
    role: str,
    role_counts: Mapping[str, int],
) -> bool:
    """Return whether an XML artifact belongs to one function.

    Standard revisions use function-qualified paths.  The single-record
    fallback remains for legacy/bootstrap repositories, while multi-function
    bundles stay conservative.
    """

    path = record.relative_path
    parts = path.split("/")
    filename = parts[-1]
    stem = filename.rsplit(".", 1)[0]
    if stem == function_key:
        return True
    if path == f"{role}/{function_key}.xml":
        return True
    if function_key in parts[:-1]:
        return True
    if stem.endswith(f"_{function_key}"):
        return True
    return role_counts.get(role, 0) == 1


def artifact_role(record: ArtifactRecord) -> str | None:
    """Classify a candidate artifact as value/stub projection or unrelated."""

    kind = record.kind.lower()
    path = record.relative_path.lower()
    if (
        "value" in kind
        or path.startswith("value/")
        or path.startswith("values/")
        or "/value/" in path
        or "/values/" in path
        or path.endswith("/value.xml")
    ):
        return "value"
    if (
        "stub" in kind
        or path.startswith("stub/")
        or path.startswith("stubs/")
        or "/stub/" in path
        or "/stubs/" in path
        or path.endswith("/stub.xml")
    ):
        return "stub"
    return None


def artifact_is_working_projection(
    record: ArtifactRecord,
    function_key: str,
    role: str,
    role_counts: Mapping[str, int],
) -> bool:
    """Whether an artifact is the mutable target projection for Merge A."""

    if artifact_slot(record) == "base":
        return False
    return artifact_matches_function(record, function_key, role, role_counts)


def validation_conflicts(
    report: ValidationReport,
    *,
    module_key: str,
    function_key: str,
) -> tuple[MergeConflict, ...]:
    """Convert transaction validation issues into stable merge diagnostics."""

    return tuple(
        _conflict(
            code=issue.code,
            module_key=module_key,
            function_key=function_key,
            message=issue.message,
            entity_type="validation",
        )
        for issue in report.issues
    )


def abort_transaction(transaction: Any | None) -> None:
    """Best-effort cleanup for every pre-publication transaction failure."""

    if transaction is None or getattr(transaction, "state", None) == "committed":
        return
    try:
        transaction.abort()
    except Exception:
        # Preserve the original transaction diagnostic.  Cleanup failures
        # must not imply a second acceptance result.
        return


__all__ = [
    "STATE_ARTIFACT",
    "INTERNAL_ARTIFACTS",
    "abort_transaction",
    "artifact_is_working_projection",
    "artifact_matches_function",
    "artifact_role",
    "artifact_slot",
    "validation_conflicts",
]

_INTERNAL_ARTIFACTS = INTERNAL_ARTIFACTS
_STATE_ARTIFACT = STATE_ARTIFACT

class MergeAService:
    """Orchestrate one complete-export Merge A for an accepted module."""

    def __init__(
        self,
        repository: IterationRepository,
        *,
        mode: Mode = "enforce",
        engine: MergeAEngine | None = None,
        clock: Callable[[], str] | None = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        value_adapter: ValueXmlAdapter | None = None,
        stub_adapter: StubXmlAdapter | None = None,
        resolution_registry: Any | None = None,
        resolution_decisions: Mapping[str, ResolutionDecision] | Iterable[ResolutionDecision] | None = None,
        resolution_applier: Callable[[MergeConflict, ResolutionDecision, object | None], object] | None = None,
    ) -> None:
        if not hasattr(repository, "load") or not hasattr(repository, "begin"):
            raise TypeError("repository must implement load() and begin()")
        if mode not in {"off", "observe", "enforce"}:
            raise ValueError("mode must be one of off, observe, enforce")
        if engine is not None and not hasattr(engine, "merge"):
            raise TypeError("engine must implement merge()")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        if resolution_applier is not None and not callable(resolution_applier):
            raise TypeError("resolution_applier must be callable")
        self.repository = repository
        self.mode: Mode = mode
        # Keep row/snapshot allocation deterministic under the same injected
        # factory used by the orchestration IDs.  An explicitly supplied
        # engine remains fully caller-owned and is not reconfigured.
        self.engine = engine or MergeAEngine(uuid_factory=uuid_factory)
        self.clock = clock
        self.uuid_factory = uuid_factory
        self.value_adapter = value_adapter or ValueXmlAdapter()
        self.stub_adapter = stub_adapter or StubXmlAdapter()
        self.resolution_registry = resolution_registry
        self.resolution_decisions = resolution_decisions
        self.resolution_applier = resolution_applier

    def merge(
        self,
        module_key: str,
        workbook: WorkbookSnapshot,
        *,
        function_key: str,
        value_snapshot: FunctionSnapshot | None = None,
        stub_snapshot: StubSnapshot | None = None,
        argument_resolver: Any | None = None,
        inventory: Any | None = None,
        required_arguments: Iterable[str] | None = None,
        required_stubs: Iterable[str] | Mapping[str, Any] | None = None,
        stub_signatures: Any | None = None,
        stub_signature_resolver: Any | None = None,
        whole_struct_bases: Iterable[str] | Mapping[str, bool] | None = None,
        resolution_registry: Any | None = None,
        resolution_decisions: Mapping[str, ResolutionDecision] | Iterable[ResolutionDecision] | None = None,
        resolution_context: ResolutionContext | Mapping[str, ResolutionContext] | None = None,
        resolution_applier: Callable[[MergeConflict, ResolutionDecision, object | None], object] | None = None,
    ) -> MergeOutcome:
        """Run Merge A according to the service's configured rollout mode.

        Inputs are already normalized by PR06/PR07.  The method performs no
        direct filesystem writes; enforce mode delegates the one accepted
        publication to the PR05 repository transaction.
        """

        self._validate_inputs(module_key, workbook, function_key, value_snapshot, stub_snapshot)
        if resolution_applier is not None and not callable(resolution_applier):
            raise TypeError("resolution_applier must be callable")
        effective_resolution_registry = (
            self.resolution_registry if resolution_registry is None else resolution_registry
        )
        effective_resolution_decisions = (
            self.resolution_decisions if resolution_decisions is None else resolution_decisions
        )
        effective_resolution_applier = (
            self.resolution_applier if resolution_applier is None else resolution_applier
        )
        operation_id = _operation_id(self.uuid_factory)
        if self.mode == "off":
            event = AuditEvent(
                operation_id,
                "merge_a",
                "off",
                module_key,
                function_key,
                None,
                None,
                None,
                None,
                None,
                None,
                False,
                "off",
                "iterative Merge A is disabled",
                0,
                (),
                None,
            )
            return MergeOutcome(None, False, (), (event,), "off", False, None, event.reason)

        try:
            accepted = self.repository.load(module_key)
        except Exception as exc:
            conflict = _conflict(
                code="ORCHESTRATION_LOAD_FAILED",
                module_key=module_key,
                function_key=function_key,
                message=f"accepted bundle could not be loaded: {exc}",
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                None,
                (conflict,),
                "load_failed",
                mode=self.mode,
            )

        target = self._target_state(accepted, function_key)
        if target is None:
            conflict = _conflict(
                code="IDENTITY_FUNCTION_MISMATCH",
                module_key=module_key,
                function_key=function_key,
                message=f"accepted module has no function_key {function_key!r}",
                base=tuple(state.function_key for state in accepted.function_states),
                user=function_key,
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "function_not_found",
                mode=self.mode,
            )

        # XML projections are mirrors of accepted WORKING, not an independent
        # user-editable Merge A input.  Capture the WORKING-first baselines
        # once so every later fallback/semantic comparison uses the same
        # accepted truth.  Explicit projections remain useful to the PR09
        # engine, but only after this consistency check passes.
        accepted_value_snapshot = self._accepted_value_snapshot(accepted, function_key)
        accepted_stub_snapshot = self._accepted_stub_snapshot(accepted, function_key)
        value_projection_present = bool(
            self._accepted_projection_records(accepted, function_key, role="value")
        )
        stub_projection_present = bool(
            self._accepted_projection_records(accepted, function_key, role="stub")
        )

        if value_snapshot is not None and value_snapshot.name != target.function:
            conflict = _conflict(
                code="IDENTITY_FUNCTION_MISMATCH",
                module_key=module_key,
                function_key=function_key,
                message=(
                    f"value snapshot function {value_snapshot.name!r} does not match "
                    f"accepted function {target.function!r}"
                ),
                base=target.function,
                user=value_snapshot.name,
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "function_identity_conflict",
                mode=self.mode,
            )
        if (
            value_snapshot is not None
            and value_snapshot.display_version is not None
            and value_snapshot.display_version != target.display_version
        ):
            conflict = _conflict(
                code="VERSION_METADATA_MISMATCH",
                module_key=module_key,
                function_key=function_key,
                entity_type="value_xml",
                message="value XML display metadata does not match the accepted state",
                base=target.display_version,
                user=value_snapshot.display_version,
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "blocking_conflict",
                mode=self.mode,
            )
        if stub_snapshot is not None and stub_snapshot.function_name != target.function:
            conflict = _conflict(
                code="IDENTITY_FUNCTION_MISMATCH",
                module_key=module_key,
                function_key=function_key,
                message=(
                    f"stub snapshot function {stub_snapshot.function_name!r} does not match "
                    f"accepted function {target.function!r}"
                ),
                base=target.function,
                user=stub_snapshot.function_name,
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "function_identity_conflict",
                mode=self.mode,
            )
        if (
            stub_snapshot is not None
            and stub_snapshot.display_version is not None
            and stub_snapshot.display_version != target.display_version
        ):
            conflict = _conflict(
                code="VERSION_METADATA_MISMATCH",
                module_key=module_key,
                function_key=function_key,
                entity_type="stub_xml",
                message="stub XML display metadata does not match the accepted state",
                base=target.display_version,
                user=stub_snapshot.display_version,
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "blocking_conflict",
                mode=self.mode,
            )

        projection_input_conflicts = self._projection_input_conflicts(
            accepted_value_snapshot,
            accepted_stub_snapshot,
            value_snapshot,
            stub_snapshot,
            value_projection_present=value_projection_present,
            stub_projection_present=stub_projection_present,
            module_key=module_key,
            function_key=function_key,
        )
        if projection_input_conflicts:
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                projection_input_conflicts,
                "blocking_conflict",
                mode=self.mode,
            )

        # A supplied projection is an input consistency check only.  Once an
        # accepted WORKING projection exists, pass that immutable baseline to
        # the engine and use it for candidate fallback; this prevents a
        # caller-supplied same-version XML object from becoming a second source
        # of truth.  Explicit projections are allowed as a bootstrap fallback
        # only when the accepted bundle has no matching projection at all.
        selected_value_snapshot = (
            accepted_value_snapshot if value_projection_present else value_snapshot
        )
        selected_stub_snapshot = (
            accepted_stub_snapshot if stub_projection_present else stub_snapshot
        )

        projection_conflicts = self._accepted_projection_metadata_conflicts(
            accepted,
            target,
            module_key=module_key,
            function_key=function_key,
        )
        if projection_conflicts:
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                projection_conflicts,
                "blocking_conflict",
                mode=self.mode,
            )

        try:
            merge_result = self._invoke_engine(
                # The engine contract is pure.  Supplying detached accepted
                # objects also makes that contract enforceable for lightweight
                # integrations that use in-place edits internally: a failed
                # operation can never mutate the loaded accepted state before
                # the transaction boundary is reached.
                deepcopy(target),
                deepcopy(workbook),
                argument_resolver=argument_resolver,
                inventory=inventory,
                required_arguments=required_arguments,
                function_key=function_key,
                source_file=target.source_file,
                value_snapshot=deepcopy(selected_value_snapshot),
                module_stub_state=tuple(deepcopy(item) for item in accepted.module_stub_state),
                required_stubs=required_stubs,
                stub_signatures=stub_signatures,
                stub_signature_resolver=stub_signature_resolver,
                whole_struct_bases=whole_struct_bases,
            )
        except Exception as exc:
            conflict = _conflict(
                code="ORCHESTRATION_MERGE_FAILED",
                module_key=module_key,
                function_key=function_key,
                message=f"Merge A engine failed before acceptance: {exc}",
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "merge_failed",
                mode=self.mode,
            )

        try:
            # Accept the documented MergeAResult as well as a compatible
            # test/integration double exposing the same small attribute set.
            all_conflicts = tuple(merge_result.conflicts)
            delta_count = len(merge_result.delta)
            result_candidate = merge_result.candidate
            if not all(isinstance(conflict, MergeConflict) for conflict in all_conflicts):
                raise TypeError("conflicts must contain MergeConflict records")
        except Exception as exc:
            conflict = _conflict(
                code="ORCHESTRATION_INVALID_RESULT",
                module_key=module_key,
                function_key=function_key,
                message=f"Merge A engine returned an invalid result: {exc}",
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "merge_failed",
                mode=self.mode,
            )

        blocking_conflicts = tuple(
            conflict for conflict in all_conflicts if conflict.severity == "blocking"
        )
        # Merge A consumes a complete TESSY export into WORKING.  It has no
        # independent pure NEW snapshot, so its conflicts are import/schema
        # validation diagnostics rather than BASE/USER/NEW value conflicts.
        # Do not run the generic resolution applier here: in particular it
        # cannot turn the engine's ``candidate=None`` into a production
        # candidate.  Persisted resolutions are therefore an explicit
        # unsupported boundary for Merge A until a real three-way import
        # model exists.  A blocking result remains fail-closed and accepted
        # state is never mutated.
        if blocking_conflicts and any(
            item is not None
            for item in (
                effective_resolution_registry,
                effective_resolution_decisions,
                resolution_context,
                effective_resolution_applier,
            )
        ):
            unsupported = tuple(
                _resolution_failure_conflict(
                    conflict,
                    "RESOLUTION_UNSUPPORTED_MERGE_A",
                    "Merge A has no independent NEW snapshot for persisted USER-vs-NEW resolution",
                )
                for conflict in blocking_conflicts
            )
            all_conflicts = (*all_conflicts, *unsupported)
            blocking_conflicts = tuple(
                conflict for conflict in all_conflicts if conflict.severity == "blocking"
            )
        # A pure engine result with no candidate is never publishable, even if a
        # custom engine forgot to attach a diagnostic conflict.
        if blocking_conflicts or result_candidate is None:
            conflicts = all_conflicts
            if not conflicts:
                conflicts = (
                    _conflict(
                        code="ORCHESTRATION_EMPTY_CANDIDATE",
                        module_key=module_key,
                        function_key=function_key,
                        message="Merge A engine returned no candidate",
                    ),
                )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                conflicts,
                "blocking_conflict",
                mode=self.mode,
                delta_count=delta_count,
            )
        try:
            if result_candidate is merge_result.candidate:
                candidate_state = self._candidate_state(target, merge_result)
            elif isinstance(result_candidate, FunctionIterationState):
                candidate_state = result_candidate
            elif isinstance(result_candidate, WorkingSnapshot):
                candidate_state = replace(target, working=result_candidate)
            else:
                raise MergeAServiceError("resolution returned an invalid state candidate")
            candidate_stubs = tuple(
                getattr(merge_result, "module_stub_state", None)
                if getattr(merge_result, "module_stub_state", None) is not None
                else accepted.module_stub_state
            )
        except Exception as exc:
            conflict = _conflict(
                code="ORCHESTRATION_INVALID_CANDIDATE",
                module_key=module_key,
                function_key=function_key,
                message=f"Merge A engine returned an invalid state candidate: {exc}",
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "merge_failed",
                mode=self.mode,
                delta_count=len(merge_result.delta),
            )
        if not all(isinstance(stub, StubState) for stub in candidate_stubs):
            conflict = _conflict(
                code="ORCHESTRATION_INVALID_CANDIDATE",
                module_key=module_key,
                function_key=function_key,
                message="Merge A engine returned invalid module stub state records",
            )
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                (conflict,),
                "merge_failed",
                mode=self.mode,
                delta_count=len(merge_result.delta),
            )
        # PR09 returns the updated XML projection only for the complex path.
        # Keep the caller's accepted projection as a fallback for scalar-only
        # engines so the service can still mirror version metadata on commit.
        # With no accepted projection, an explicit snapshot is the documented
        # legacy/bootstrap baseline.  It is still a caller-supplied input, but
        # there is no WORKING mirror to contradict it yet; comparing against
        # ``None`` would turn an unchanged bootstrap into a false semantic
        # change and consume a version/transaction unnecessarily.
        old_value = selected_value_snapshot
        candidate_value = getattr(merge_result, "value_snapshot", None)
        if candidate_value is None:
            # Scalar engines and state-only Merge A calls legitimately omit a
            # value projection.  Treat that omission as "unchanged" when the
            # accepted bundle has a compatible value XML, otherwise every
            # scalar no-op would look like a value deletion.
            candidate_value = selected_value_snapshot

        projection_conflicts: list[MergeConflict] = []
        if candidate_value is not None and not isinstance(candidate_value, FunctionSnapshot):
            projection_conflicts.append(
                _conflict(
                    code="ORCHESTRATION_INVALID_CANDIDATE",
                    module_key=module_key,
                    function_key=function_key,
                    entity_type="value_xml",
                    message="Merge A engine returned an invalid value XML projection",
                )
            )
        elif candidate_value is not None and candidate_value.name != target.function:
            projection_conflicts.append(
                _conflict(
                    code="IDENTITY_FUNCTION_MISMATCH",
                    module_key=module_key,
                    function_key=function_key,
                    entity_type="value_xml",
                    message=(
                        f"candidate value snapshot function {candidate_value.name!r} does not "
                        f"match accepted function {target.function!r}"
                    ),
                    base=target.function,
                    user=candidate_value.name,
                )
            )
        if (
            isinstance(candidate_value, FunctionSnapshot)
            and candidate_value.display_version is not None
            and candidate_value.display_version != target.display_version
        ):
            projection_conflicts.append(
                _conflict(
                    code="VERSION_METADATA_MISMATCH",
                    module_key=module_key,
                    function_key=function_key,
                    entity_type="value_xml",
                    message="candidate value XML display metadata does not match accepted state",
                    base=target.display_version,
                    user=candidate_value.display_version,
                )
            )
        if projection_conflicts:
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                tuple(projection_conflicts),
                "blocking_conflict",
                mode=self.mode,
                delta_count=len(merge_result.delta),
            )

        invariant_conflicts = self._candidate_invariant_conflicts(
            target,
            candidate_state,
            module_key=module_key,
            function_key=function_key,
        )
        if invariant_conflicts:
            return self._failure_outcome(
                operation_id,
                module_key,
                function_key,
                accepted,
                invariant_conflicts,
                "blocking_conflict",
                mode=self.mode,
                delta_count=len(merge_result.delta),
            )
        semantic_change = self._semantic_change(
            target,
            candidate_state,
            accepted.module_stub_state,
            candidate_stubs,
            old_value,
            candidate_value,
            selected_stub_snapshot,
            selected_stub_snapshot,
        )

        # Warnings are data, not blockers.  They travel with an accepted
        # candidate and remain visible in the returned outcome/audit report.
        warnings = tuple(
            conflict for conflict in all_conflicts if conflict.severity != "blocking"
        )

        if not semantic_change:
            try:
                candidate = self._build_candidate_bundle(
                    accepted,
                    target,
                    candidate_state,
                    candidate_stubs,
                    selected_value_snapshot,
                    candidate_value,
                    selected_stub_snapshot,
                    target.display_version,
                    working_snapshot_id=None,
                    audit=None,
                    conflicts=warnings,
                    operation_id=operation_id,
                )
            except Exception as exc:
                conflict = _conflict(
                    code="ORCHESTRATION_CANDIDATE_BUILD_FAILED",
                    module_key=module_key,
                    function_key=function_key,
                    message=f"no-op candidate could not be assembled: {exc}",
                )
                return self._failure_outcome(
                    operation_id,
                    module_key,
                    function_key,
                    accepted,
                    (conflict,),
                    "merge_failed",
                    mode=self.mode,
                    delta_count=len(merge_result.delta),
                )
            event = AuditEvent(
                operation_id,
                "merge_a",
                self.mode,
                module_key,
                function_key,
                accepted.revision,
                None,
                target.display_version,
                target.display_version,
                target.display_version.updated_at,
                target.display_version.updated_at,
                False,
                "noop",
                "normalized workbook has no semantic change",
                len(merge_result.delta),
                tuple(conflict.code for conflict in warnings),
                None,
            )
            return MergeOutcome(
                candidate,
                False,
                warnings,
                (event,),
                self.mode,
                False,
                accepted,
                event.reason,
            )

        if self.mode == "observe":
            try:
                candidate = self._build_candidate_bundle(
                    accepted,
                    target,
                    candidate_state,
                    candidate_stubs,
                    selected_value_snapshot,
                    candidate_value,
                    selected_stub_snapshot,
                    target.display_version,
                    working_snapshot_id=None,
                    audit=None,
                    conflicts=warnings,
                    operation_id=operation_id,
                )
            except Exception as exc:
                conflict = _conflict(
                    code="ORCHESTRATION_CANDIDATE_BUILD_FAILED",
                    module_key=module_key,
                    function_key=function_key,
                    message=f"observe candidate could not be assembled: {exc}",
                )
                return self._failure_outcome(
                    operation_id,
                    module_key,
                    function_key,
                    accepted,
                    (conflict,),
                    "merge_failed",
                    mode="observe",
                    delta_count=len(merge_result.delta),
                )
            event = AuditEvent(
                operation_id,
                "merge_a",
                "observe",
                module_key,
                function_key,
                accepted.revision,
                None,
                target.display_version,
                None,
                target.display_version.updated_at,
                None,
                True,
                "observed",
                "semantic candidate built; observe mode does not accept writes",
                len(merge_result.delta),
                tuple(conflict.code for conflict in warnings),
                None,
            )
            return MergeOutcome(
                candidate,
                True,
                warnings,
                (event,),
                "observe",
                False,
                accepted,
                event.reason,
            )

        # Enforce acquires the revision/CAS lock before reading the clock.  A
        # stale caller therefore cannot consume a visible acceptance time.
        transaction = None
        candidate: CandidateBundle | None = None
        audit: AuditEvent | None = None
        try:
            transaction = self.repository.begin(module_key, accepted.revision)
            next_version = advance_merge_a(
                target.display_version,
                semantic_change=True,
                clock=self.clock,
            )
            new_working_id = new_persistent_id("snap", self.uuid_factory)
            audit = AuditEvent(
                operation_id,
                "merge_a",
                "enforce",
                module_key,
                function_key,
                accepted.revision,
                accepted.revision + 1,
                target.display_version,
                next_version,
                target.display_version.updated_at,
                next_version.updated_at,
                True,
                "accepted",
                "semantic Merge A candidate accepted",
                len(merge_result.delta),
                tuple(conflict.code for conflict in warnings),
                True,
            )
            candidate = self._build_candidate_bundle(
                accepted,
                target,
                candidate_state,
                candidate_stubs,
                selected_value_snapshot,
                candidate_value,
                selected_stub_snapshot,
                next_version,
                working_snapshot_id=new_working_id,
                audit=audit,
                conflicts=warnings,
                operation_id=operation_id,
            )
            transaction.stage(candidate)
            validation = transaction.validate()
            if not validation.ok:
                transaction.abort()
                conflicts = self._validation_conflicts(
                    validation,
                    module_key=module_key,
                    function_key=function_key,
                )
                failed_event = replace(
                    audit,
                    commit_revision=None,
                    next_version=None,
                    acceptance_timestamp=None,
                    outcome="failed",
                    reason="candidate validation failed",
                    conflict_codes=tuple(conflict.code for conflict in conflicts),
                    validation_ok=False,
                )
                return MergeOutcome(None, True, conflicts, (failed_event,), "enforce", False, accepted, failed_event.reason)
            accepted_after = transaction.commit()
            if accepted_after is None:
                # The protocol returns an AcceptedBundle, but a small adapter
                # or test double may expose a commit operation with no return;
                # reload through the repository before reporting acceptance.
                accepted_after = self.repository.load(module_key)
            committed_event = replace(
                audit,
                commit_revision=accepted_after.revision,
                outcome="accepted",
                validation_ok=True,
            )
            return MergeOutcome(
                candidate,
                True,
                warnings,
                (committed_event,),
                "enforce",
                True,
                accepted_after,
                committed_event.reason,
            )
        except ConcurrentStateUpdate as exc:
            self._abort_transaction(transaction)
            conflict = _conflict(
                code="CONCURRENT_STATE_UPDATE",
                module_key=module_key,
                function_key=function_key,
                message=str(exc),
            )
            event = AuditEvent(
                operation_id,
                "merge_a",
                "enforce",
                module_key,
                function_key,
                accepted.revision,
                None,
                target.display_version,
                None,
                target.display_version.updated_at,
                None,
                True,
                "conflict",
                "repository compare-and-swap rejected the candidate",
                len(merge_result.delta),
                (conflict.code,),
                None,
            )
            return MergeOutcome(None, True, (conflict,), (event,), "enforce", False, accepted, event.reason)
        except Exception as exc:
            # PR05 deliberately distinguishes a publication whose pointer is
            # already visible from a failure before publication.  Do not turn
            # a post-pointer durability/load diagnostic into a false failed
            # report (or call abort on an already committed transaction).
            error_code = getattr(exc, "code", None)
            pointer_published = getattr(transaction, "state", None) == "committed"
            published_error = isinstance(
                exc,
                (PublishedCommitDurabilityError, PublishedCommitLoadError),
            ) or error_code in {
                PublishedCommitDurabilityError.code,
                PublishedCommitLoadError.code,
            } or pointer_published
            if (
                transaction is not None
                and (
                    getattr(transaction, "state", None) == "committed"
                    or published_error
                )
                and candidate is not None
                and audit is not None
            ):
                try:
                    accepted_after = self.repository.load(module_key)
                except Exception:
                    # The pointer is visible but cannot be reloaded.  Preserve
                    # the transaction's diagnostic code and report the state
                    # as unknown rather than attempting another write.
                    accepted_after = None
                # A broken adapter may return the pre-commit object after a
                # post-pointer exception.  Never expose that stale object as
                # the accepted result: publication has already moved the
                # pointer, so the only safe values are the newly reloaded
                # bundle or ``None`` (unknown until a later explicit reload).
                if accepted_after is not None and (
                    getattr(accepted_after, "revision", None) != accepted.revision + 1
                    or getattr(accepted_after, "revision_id", None) == accepted.revision_id
                ):
                    accepted_after = None
                if accepted_after is not None:
                    published_conflict = _published_commit_conflict(
                        code=error_code
                        if type(error_code) is str and error_code
                        else PublishedCommitDurabilityError.code,
                        module_key=module_key,
                        function_key=function_key,
                        message=(
                            "repository pointer was published; accepted revision was "
                            "reloaded after a post-publication transaction diagnostic"
                        ),
                    )
                    committed_event = replace(
                        audit,
                        commit_revision=accepted_after.revision,
                        outcome="published",
                        reason="candidate accepted; post-publication transaction diagnostic was reloaded",
                        conflict_codes=(published_conflict.code,),
                        validation_ok=True,
                    )
                    return MergeOutcome(
                        candidate,
                        True,
                        (*warnings, published_conflict),
                        (committed_event,),
                        "enforce",
                        True,
                        accepted_after,
                        committed_event.reason,
                    )
            # PublishedCommit* is terminal after the pointer swap even when a
            # lightweight transaction double does not expose ``state``.  An
            # abort here could make a caller believe the visible revision was
            # rolled back, so only abort ordinary pre-publication failures.
            if not published_error:
                self._abort_transaction(transaction)
            if type(error_code) is not str or not error_code:
                error_code = (
                    PublishedCommitDurabilityError.code
                    if published_error
                    else "ORCHESTRATION_COMMIT_FAILED"
                )
            if published_error and transaction is not None and candidate is not None and audit is not None:
                published_conflict = _published_commit_conflict(
                    code=error_code,
                    module_key=module_key,
                    function_key=function_key,
                    message=(
                        "repository pointer was published, but the accepted revision could not "
                        "be reloaded; accepted state is unknown and requires explicit reload"
                    ),
                )
                published_event = replace(
                    audit,
                    commit_revision=accepted.revision + 1,
                    outcome="published",
                    reason="candidate accepted; post-publication reload is required",
                    conflict_codes=(published_conflict.code,),
                    validation_ok=True,
                )
                return MergeOutcome(
                    candidate,
                    True,
                    (*warnings, published_conflict),
                    (published_event,),
                    "enforce",
                    True,
                    None,
                    published_event.reason,
                )
            conflict = _conflict(
                code=error_code,
                module_key=module_key,
                function_key=function_key,
                message=f"candidate was not accepted: {exc}",
            )
            event = AuditEvent(
                operation_id,
                "merge_a",
                "enforce",
                module_key,
                function_key,
                accepted.revision,
                None,
                target.display_version,
                None,
                target.display_version.updated_at,
                None,
                True,
                "failed",
                "repository transaction failed before acceptance",
                len(merge_result.delta),
                (conflict.code,),
                None,
            )
            return MergeOutcome(None, True, (conflict,), (event,), "enforce", False, accepted, event.reason)

    @staticmethod
    def _abort_transaction(transaction: Any | None) -> None:
        abort_transaction(transaction)

    def _invoke_engine(
        self,
        target: FunctionIterationState,
        workbook: WorkbookSnapshot,
        **kwargs: Any,
    ) -> MergeAResult:
        """Call an engine while retaining the small PR10 engine protocol.

        The production PR09 engine accepts the richer keyword context, whereas
        contract tests and integrations are allowed to provide a minimal
        ``merge(accepted, workbook)`` implementation.  Filter optional
        keywords only when the callable does not advertise ``**kwargs``; this
        avoids masking a real engine failure with a signature-related error.
        """

        merge_method = self.engine.merge
        try:
            signature = inspect.signature(merge_method)
        except (TypeError, ValueError):
            return merge_method(target, workbook, **kwargs)
        parameters = signature.parameters.values()
        accepts_keywords = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if accepts_keywords:
            return merge_method(target, workbook, **kwargs)
        accepted_names = {
            parameter.name
            for parameter in parameters
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        }
        filtered = {name: value for name, value in kwargs.items() if name in accepted_names}
        return merge_method(target, workbook, **filtered)

    def _accepted_value_snapshot(
        self,
        accepted: AcceptedBundle,
        function_key: str,
    ) -> FunctionSnapshot | None:
        """Best-effort decode of the accepted target value projection.

        State-only scalar Merge A calls often do not pass a value snapshot to
        the engine.  Reading the accepted projection lets semantic no-op
        detection remain correct when a custom engine returns a new projection,
        and lets the commit path update only the root version metadata.  A
        malformed accepted artifact is left for the transaction's validator to
        report; this helper must not make an unrelated artifact load fatal.
        """

        return self._accepted_projection_snapshot(
            accepted,
            function_key,
            role="value",
            reader=self.value_adapter.read,
        )

    def _accepted_stub_snapshot(
        self,
        accepted: AcceptedBundle,
        function_key: str,
    ) -> StubSnapshot | None:
        """Best-effort decode of the accepted WORKING stub projection."""

        return self._accepted_projection_snapshot(
            accepted,
            function_key,
            role="stub",
            reader=self.stub_adapter.read,
        )

    def _accepted_projection_records(
        self,
        accepted: AcceptedBundle,
        function_key: str,
        *,
        role: str,
    ) -> tuple[ArtifactRecord, ...]:
        """Return target projection records in WORKING-first slot order.

        Internal module state is not an XML projection even though its kind
        contains the word ``stub``.  A matching WORKING path is authoritative;
        when it exists, malformed content must not cause a silent BASE
        fallback.  BASE is used only as a legacy/bootstrap fallback when no
        WORKING or unscoped projection exists.
        """

        role_counts = {
            "value": sum(
                self._artifact_role(item) == "value"
                and item.relative_path not in _INTERNAL_ARTIFACTS
                and _STATE_ARTIFACT.fullmatch(item.relative_path) is None
                for item in accepted.artifacts
            ),
            "stub": sum(
                self._artifact_role(item) == "stub"
                and item.relative_path not in _INTERNAL_ARTIFACTS
                and _STATE_ARTIFACT.fullmatch(item.relative_path) is None
                for item in accepted.artifacts
            ),
        }
        for slot in ("working", None, "base"):
            candidates = tuple(
                record
                for record in accepted.artifacts
                if record.relative_path not in _INTERNAL_ARTIFACTS
                and _STATE_ARTIFACT.fullmatch(record.relative_path) is None
                and self._artifact_role(record) == role
                and self._artifact_slot(record) == slot
                and self._artifact_matches_function(record, function_key, role, role_counts)
            )
            if candidates:
                return candidates
        return ()

    def _accepted_projection_snapshot(
        self,
        accepted: AcceptedBundle,
        function_key: str,
        *,
        role: str,
        reader: Callable[[bytes], Any],
    ) -> Any | None:
        records = self._accepted_projection_records(accepted, function_key, role=role)
        if not records:
            return None
        for record in records:
            try:
                snapshot = reader(record.data)
            except Exception:
                continue
            identity = snapshot.name if role == "value" else snapshot.function_name
            if identity:
                return snapshot
        return None

    @staticmethod
    def _projection_input_conflicts(
        accepted_value: FunctionSnapshot | None,
        accepted_stub: StubSnapshot | None,
        supplied_value: FunctionSnapshot | None,
        supplied_stub: StubSnapshot | None,
        *,
        value_projection_present: bool,
        stub_projection_present: bool,
        module_key: str,
        function_key: str,
    ) -> tuple[MergeConflict, ...]:
        """Reject supplied XML that diverges from accepted WORKING truth.

        Merge A imports the normalized workbook.  A caller-supplied XML object
        is therefore only a projection consistency check; treating it as the
        old baseline would allow same-version semantic edits (for example an
        extra root attribute or called function) to disappear into a no-op.
        Explicit XML remains a valid bootstrap input only when no matching
        accepted projection exists.
        """

        conflicts: list[MergeConflict] = []
        if supplied_value is not None and value_projection_present:
            accepted_semantics = _value_semantics(accepted_value)
            supplied_semantics = _value_semantics(supplied_value)
            if accepted_value is None or accepted_semantics != supplied_semantics:
                conflicts.append(
                    _conflict(
                        code="VALUE_XML_WORKING_MISMATCH",
                        module_key=module_key,
                        function_key=function_key,
                        entity_type="value_xml",
                        base=accepted_semantics,
                        user=supplied_semantics,
                        message=(
                            "supplied value XML differs semantically from the "
                            "accepted WORKING projection"
                        ),
                    )
                )
        if supplied_stub is not None and stub_projection_present:
            accepted_semantics = _stub_semantics(accepted_stub)
            supplied_semantics = _stub_semantics(supplied_stub)
            if accepted_stub is None or accepted_semantics != supplied_semantics:
                conflicts.append(
                    _conflict(
                        code="STUB_XML_WORKING_MISMATCH",
                        module_key=module_key,
                        function_key=function_key,
                        entity_type="stub_xml",
                        base=accepted_semantics,
                        user=supplied_semantics,
                        message=(
                            "supplied stub XML differs semantically from the "
                            "accepted WORKING projection"
                        ),
                    )
                )
        return tuple(conflicts)

    def _accepted_projection_metadata_conflicts(
        self,
        accepted: AcceptedBundle,
        target: FunctionIterationState,
        *,
        module_key: str,
        function_key: str,
    ) -> tuple[MergeConflict, ...]:
        """Check known XML mirrors before constructing a new candidate.

        Legacy projections may legitimately omit the PR06 metadata during an
        explicit bootstrap/migration.  A projection that *does* carry metadata,
        however, must agree with the accepted state or Merge A would be unable
        to prove a coherent four-plane bundle.
        """

        role_counts: dict[str, int] = {"value": 0, "stub": 0}
        for record in accepted.artifacts:
            role = self._artifact_role(record)
            if role in role_counts:
                role_counts[role] += 1
        conflicts: list[MergeConflict] = []
        for record in accepted.artifacts:
            role = self._artifact_role(record)
            # BASE projections are immutable source history.  Their bytes and
            # display metadata intentionally remain at the accepted BASE
            # version after a Merge A, so only WORKING (or legacy unscoped)
            # projections participate in current accepted-mirror checks.
            if self._artifact_slot(record) == "base":
                continue
            if role not in role_counts or not self._artifact_matches_function(
                record,
                function_key,
                role,
                role_counts,
            ):
                continue
            try:
                snapshot = (
                    self.value_adapter.read(record.data)
                    if role == "value"
                    else self.stub_adapter.read(record.data)
                )
            except Exception:
                # The transaction's adapter validator owns malformed-artifact
                # diagnostics.  Do not turn a lightweight fake repository or a
                # legacy migration artifact into a service-level crash here.
                continue
            identity = snapshot.name if role == "value" else snapshot.function_name
            if identity != target.function:
                conflicts.append(
                    _conflict(
                        code="IDENTITY_FUNCTION_MISMATCH",
                        module_key=module_key,
                        function_key=function_key,
                        entity_type=f"{role}_xml",
                        message=(
                            f"accepted {role} XML function {identity!r} does not match "
                            f"accepted function {target.function!r}"
                        ),
                        base=target.function,
                        user=identity,
                    )
                )
            metadata = getattr(snapshot, "display_version", None)
            if metadata is not None and metadata != target.display_version:
                conflicts.append(
                    _conflict(
                        code="VERSION_METADATA_MISMATCH",
                        module_key=module_key,
                        function_key=function_key,
                        entity_type=f"{role}_xml",
                        message=(
                            f"accepted {role} XML display metadata does not match "
                            "the accepted state"
                        ),
                        base=target.display_version,
                        user=metadata,
                    )
                )
        return tuple(conflicts)

    @staticmethod
    def _candidate_invariant_conflicts(
        target: FunctionIterationState,
        candidate: FunctionIterationState,
        *,
        module_key: str,
        function_key: str,
    ) -> tuple[MergeConflict, ...]:
        """Reject a Merge A candidate that changes source-owned BASE identity."""

        checks = (
            (
                candidate.function_key != target.function_key,
                "IDENTITY_FUNCTION_MISMATCH",
                "Merge A must keep the target function key unchanged",
            ),
            (
                candidate.function != target.function,
                "IDENTITY_FUNCTION_MISMATCH",
                "Merge A must keep the target function name unchanged",
            ),
            (
                candidate.source_file != target.source_file,
                "IDENTITY_SOURCE_FILE_MISMATCH",
                "Merge A must keep the target source identity unchanged",
            ),
            (
                candidate.base != target.base,
                "MERGE_BASE_MUTATION",
                "Merge A must leave BASE unchanged",
            ),
            (
                candidate.accepted_analysis_id != target.accepted_analysis_id,
                "ANALYSIS_ID_MUTATION",
                "Merge A must leave the accepted analysis ID unchanged",
            ),
            (
                candidate.working.analysis_id != target.working.analysis_id,
                "ANALYSIS_ID_MUTATION",
                "Merge A WORKING must remain bound to the accepted analysis ID",
            ),
            (
                candidate.working.generation != target.working.generation,
                "MERGE_GENERATION_MUTATION",
                "Merge A must not change the accepted generation",
            ),
        )
        return tuple(
            _conflict(
                code=code,
                module_key=module_key,
                function_key=function_key,
                entity_type="state",
                message=message,
            )
            for changed, code, message in checks
            if changed
        )

    @staticmethod
    def _validate_inputs(
        module_key: str,
        workbook: WorkbookSnapshot,
        function_key: str,
        value_snapshot: FunctionSnapshot | None,
        stub_snapshot: StubSnapshot | None,
    ) -> None:
        if type(module_key) is not str or not module_key:
            raise TypeError("module_key must be a non-empty string")
        if not isinstance(workbook, WorkbookSnapshot):
            raise TypeError("workbook must be a normalized WorkbookSnapshot")
        if type(function_key) is not str or not function_key:
            raise TypeError("function_key must be a non-empty string")
        if value_snapshot is not None and not isinstance(value_snapshot, FunctionSnapshot):
            raise TypeError("value_snapshot must be a parsed FunctionSnapshot")
        if stub_snapshot is not None and not isinstance(stub_snapshot, StubSnapshot):
            raise TypeError("stub_snapshot must be a parsed StubSnapshot")

    @staticmethod
    def _target_state(accepted: AcceptedBundle, function_key: str) -> FunctionIterationState | None:
        matches = [state for state in accepted.function_states if state.function_key == function_key]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _candidate_state(target: FunctionIterationState, result: MergeAResult) -> FunctionIterationState:
        if isinstance(result.candidate, FunctionIterationState):
            return result.candidate
        if isinstance(result.candidate, WorkingSnapshot):
            return replace(target, working=result.candidate)
        raise MergeAServiceError("Merge A result did not contain a state candidate")

    @staticmethod
    def _semantic_change(
        target: FunctionIterationState,
        candidate_state: FunctionIterationState,
        old_stubs: tuple[StubState, ...],
        candidate_stubs: tuple[StubState, ...],
        old_value: FunctionSnapshot | None,
        candidate_value: FunctionSnapshot | None,
        old_stub: StubSnapshot | None = None,
        candidate_stub: StubSnapshot | None = None,
    ) -> bool:
        # Working snapshot IDs are persistence identities, not semantic data.
        state_changed = replace(candidate_state.working, snapshot_id=target.working.snapshot_id) != target.working
        stubs_changed = tuple(candidate_stubs) != tuple(old_stubs)
        value_changed = _value_semantics(candidate_value) != _value_semantics(old_value)
        stub_projection_changed = _stub_semantics(candidate_stub) != _stub_semantics(old_stub)
        return state_changed or stubs_changed or value_changed or stub_projection_changed

    def _build_candidate_bundle(
        self,
        accepted: AcceptedBundle,
        target: FunctionIterationState,
        candidate_state: FunctionIterationState,
        candidate_stubs: tuple[StubState, ...],
        old_value: FunctionSnapshot | None,
        candidate_value: FunctionSnapshot | None,
        stub_snapshot: StubSnapshot | None,
        display_version: DisplayVersion,
        *,
        working_snapshot_id: str | None,
        audit: AuditEvent | None,
        conflicts: tuple[MergeConflict, ...] = (),
        operation_id: str | None = None,
    ) -> CandidateBundle:
        if working_snapshot_id is None:
            working_snapshot_id = target.working.snapshot_id
        function_states: list[FunctionIterationState] = []
        for state in accepted.function_states:
            if state.function_key == target.function_key:
                updated = replace(
                    candidate_state,
                    display_version=display_version,
                    working=replace(candidate_state.working, snapshot_id=working_snapshot_id),
                )
            else:
                updated = replace(
                    state,
                    working=replace(state.working, snapshot_id=working_snapshot_id),
                )
            function_states.append(updated)

        versions = dict(accepted.module_manifest.function_versions)
        versions[target.function] = display_version
        module_manifest = replace(
            accepted.module_manifest,
            working_snapshot_id=working_snapshot_id,
            function_versions=versions,
        )

        artifacts = self._candidate_artifacts(
            accepted.artifacts,
            old_value,
            candidate_value,
            stub_snapshot,
            display_version,
            audit,
            target.function_key,
            function_count=len(accepted.function_states),
        )
        return CandidateBundle(
            accepted.module_key,
            module_manifest,
            tuple(function_states),
            tuple(artifacts),
            tuple(candidate_stubs),
            tuple(conflicts),
            operation_id,
        )

    def _candidate_artifacts(
        self,
        accepted: tuple[ArtifactRecord, ...],
        old_value: FunctionSnapshot | None,
        candidate_value: FunctionSnapshot | None,
        stub_snapshot: StubSnapshot | None,
        display_version: DisplayVersion,
        audit: AuditEvent | None,
        function_key: str,
        *,
        function_count: int,
    ) -> list[ArtifactRecord]:
        # A module may contain projections for several functions.  Merge A is
        # scoped to one Test Object, so never feed the target snapshot into an
        # unrelated function's XML record merely because it has the same role.
        role_counts: dict[str, int] = {"value": 0, "stub": 0}
        for record in accepted:
            role = self._artifact_role(record)
            if role in role_counts:
                role_counts[role] += 1

        records: list[ArtifactRecord] = []
        value_seen = False
        stub_seen = False
        for record in accepted:
            path = record.relative_path
            if path in _INTERNAL_ARTIFACTS or _STATE_ARTIFACT.fullmatch(path):
                continue
            if path == "audit/merge_a.json":
                continue
            role = self._artifact_role(record)
            target_record = (
                role in {"value", "stub"}
                and self._artifact_is_working_projection(record, function_key, role, role_counts)
            )
            if role == "value" and target_record:
                if candidate_value is not None:
                    data = self.value_adapter.render(
                        candidate_value,
                        display_version=display_version,
                    )
                elif audit is not None:
                    # State-only Merge A changes still need the accepted
                    # value XML root metadata to advance with state/manifest.
                    parsed_value = self.value_adapter.read(record.data)
                    data = self.value_adapter.render(
                        parsed_value,
                        display_version=display_version,
                    )
                else:
                    data = record.data
                records.append(ArtifactRecord(path, record.kind, data, self._value_validator))
                value_seen = True
            elif role == "value":
                # The repository reloads artifacts without in-memory
                # validators.  Reattach the adapter validator even when the
                # semantic value XML is unchanged.
                records.append(ArtifactRecord(path, record.kind, record.data, self._value_validator))
            elif role == "stub" and target_record:
                if stub_snapshot is not None:
                    data = self.stub_adapter.render(
                        stub_snapshot,
                        display_version=display_version,
                    )
                elif audit is not None:
                    # A valid accepted StubSnapshot can be re-read to update
                    # only its root metadata.  Legacy bytes require the
                    # caller to cross the explicit PR06 migration boundary.
                    parsed_stub = self.stub_adapter.read(record.data)
                    data = self.stub_adapter.render(
                        parsed_stub,
                        display_version=display_version,
                    )
                else:
                    data = record.data
                records.append(ArtifactRecord(path, record.kind, data, self._stub_validator))
                stub_seen = True
            elif role == "stub":
                records.append(ArtifactRecord(path, record.kind, record.data, self._stub_validator))
            else:
                records.append(record)

        if candidate_value is not None and not value_seen:
            value_path = f"value/{function_key}.xml"
            for record in accepted:
                if (
                    self._artifact_slot(record) == "base"
                    and self._artifact_role(record) == "value"
                    and self._artifact_matches_function(record, function_key, "value", role_counts)
                ):
                    # Preserve the standard filename and mirror a BASE
                    # projection into WORKING if a legacy/migrated revision
                    # has not created the latter yet.
                    value_path = "working/" + record.relative_path[len("base/") :]
                    break
            records.append(
                ArtifactRecord(
                    value_path,
                    "value_xml",
                    self.value_adapter.render(candidate_value, display_version=display_version),
                    self._value_validator,
                )
            )
        if stub_snapshot is not None and not stub_seen:
            stub_path = f"stub/{function_key}.xml"
            for record in accepted:
                if (
                    self._artifact_slot(record) == "base"
                    and self._artifact_role(record) == "stub"
                    and self._artifact_matches_function(record, function_key, "stub", role_counts)
                ):
                    stub_path = "working/" + record.relative_path[len("base/") :]
                    break
            records.append(
                ArtifactRecord(
                    stub_path,
                    "stub_xml",
                    self.stub_adapter.render(stub_snapshot, display_version=display_version),
                    self._stub_validator,
                )
            )
        if audit is not None:
            records.append(
                ArtifactRecord(
                    "audit/merge_a.json",
                    "audit",
                    canonical_json_bytes({"events": [audit.to_dict()]}),
                )
            )
        return records

    @staticmethod
    def _artifact_slot(record: ArtifactRecord) -> str | None:
        return artifact_slot(record)

    def _artifact_is_working_projection(
        self,
        record: ArtifactRecord,
        function_key: str,
        role: str,
        role_counts: Mapping[str, int],
    ) -> bool:
        return artifact_is_working_projection(record, function_key, role, role_counts)

    @staticmethod
    def _artifact_matches_function(
        record: ArtifactRecord,
        function_key: str,
        role: str,
        role_counts: Mapping[str, int],
    ) -> bool:
        return artifact_matches_function(record, function_key, role, role_counts)

    @staticmethod
    def _artifact_role(record: ArtifactRecord) -> str | None:
        return artifact_role(record)

    def _value_validator(self, data: bytes) -> None:
        self.value_adapter.read(data)

    def _stub_validator(self, data: bytes) -> None:
        self.stub_adapter.read(data)

    @staticmethod
    def _validation_conflicts(
        report: ValidationReport,
        *,
        module_key: str,
        function_key: str,
    ) -> tuple[MergeConflict, ...]:
        return validation_conflicts(report, module_key=module_key, function_key=function_key)

    @staticmethod
    def _failure_outcome(
        operation_id: str,
        module_key: str,
        function_key: str,
        accepted: AcceptedBundle | None,
        conflicts: tuple[MergeConflict, ...],
        reason: str,
        *,
        mode: Mode,
        delta_count: int = 0,
    ) -> MergeOutcome:
        previous = None
        revision = None
        if accepted is not None:
            revision = accepted.revision
            for state in accepted.function_states:
                if state.function_key == function_key:
                    previous = state.display_version
                    break
        event = AuditEvent(
            operation_id,
            "merge_a",
            mode,
            module_key,
            function_key,
            revision,
            None,
            previous,
            None,
            None if previous is None else previous.updated_at,
            None,
            False,
            "failed" if reason.endswith("failed") else "conflict",
            reason,
            delta_count,
            tuple(conflict.code for conflict in conflicts),
            None,
        )
        return MergeOutcome(None, False, conflicts, (event,), mode, False, accepted, reason)


__all__ = ["MergeAService"]

ConflictFactory = Callable[..., MergeConflict]


def stub_signature_tuple(state: StubState) -> tuple[Any, ...]:
    return (
        state.function_name,
        state.return_type,
        state.return_storage_name,
        tuple((item.type_name, item.name) for item in state.parameters),
    )


class MergeBService:
    """High-level exact Merge B orchestration and atomic acceptance.

    ``MergeBEngine`` remains a pure row merge.  This service owns the PR13
    lifecycle boundary: analysis linkage, function/stub lifecycle, version
    metadata, candidate artifact assembly, and repository publication.
    """


    def __init__(
        self,
        repository: IterationRepository,
        *,
        mode: Mode = "enforce",
        engine: MergeBEngine | None = None,
        clock: Callable[[], str] | None = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        value_adapter: ValueXmlAdapter | None = None,
        stub_adapter: StubXmlAdapter | None = None,
        analysis_store: Any | None = None,
        body_validator: Callable[..., bool] | None = None,
        metrics: PhaseMetrics | None = None,
        resolution_registry: Any | None = None,
        resolution_decisions: Mapping[str, ResolutionDecision] | Iterable[ResolutionDecision] | None = None,
        resolution_applier: Callable[[MergeConflict, ResolutionDecision, object | None], object] | None = None,
    ) -> None:
        if not hasattr(repository, "load") or not hasattr(repository, "begin"):
            raise TypeError("repository must implement load() and begin()")
        if mode not in {"off", "observe", "enforce"}:
            raise ValueError("mode must be one of off, observe, enforce")
        if engine is not None and not hasattr(engine, "merge"):
            raise TypeError("engine must implement merge()")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        if body_validator is not None and not callable(body_validator):
            raise TypeError("body_validator must be callable")
        if metrics is not None and not isinstance(metrics, PhaseMetrics):
            raise TypeError("metrics must be a PhaseMetrics instance")
        if resolution_applier is not None and not callable(resolution_applier):
            raise TypeError("resolution_applier must be callable")
        self.repository = repository
        self.mode: Mode = mode
        self.engine = engine or MergeBEngine(uuid_factory=uuid_factory)
        self.clock = clock
        self.uuid_factory = uuid_factory
        self.value_adapter = value_adapter or ValueXmlAdapter()
        self.stub_adapter = stub_adapter or StubXmlAdapter()
        self.analysis_store = analysis_store if analysis_store is not None else getattr(repository, "analysis_store", None)
        # The historical monolith owns analysis, projection, and lifecycle
        # methods directly; retain self aliases for legacy integrations that
        # accessed the former collaborator attributes.
        self._conflict = _merge_b_conflict
        self.projection = self
        self.analysis = self
        self._projection = self
        self._analysis = self
        self._lifecycle = self
        self.body_validator = body_validator
        self.resolution_registry = resolution_registry
        self.resolution_decisions = resolution_decisions
        self.resolution_applier = resolution_applier
        # A service outcome carries a per-operation report.  Keep the initial
        # object for the first call (which permits deterministic test clocks),
        # then allocate a fresh report for every subsequent call so a reused
        # service cannot attribute prior operations to the current one.
        self.metrics = metrics or PhaseMetrics()
        self._initial_metrics = self.metrics
        self._metrics_started = False

    # ------------------------------------------------------------------
    # Merge B orchestration helpers
    # ------------------------------------------------------------------


    def _projection_or_default(self) -> "MergeBService":
        """Return this monolith as the projection compatibility target."""

        if not hasattr(self, "value_adapter"):
            self.value_adapter = ValueXmlAdapter()
        if not hasattr(self, "stub_adapter"):
            self.stub_adapter = StubXmlAdapter()
        return self

    def _event(
        self,
        operation_id: str,
        module_key: str,
        function_key: str,
        accepted_revision: int | None,
        commit_revision: int | None,
        previous_version: DisplayVersion | None,
        next_version: DisplayVersion | None,
        semantic_change: bool,
        outcome: str,
        reason: str,
        delta_count: int,
        *,
        conflict_codes: tuple[str, ...] = (),
        validation_ok: bool | None = None,
    ) -> AuditEvent:
        """Build one Merge B audit event with shared version timestamps."""

        return AuditEvent(
            operation_id,
            "merge_b",
            self.mode,
            module_key,
            function_key,
            accepted_revision,
            commit_revision,
            previous_version,
            next_version,
            None if previous_version is None else previous_version.updated_at,
            None if next_version is None else next_version.updated_at,
            bool(semantic_change),
            outcome,
            reason,
            max(0, int(delta_count)),
            tuple(conflict_codes),
            validation_ok,
        )


    def _failure(
        self,
        operation_id: str,
        module_key: str,
        function_key: str,
        accepted: AcceptedBundle | None,
        conflicts: tuple[MergeConflict, ...],
        reason: str,
        *,
        semantic_change: bool = False,
        delta_count: int = 0,
    ) -> MergeOutcome:
        """Return a terminal pre-publication failure without mutating state."""

        previous: DisplayVersion | None = None
        revision: int | None = None
        if accepted is not None:
            revision = accepted.revision
            for state in accepted.function_states:
                if state.function_key == function_key or state.function == function_key:
                    previous = state.display_version
                    break
            if previous is None and accepted.function_states and function_key == "module":
                previous = accepted.function_states[0].display_version
        blocking = any(item.severity == "blocking" for item in conflicts)
        outcome = "conflict" if blocking else "failed"
        event = self._event(
            operation_id,
            module_key,
            function_key or "unknown_function",
            revision,
            None,
            previous,
            None,
            semantic_change,
            outcome,
            reason,
            delta_count,
            conflict_codes=tuple(item.code for item in conflicts),
            validation_ok=False,
        )
        return MergeOutcome(
            None,
            semantic_change,
            tuple(conflicts),
            (event,),
            self.mode,
            False,
            accepted,
            reason,
        )


    @staticmethod
    def _abort_transaction(transaction: Any | None) -> None:
        abort_transaction(transaction)

    # These artifact/validation predicates are deliberately shared policy.
    # Keep explicit forwarding methods for the service's internal call sites
    # and legacy integrations, without coupling Merge B to Merge A internals.

    @staticmethod
    def _validation_conflicts(
        report: ValidationReport,
        *,
        module_key: str,
        function_key: str,
    ) -> tuple[MergeConflict, ...]:
        return validation_conflicts(report, module_key=module_key, function_key=function_key)


    def _resolve_analysis_reference(
        self,
        accepted: AcceptedBundle,
        analysis_id: str,
        supplied: object | None,
        *,
        module_key: str,
        function_key: str,
    ) -> tuple[AnalysisRef | None, tuple[MergeConflict, ...]]:
        """Resolve and validate the immutable ANALYSIS_NEW reference.

        The service accepts an ``AnalysisRef``, an ``AnalysisSnapshot``-like
        object, a stored analysis ID, or a DB path.  A path is content-addressed
        through the configured store; putting the same payload twice therefore
        reuses the existing immutable file and never changes its timestamp.
        """

        old_ref = accepted.module_manifest.analysis
        conflicts: list[MergeConflict] = []
        if type(analysis_id) is not str or not analysis_id:
            conflicts.append(
                self._conflict(
                    module_key=module_key,
                    function_key=function_key,
                    code="ANALYSIS_DB_MISMATCH",
                    message="NEW analysis_id is missing or invalid",
                    base=old_ref.analysis_id,
                    new=analysis_id,
                )
            )
            return None, tuple(conflicts)

        def fail(code: str, message: str, *, base: object = None, user: object = None, new: object = None) -> None:
            conflicts.append(
                self._conflict(
                    module_key=module_key,
                    function_key=function_key,
                    code=code,
                    message=message,
                    entity_type="analysis",
                    base=base,
                    user=user,
                    new=new,
                )
            )

        ref: AnalysisRef | None = None
        source_path: Path | None = None
        if isinstance(supplied, AnalysisRef):
            ref = supplied
        elif supplied is not None:
            candidate_ref = getattr(supplied, "ref", None)
            if isinstance(candidate_ref, AnalysisRef):
                ref = candidate_ref
            elif isinstance(supplied, (str, Path)):
                text = str(supplied)
                if text == analysis_id and not Path(text).exists():
                    # A bare stored ID is resolved below; a path is put into
                    # the content-addressed store first.
                    supplied = text
                else:
                    source_path = Path(supplied)
            elif getattr(supplied, "analysis_id", None) is not None:
                fail(
                    "ANALYSIS_DB_MISMATCH",
                    "analysis_new exposes an analysis_id but no valid AnalysisRef",
                    user=getattr(supplied, "analysis_id", None),
                    new=analysis_id,
                )
        if source_path is not None:
            store = self.analysis_store
            if store is None or not hasattr(store, "put"):
                fail(
                    "ANALYSIS_DB_INTEGRITY_ERROR",
                    "a configured AnalysisStore is required to import an Analysis DB path",
                    new=str(source_path),
                )
            else:
                try:
                    ref = store.put(source_path)
                except Exception as exc:
                    fail(
                        "ANALYSIS_DB_INTEGRITY_ERROR",
                        f"Analysis DB could not be content-addressed: {exc}",
                        new=str(source_path),
                    )
        elif isinstance(supplied, str) and supplied == analysis_id:
            # Resolve an ID below.  Do not fabricate integrity metadata from
            # the identifier alone.
            ref = None

        if ref is not None and ref.analysis_id != analysis_id:
            fail(
                "ANALYSIS_DB_MISMATCH",
                "supplied AnalysisRef does not match NEW analysis_id",
                user=ref.analysis_id,
                new=analysis_id,
            )

        if analysis_id == old_ref.analysis_id:
            if ref is not None and ref != old_ref:
                fail(
                    "ANALYSIS_DB_INTEGRITY_ERROR",
                    "supplied reference disagrees with the accepted immutable AnalysisRef",
                    base=old_ref,
                    user=ref,
                )
            if conflicts:
                return None, tuple(conflicts)
            # An unchanged analysis does not need to be resolved again; the
            # accepted repository already validated this immutable reference.
            return old_ref, ()

        store = self.analysis_store
        if ref is None and store is not None and hasattr(store, "resolve"):
            try:
                resolved = store.resolve(analysis_id)
                candidate_ref = getattr(resolved, "ref", None)
                if isinstance(candidate_ref, AnalysisRef):
                    ref = candidate_ref
                elif isinstance(resolved, AnalysisRef):
                    ref = resolved
                else:
                    fail(
                        "ANALYSIS_DB_INTEGRITY_ERROR",
                        "AnalysisStore.resolve did not return an AnalysisRef",
                        new=analysis_id,
                    )
            except Exception as exc:
                fail(
                    "ANALYSIS_DB_INTEGRITY_ERROR",
                    f"ANALYSIS_NEW could not be resolved: {exc}",
                    new=analysis_id,
                )
        elif ref is None and store is not None and hasattr(store, "validate"):
            try:
                store.validate(analysis_id)
            except Exception as exc:
                fail(
                    "ANALYSIS_DB_INTEGRITY_ERROR",
                    f"ANALYSIS_NEW failed immutable validation: {exc}",
                    new=analysis_id,
                )
        elif ref is None:
            fail(
                "ANALYSIS_DB_MISMATCH",
                "ANALYSIS_NEW must provide a resolvable AnalysisRef",
                base=old_ref.analysis_id,
                new=analysis_id,
            )

        if ref is not None and ref.analysis_id != analysis_id:
            fail(
                "ANALYSIS_DB_MISMATCH",
                "resolved AnalysisRef does not match NEW analysis_id",
                user=ref.analysis_id,
                new=analysis_id,
            )
        if conflicts:
            return None, tuple(conflicts)
        # PR05's transaction intentionally requires the optional semantic
        # fingerprint to remain absent.  Keep the immutable content/file
        # hashes while dropping that non-authoritative field at this boundary.
        if ref is not None and ref.analysis_fingerprint is not None:
            ref = replace(ref, analysis_fingerprint=None)
        return ref, ()

    @staticmethod
    def _analysis_model(value: object | None) -> object | None:
        """Unwrap an AnalysisSnapshot/adapter into its immutable model."""

        if value is None:
            return None
        for attribute in ("analysis", "module_analysis"):
            try:
                model = getattr(value, attribute, None)
            except Exception:
                model = None
            if model is not None:
                return model
        if hasattr(value, "functions"):
            return value
        return None

    @staticmethod
    def _analysis_functions(value: object | None) -> tuple[object, ...]:
        model = MergeBService._analysis_model(value)
        if model is None:
            return ()
        functions = getattr(model, "functions", None)
        if isinstance(functions, Mapping):
            return tuple(functions.values())
        if functions is None or isinstance(functions, (str, bytes, bytearray)):
            return ()
        try:
            return tuple(functions)
        except TypeError:
            return ()

    @staticmethod
    def _analysis_function_name(function: object) -> str:
        if isinstance(function, Mapping):
            value = function.get("name", function.get("function", ""))
        else:
            value = getattr(function, "name", getattr(function, "function", ""))
        return str(value) if value is not None else ""

    @staticmethod
    def _analysis_stub_name(stub: object) -> str:
        if isinstance(stub, Mapping):
            value = stub.get("name", stub.get("function_name", ""))
        else:
            value = getattr(stub, "name", getattr(stub, "function_name", ""))
        return str(value) if value is not None else ""

    @staticmethod
    def _analysis_stub_field(stub: object, *names: str) -> object | None:
        if isinstance(stub, Mapping):
            for name in names:
                if name in stub and stub[name] is not None:
                    return stub[name]
            return None
        for name in names:
            value = getattr(stub, name, None)
            if value is not None:
                return value
        return None

    @staticmethod
    def _analysis_parameter_pair(parameter: object) -> tuple[str, str] | None:
        if isinstance(parameter, Mapping):
            type_name = parameter.get("type_name", parameter.get("type", parameter.get("ctype")))
            name = parameter.get("name", parameter.get("parameter_name"))
        elif isinstance(parameter, (tuple, list)) and len(parameter) >= 2:
            type_name, name = parameter[0], parameter[1]
        else:
            type_name = getattr(parameter, "type_name", getattr(parameter, "type", getattr(parameter, "ctype", None)))
            name = getattr(parameter, "name", getattr(parameter, "parameter_name", None))
        if hasattr(type_name, "name"):
            type_name = getattr(type_name, "name")
        if type_name is None or name is None:
            return None
        type_text, name_text = str(type_name), str(name)
        if not type_text or not name_text:
            return None
        return type_text, name_text

    def _analysis_stub_states(
        self,
        supplied: object | None,
        *,
        analysis_id: str,
        module_key: str,
    ) -> tuple[tuple[StubState, ...], frozenset[str], tuple[MergeConflict, ...], bool]:
        """Decode module-wide source stubs from an immutable analysis DB.

        The analysis model is the authority for signatures and owners.  A
        caller-provided ``new_stubs`` tuple is checked against this result by
        the caller, but never allowed to replace it.
        """

        source = supplied
        store = self.analysis_store
        if store is not None and hasattr(store, "resolve"):
            # Once a real content-addressed store is configured, a caller's
            # attached ``analysis`` object is only a convenience hint.  It is
            # mutable and must never override the immutable DB record.  Use
            # the stored model whenever one is available; a ref-only
            # lightweight double may fall back to the supplied model solely as
            # a compatibility boundary for tests that cannot decode XML.
            try:
                resolved = store.resolve(analysis_id)
            except Exception as exc:
                conflict = self._conflict(
                    module_key=module_key,
                    function_key="module",
                    code="ANALYSIS_DB_INTEGRITY_ERROR",
                    entity_type="analysis",
                    message=f"ANALYSIS_NEW could not decode the immutable analysis model: {exc}",
                    new=analysis_id,
                )
                return (), frozenset(), (conflict,), False
            resolved_ref = getattr(resolved, "ref", None)
            if isinstance(resolved, AnalysisRef) and resolved_ref is None:
                resolved_ref = resolved
            if isinstance(resolved_ref, AnalysisRef) and resolved_ref.analysis_id != analysis_id:
                conflict = self._conflict(
                    module_key=module_key,
                    function_key="module",
                    code="ANALYSIS_DB_INTEGRITY_ERROR",
                    entity_type="analysis",
                    message="resolved immutable analysis reference does not match NEW analysis_id",
                    user=resolved_ref.analysis_id,
                    new=analysis_id,
                )
                return (), frozenset(), (conflict,), False
            stored_model = self._analysis_model(resolved)
            source = resolved if stored_model is not None else (supplied if self._analysis_model(supplied) is not None else resolved)
        functions = self._analysis_functions(source)
        if not functions:
            return (), frozenset(), (), False

        conflicts: list[MergeConflict] = []
        by_name: dict[str, StubState] = {}
        required: set[str] = set()
        for function in functions:
            owner = self._analysis_function_name(function)
            raw_stubs = (
                function.get("stubs", ())
                if isinstance(function, Mapping)
                else getattr(function, "stubs", ())
            )
            if raw_stubs is None or isinstance(raw_stubs, (str, bytes, bytearray)):
                raw_stubs = ()
            try:
                stubs = tuple(raw_stubs)
            except TypeError:
                stubs = ()
            for raw_stub in stubs:
                name = self._analysis_stub_name(raw_stub)
                if not name:
                    conflicts.append(
                        self._conflict(
                            module_key=module_key,
                            function_key=owner or "module",
                            code="MISSING_STUB_SIGNATURE",
                            message="analysis DB contains a stub without a function name",
                        )
                    )
                    continue
                ret_ctype = self._analysis_stub_field(raw_stub, "ret_ctype", "return_type", "type")
                return_type = getattr(ret_ctype, "name", None) or (str(ret_ctype) if ret_ctype is not None else None)
                ret_struct = self._analysis_stub_field(raw_stub, "ret_struct", "return_struct")
                if not return_type and ret_struct is not None:
                    return_type = str(ret_struct)
                raw_parameters = self._analysis_stub_field(raw_stub, "params", "parameters", "arguments") or ()
                try:
                    parameter_pairs = tuple(self._analysis_parameter_pair(item) for item in raw_parameters)
                except TypeError:
                    parameter_pairs = ()
                if not return_type or any(item is None for item in parameter_pairs):
                    conflicts.append(
                        self._conflict(
                            module_key=module_key,
                            function_key=owner or "module",
                            code="MISSING_STUB_SIGNATURE",
                            message=f"analysis DB cannot resolve complete signature for stub {name!r}",
                            new=raw_stub,
                        )
                    )
                    continue
                parameters = tuple(StubParameter(type_name, parameter_name) for type_name, parameter_name in parameter_pairs if type_name and parameter_name)
                candidate = StubState(
                    stub_key=name,
                    function_name=name,
                    return_type=return_type,
                    return_storage_name=None,
                    parameters=parameters,
                    body_raw="",
                    owners=frozenset({owner}) if owner else frozenset(),
                    origin="generated",
                    status="active",
                    metadata_status="complete",
                )
                required.add(name)
                previous = by_name.get(name)
                if previous is None:
                    by_name[name] = candidate
                elif stub_signature_tuple(previous) != stub_signature_tuple(candidate):
                    conflicts.append(
                        self._conflict(
                            module_key=module_key,
                            function_key=owner or "module",
                            code="DUPLICATE_STUB_NAME",
                            message=f"analysis DB provides conflicting signatures for stub {name!r}",
                            base=previous,
                            new=candidate,
                        )
                    )
                else:
                    by_name[name] = replace(previous, owners=frozenset(set(previous.owners) | set(candidate.owners)))
        return tuple(by_name[name] for name in sorted(by_name)), frozenset(required), tuple(conflicts), True

    def _stub_inputs_agree(
        self,
        authoritative: Iterable[StubState],
        supplied: Iterable[StubState],
    ) -> bool:
        def projection(states: Iterable[StubState]) -> tuple[tuple[Any, ...], ...]:
            return tuple(sorted(
                (
                    state.function_name,
                    stub_signature_tuple(state),
                    tuple(sorted(state.owners)),
                )
                for state in states
            ))
        return projection(authoritative) == projection(supplied)


    @staticmethod
    def _artifact_slot(record: ArtifactRecord) -> str | None:
        return artifact_slot(record)

    @staticmethod
    def _artifact_matches_function(
        record: ArtifactRecord,
        function_key: str,
        role: str,
        role_counts: Mapping[str, int],
    ) -> bool:
        return artifact_matches_function(record, function_key, role, role_counts)

    @staticmethod
    def _artifact_role(record: ArtifactRecord) -> str | None:
        return artifact_role(record)

    def _value_validator(self, data: bytes) -> None:
        self.value_adapter.read(data)

    def _stub_validator(self, data: bytes) -> None:
        self.stub_adapter.read(data)

    def _snapshot_projection(
        self,
        accepted: AcceptedBundle,
        function_key: str,
        role: str,
        slot: str,
        reader: Callable[[bytes], Any],
    ) -> Any | None:
        counts: dict[str, int] = {"value": 0, "stub": 0}
        for record in accepted.artifacts:
            path = record.relative_path.lower()
            kind = record.kind.lower()
            if "value" in kind or path.startswith("value/") or "/value/" in path:
                counts["value"] += 1
            elif "stub" in kind or path.startswith("stub/") or "/stub/" in path:
                counts["stub"] += 1
        for record in accepted.artifacts:
            if record.relative_path in _INTERNAL_ARTIFACTS or _STATE_ARTIFACT.fullmatch(record.relative_path):
                continue
            if self._artifact_role(record) != role or self._artifact_slot(record) != slot:
                continue
            if not self._artifact_matches_function(record, function_key, role, counts):
                continue
            try:
                snapshot = reader(record.data)
            except Exception:
                continue
            identity = snapshot.name if role == "value" else snapshot.function_name
            if identity:
                return snapshot
        return None

    @staticmethod
    def _snapshot_for_slot(value: object | None, slot: str, role: str) -> object | None:
        """Select a BASE/WORKING projection from a compatibility input.

        The public Merge B API historically accepted one snapshot per role.
        PR13 also permits a generator to provide explicit per-slot snapshots,
        for example ``{"base": new_xml, "working": merged_xml}``.  Keep the
        compatibility form (a plain ``FunctionSnapshot``/``StubSnapshot``)
        while making the slot distinction explicit whenever it is available.
        """

        if value is None:
            return None
        if isinstance(value, Mapping):
            # The role-qualified spellings are useful to callers that pass a
            # single dictionary containing value and stub projections.
            candidates = (
                f"{slot}_{role}",
                f"{slot}{role.capitalize()}",
                slot,
                role,
            )
            for key in candidates:
                if key in value and value[key] is not None:
                    return value[key]
        return value

    @staticmethod
    def _argument_states_from_value_snapshot(
        snapshot: object | None,
    ) -> tuple[tuple[ArgumentState, ...] | None, tuple[tuple[str, str, object | None], ...]]:
        """Normalize a NEW value snapshot into the one authoritative schema.

        ``ValueArgument`` is the only source available when a caller supplies
        NEW value XML without separately passing ``new_arguments``.  Reuse the
        Merge A conversion helper here instead of maintaining a second set of
        pointer/direction/dimension defaults.  The returned diagnostics are
        intentionally data-only; the orchestration layer adds module/function
        context when it creates ``MergeConflict`` records.

        The XML stream values are not part of ``ArgumentState``.  Only logical
        arguments are copied, in source XML order.  A duplicate logical name
        or unresolved required metadata is blocking because choosing one
        record would create a second source of truth.
        """

        if snapshot is None:
            return None, ()
        if not isinstance(snapshot, FunctionSnapshot):
            return None, (("ARGUMENT_SCHEMA_CONFLICT", "NEW value snapshot is not a FunctionSnapshot", None),)

        states: list[ArgumentState] = []
        diagnostics: list[tuple[str, str, object | None]] = []
        seen: dict[str, ArgumentState] = {}
        # ``original_order`` is adapter-owned XML position.  The tuple index
        # is used only as a deterministic tie breaker for direct constructors
        # that leave every argument at order zero.
        ordered = list(enumerate(snapshot.arguments))
        ordered.sort(key=lambda item: (getattr(item[1], "original_order", 0), item[0]))
        for index, value_argument in ordered:
            if (
                getattr(value_argument, "internal", False)
                or getattr(value_argument, "synthetic_pointer_control", False)
                or _is_internal_argument_name(str(getattr(value_argument, "name", "")))
            ):
                continue
            try:
                argument = _argument_state_from_value_argument(value_argument)
            except Exception as exc:
                diagnostics.append(
                    (
                        "ARGUMENT_SCHEMA_CONFLICT",
                        f"NEW value argument metadata is invalid: {exc}",
                        getattr(value_argument, "name", None),
                    )
                )
                continue
            # ``ValueArgument.original_order`` is an adapter position.  A
            # direct constructor commonly leaves it at the default zero for
            # every argument, while the rendered XML parser necessarily
            # assigns physical positions 0, 1, ... .  Canonicalize the
            # persisted state to logical source order so direct and parsed
            # snapshots use the same deterministic schema tuple.
            argument = replace(argument, original_order=len(states))
            logical_name = _canonical_argument_name(argument.name)
            previous = seen.get(logical_name)
            if previous is not None:
                diagnostics.append(
                    (
                        "ARGUMENT_SCHEMA_CONFLICT",
                        f"NEW value XML contains duplicate logical argument {logical_name!r}",
                        logical_name,
                    )
                )
                continue
            seen[logical_name] = argument
            states.append(argument)
            if argument.fmt in (None, ""):
                diagnostics.append(
                    (
                        "ARGUMENT_SCHEMA_CONFLICT",
                        f"NEW argument {argument.name!r} lacks format metadata",
                        logical_name,
                    )
                )
            if argument.xml_len in (None, ""):
                diagnostics.append(
                    (
                        "ARGUMENT_SCHEMA_CONFLICT",
                        f"NEW argument {argument.name!r} lacks XML length metadata",
                        logical_name,
                    )
                )
            if argument.pointer is None or argument.metadata_status != "complete":
                diagnostics.append(
                    (
                        "ARGUMENT_SCHEMA_CONFLICT",
                        f"NEW argument {argument.name!r} has unresolved pointer metadata",
                        logical_name,
                    )
                )
        # The source order is semantically meaningful to XML/TESSY.  Do not
        # sort by name, but make the ordering deterministic for equal adapter
        # positions through the stable sort above.
        return tuple(states), tuple(diagnostics)

    @staticmethod
    def _argument_schema_projection(value: object | None) -> tuple[tuple[object, ...], ...] | None:
        """Return exact schema fields used for NEW XML consistency checks."""

        if value is None:
            return None
        if isinstance(value, Mapping) or isinstance(value, (str, bytes, bytearray)):
            return None
        try:
            items = tuple(value)  # type: ignore[arg-type]
        except TypeError:
            return None
        if not all(isinstance(item, ArgumentState) for item in items):
            return None
        return tuple(
            (
                item.key.canonical_xml_name,
                item.key.occurrence,
                item.name,
                item.direction,
                item.fmt,
                item.xml_len,
                item.pointer,
                tuple(item.dimensions),
                item.testmethod,
                item.range_encoding,
                item.output_encoding,
            )
            for item in items
        )

    @staticmethod
    def _projection_cell_lexeme(value: object) -> str | None:
        """Return the exact wire lexeme for a merged state cell.

        Value XML is a lexical compatibility format.  Typed workbook cells
        therefore retain ``raw_text`` where present; ordinary generated model
        values are only stringified at this final projection boundary.
        """

        if value is _PROJECTION_MISSING:
            return None
        if isinstance(value, CellValue):
            if value.raw_text is not None:
                return value.raw_text
            if value.kind == "missing":
                return None
            return ""
        if value is None:
            # A present Python ``None`` is an explicit empty state, whereas a
            # missing mapping key is handled by the sentinel above.
            return ""
        if type(value) is bool:
            # Match the workbook adapter's canonical boolean spelling.
            return "TRUE" if value else "FALSE"
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="surrogateescape")
        return str(value)

    @staticmethod
    def _projection_row_value(row: object, channel: str, name: str) -> object:
        """Read one state cell, accepting canonical array-name aliases."""

        mapping = getattr(row, channel, None)
        if not isinstance(mapping, Mapping):
            return _PROJECTION_MISSING
        if name in mapping:
            return mapping[name]
        # Some lightweight callers keep an array's scalar base name in the
        # row map even though the XML schema exposes expanded names.  Accept
        # that spelling only as a representation alias; no row identity is
        # inferred from it.  When the alias is an aggregate list/dict, select
        # the exact array element rather than stringifying the whole object.
        bare_name = re.sub(r"\[[0-9]+\]", "", name)
        if bare_name in mapping:
            value = mapping[bare_name]
            indices = tuple(int(item) for item in re.findall(r"\[([0-9]+)\]", name))
            for index in indices:
                if isinstance(value, Mapping):
                    if index in value:
                        value = value[index]
                    elif str(index) in value:
                        value = value[str(index)]
                    else:
                        return _PROJECTION_INVALID
                elif isinstance(value, (tuple, list)):
                    if index >= len(value):
                        return _PROJECTION_INVALID
                    value = value[index]
                else:
                    # An aggregate scalar cannot prove any particular array
                    # element.  Fail closed instead of emitting its repr.
                    return _PROJECTION_INVALID
            return value
        return _PROJECTION_MISSING

    @staticmethod
    def _projection_stream(
        groups: tuple[tuple[str, ...], ...],
        dimensions: tuple[int, ...],
    ) -> str | None:
        """Encode expanded element streams using canonical XML policy."""

        if not groups:
            return None
        if not dimensions:
            return join_stream((groups[0],))
        if all(group == groups[0] for group in groups[1:]):
            return join_stream((groups[0],))
        return join_stream(groups)

    def _project_value_snapshot(
        self,
        snapshot: object | None,
        rows: Iterable[TestRow],
        *,
        fallback: FunctionSnapshot | None = None,
        strict_expected: bool = False,
    ) -> object | None:
        """Project merged state rows into a value XML snapshot.

        A supplied NEW XML snapshot is the generator schema authority, but it
        is not the WORKING payload.  Rebuilding its streams from the merged
        rows is what prevents a manual cell from being silently replaced by
        pure NEW data.  Unknown fields fall back to the source snapshot's
        same testcase position when possible; if no source stream exists they
        remain an explicit empty lexeme, preserving row cardinality instead
        of inventing a shorter XML stream.
        """

        if not isinstance(snapshot, FunctionSnapshot):
            return snapshot
        merged_rows = tuple(rows)
        source_rows = snapshot.logical_rows()
        fallback_rows = fallback.logical_rows() if isinstance(fallback, FunctionSnapshot) else ()
        projected: list[ValueArgument] = []
        for argument in snapshot.arguments:
            names = argument.expanded_names
            input_groups: list[tuple[str, ...]] = []
            output_groups: list[tuple[str, ...]] = []
            input_required = argument.direction in {"in", "inout"}
            output_required = argument.direction in {"out", "inout"}
            # Direction is schema authority.  A stray forbidden stream in a
            # compatibility snapshot or state map is never emitted.
            input_present = input_required and argument.range is not None
            output_present = output_required and argument.output is not None
            unresolved_input = False
            unresolved_output = False
            for name in names:
                input_tokens: list[str] = []
                output_tokens: list[str] = []
                for index, row in enumerate(merged_rows):
                    if input_required:
                        input_value = self._projection_row_value(row, "values", name)
                        if input_value is _PROJECTION_INVALID:
                            raise MergeBProjectionError(
                                "UNRESOLVED_VALUE_CELL",
                                f"merged WORKING row {index} cannot resolve array input {name!r}",
                            )
                        input_lexeme = self._projection_cell_lexeme(input_value)
                        if input_lexeme is not None:
                            input_present = True
                        else:
                            unresolved_input = True
                        input_tokens.append("" if input_lexeme is None else input_lexeme)
                    if output_required:
                        output_value = self._projection_row_value(row, "expected", name)
                        if output_value is _PROJECTION_INVALID:
                            raise MergeBProjectionError(
                                "UNRESOLVED_EXPECTED_OUTPUT",
                                f"merged WORKING row {index} cannot resolve array output {name!r}",
                            )
                        output_lexeme = self._projection_cell_lexeme(output_value)
                        if output_lexeme is not None:
                            output_present = True
                        else:
                            unresolved_output = True
                        output_tokens.append("" if output_lexeme is None else output_lexeme)
                if input_required:
                    input_groups.append(tuple(input_tokens))
                if output_required:
                    output_groups.append(tuple(output_tokens))
            if input_required and unresolved_input:
                raise MergeBProjectionError(
                    "UNRESOLVED_VALUE_CELL",
                    f"merged WORKING snapshot has no resolvable input value for {argument.name!r}",
                )
            if output_required and unresolved_output:
                raise MergeBProjectionError(
                    "UNRESOLVED_EXPECTED_OUTPUT",
                    f"merged WORKING snapshot has no resolvable expected output for {argument.name!r}",
                )
            input_raw = self._projection_stream(tuple(input_groups), argument.dimensions) if input_present else None
            output_raw = self._projection_stream(tuple(output_groups), argument.dimensions) if output_present else None
            projected.append(argument.with_values(range=input_raw, output=output_raw))
        return replace(snapshot, arguments=tuple(projected))

    @staticmethod
    def _stub_signature_tuple(state: StubState) -> tuple[Any, ...]:
        return (
            state.function_name,
            state.return_type,
            state.return_storage_name,
            tuple((item.type_name, item.name) for item in state.parameters),
        )

    @staticmethod
    def _stub_states_from_snapshot(snapshot: StubSnapshot | None, function_key: str) -> tuple[StubState, ...]:
        if snapshot is None:
            return ()
        states: list[StubState] = []
        for called in snapshot.called_functions:
            try:
                return_type = getattr(called, "return_type", None)
                parameters = tuple(
                    StubParameter(str(item.type_name), str(item.name))
                    for item in called.arguments
                )
                metadata_status = (
                    "complete"
                    if return_type and all(parameter.type_name and parameter.name for parameter in parameters)
                    else "unresolved"
                )
                states.append(
                    StubState(
                        stub_key=str(called.name),
                        function_name=str(called.name),
                        return_type=return_type,
                        return_storage_name=getattr(called, "return_name", None),
                        parameters=parameters,
                        body_raw="",
                        owners=frozenset({function_key}),
                        origin="generated",
                        status="active",
                        metadata_status=metadata_status,
                    )
                )
            except Exception:
                # An adapter snapshot with incomplete signature metadata is
                # represented as a blocking lifecycle conflict by the caller;
                # never fabricate a partial StubState here.
                continue
        return tuple(states)

    @staticmethod
    def _stub_snapshot_from_states(
        states: Iterable[StubState],
        *,
        function_key: str,
        function_name: str,
        source_file: str,
        template: StubSnapshot | None = None,
        fallback_template: StubSnapshot | None = None,
        require_payload_templates: Iterable[str] = (),
    ) -> StubSnapshot:
        """Apply normalized signatures to a complete XML payload template.

        The analysis DB and ``StubState`` own source signature/ownership; the
        adapter's XML is a compatibility projection.  When a parsed NEW or
        accepted snapshot is available, it is the payload template: only
        known signature fields are updated.  Unknown attributes/elements,
        synthetic TESSY child names, raw text, and child order are retained by
        the stub adapter's raw-element renderer.  A template gap for an
        existing changed signature is blocking instead of silently rebuilding
        a lossy bare parameter.

        ``fallback_template`` is used for a newly-added called function when
        the primary (usually accepted WORKING) template does not contain it;
        this allows a NEW template to provide the complete payload while still
        preserving accepted payload for existing calls.
        """

        template = template if isinstance(template, StubSnapshot) else None
        fallback_template = fallback_template if isinstance(fallback_template, StubSnapshot) else None
        required_template_aliases = {
            str(alias)
            for alias in require_payload_templates
            if str(alias)
        }
        state_items = [
            state
            for state in states
            if state.status == "active"
            and (
                not state.owners
                or function_key in state.owners
                or function_name in state.owners
            )
        ]
        if required_template_aliases and template is None and fallback_template is None:
            for state in state_items:
                if required_template_aliases.intersection(
                    {state.stub_key, state.function_name} - {""}
                ):
                    raise MergeBProjectionError(
                        "STUB_PAYLOAD_CONFLICT",
                        f"called function {state.function_name!r} has no lossless payload template",
                    )
        # Preserve caller/source order while rejecting duplicate exact names.
        seen_state_names: set[str] = set()
        for state in state_items:
            aliases = {state.stub_key, state.function_name} - {""}
            if seen_state_names.intersection(aliases):
                raise MergeBProjectionError(
                    "STUB_PAYLOAD_CONFLICT",
                    f"stub signature aliases are ambiguous for {state.stub_key!r}",
                )
            seen_state_names.update(aliases)

        primary_calls = () if template is None else tuple(template.called_functions)
        fallback_calls = () if fallback_template is None else tuple(fallback_template.called_functions)
        available_calls = primary_calls + tuple(
            item for item in fallback_calls if item.name not in {call.name for call in primary_calls}
        )
        used_call_indexes: set[int] = set()

        def choose_call(state: StubState) -> tuple[CalledFunction | None, int | None]:
            aliases = {state.stub_key, state.function_name} - {""}
            for index, candidate in enumerate(available_calls):
                if index in used_call_indexes:
                    continue
                if candidate.name in aliases:
                    return candidate, index
            return None, None

        def update_parameters(
            state: StubState,
            old_called: CalledFunction | None,
        ) -> tuple[XmlStubParameter, ...]:
            old_parameters = () if old_called is None else tuple(old_called.arguments)
            updated: list[XmlStubParameter] = []
            used_parameter_indexes: set[int] = set()
            for position, parameter in enumerate(state.parameters):
                selected_parameter: XmlStubParameter | None = None
                selected_index: int | None = None
                # Source parameter names (the XML ``name`` attribute) are an
                # exact identity when present.  Synthetic ``<name>`` text is
                # deliberately not used for matching.
                for index, candidate in enumerate(old_parameters):
                    if index in used_parameter_indexes:
                        continue
                    if candidate.name == parameter.name:
                        selected_parameter = candidate
                        selected_index = index
                        break
                if selected_parameter is None and position < len(old_parameters) and position not in used_parameter_indexes:
                    selected_parameter = old_parameters[position]
                    selected_index = position
                if selected_parameter is None:
                    if template is not None or fallback_template is not None:
                        raise MergeBProjectionError(
                            "STUB_PAYLOAD_CONFLICT",
                            f"stub parameter {parameter.name!r} has no lossless payload template",
                        )
                    updated.append(XmlStubParameter(parameter.type_name, parameter.name))
                    continue
                assert selected_index is not None
                used_parameter_indexes.add(selected_index)
                # A source-name edit against an old-only template cannot
                # prove that its synthetic TESSY storage child should change;
                # fail closed instead of silently replacing that payload.
                if selected_parameter.name != parameter.name and fallback_template is None:
                    raise MergeBProjectionError(
                        "STUB_PAYLOAD_CONFLICT",
                        f"stub parameter source name changed without a NEW payload template: {selected_parameter.name!r} -> {parameter.name!r}",
                    )
                updated.append(
                    replace(
                        deepcopy(selected_parameter),
                        type_name=parameter.type_name,
                        name=parameter.name,
                    )
                )
            return tuple(updated)

        called: list[CalledFunction] = []
        for state in state_items:
            old_called, call_index = choose_call(state)
            if old_called is None:
                if (
                    template is not None
                    or fallback_template is not None
                    or bool(
                        required_template_aliases.intersection(
                            {state.stub_key, state.function_name} - {""}
                        )
                    )
                ):
                    raise MergeBProjectionError(
                        "STUB_PAYLOAD_CONFLICT",
                        f"called function {state.function_name!r} has no lossless payload template",
                    )
                parameters = update_parameters(state, None)
                called.append(
                    CalledFunction(
                        name=state.function_name,
                        file=source_file,
                        return_type=state.return_type,
                        return_name=state.return_storage_name,
                        arguments=parameters,
                    )
                )
                continue
            assert call_index is not None
            used_call_indexes.add(call_index)
            parameters = update_parameters(state, old_called)
            called.append(
                replace(
                    deepcopy(old_called),
                    name=state.function_name,
                    return_type=state.return_type,
                    return_name=state.return_storage_name,
                    arguments=parameters,
                )
            )

        # Existing called-function order is wire-visible.  Reorder the state
        # updates to the template order and append genuinely new calls in the
        # deterministic state order above.
        by_name = {item.name: item for item in called}
        ordered_called: list[CalledFunction] = []
        for source_call in available_calls:
            if source_call.name in by_name:
                ordered_called.append(by_name.pop(source_call.name))
        ordered_called.extend(by_name.values())
        called = ordered_called

        root_template = template or fallback_template
        if root_template is None:
            return StubSnapshot(function_name=function_name, file=source_file, called_functions=tuple(called))
        return replace(
            root_template,
            function_name=function_name,
            # A parsed NEW/accepted snapshot is the complete wire payload
            # template.  Preserve an explicitly empty file attribute (and
            # any other raw root spelling) instead of filling it from the
            # source-state fallback; doing so would make the rendered
            # projection differ from the authoritative NEW payload.
            file=root_template.file,
            called_functions=tuple(called),
        )

    @staticmethod
    def _artifact_function_hint(
        record: ArtifactRecord,
        identities: Iterable[str],
    ) -> str | None:
        """Extract only an exact function hint from a projection path.

        Path hints are validation evidence, never a matching mechanism.  A
        root XML identity must still resolve to one function.  The suffix
        check supports the legacy ``value_<module>_<function>.xml`` spelling
        without introducing fuzzy matching.
        """

        path = record.relative_path
        parts = path.split("/")
        slot = parts[0] if parts and parts[0] in {"base", "working"} else None
        if slot is not None and len(parts) >= 3:
            return parts[1]
        filename = parts[-1]
        stem = filename.rsplit(".", 1)[0]
        if stem in {"value", "values", "stub", "stubs"}:
            return None
        known = tuple(sorted({str(item) for item in identities if str(item)}, key=lambda item: (-len(item), item)))
        if stem in known:
            return stem
        for identity in known:
            if stem.endswith("_" + identity):
                return identity
        # A role-directory filename which is not a role placeholder is an
        # explicit, exact hint.  The caller will reject it if it disagrees
        # with the XML root identity.
        if parts and parts[0].lower() in {"value", "values", "stub", "stubs"}:
            return stem
        return None

    def _merge_b_artifacts(
        self,
        accepted: AcceptedBundle,
        plans: Mapping[str, Mapping[str, Any]],
        removed_reviews: Iterable[Mapping[str, Any]],
        *,
        versions: Mapping[str, DisplayVersion],
        audit: AuditEvent | None,
    ) -> tuple[ArtifactRecord, ...]:
        """Render a complete, attributable BASE/WORKING XML projection set.

        Accepted artifacts are input evidence only.  Every value/stub XML is
        parsed and associated with exactly one active or removed function by
        its root identity plus exact path hints.  A recognized artifact that
        cannot be attributed, has stale metadata, or has no candidate state
        projection is rejected instead of being copied into the next
        revision.  This closes the old ``preserve unrelated XML`` escape hatch
        for versioned artifacts while retaining unrelated non-XML artifacts.
        """

        def render_projection(snapshot: object, role: str, version: DisplayVersion) -> bytes:
            if role == "value" and isinstance(snapshot, FunctionSnapshot):
                snapshot = snapshot.with_metadata(version)
                return self.value_adapter.render(snapshot, display_version=version)
            if role == "stub" and isinstance(snapshot, StubSnapshot):
                snapshot = snapshot.with_metadata(version)
                return self.stub_adapter.render(snapshot, display_version=version)
            raise MergeBProjectionError(
                "XML_ARTIFACT_STATE_MISMATCH",
                f"{role} artifact has no typed candidate snapshot",
            )

        def parse_role(record: ArtifactRecord) -> tuple[str | None, object | None]:
            role = self._artifact_role(record)
            if role == "value":
                try:
                    return role, self.value_adapter.read(record.data)
                except Exception as exc:
                    raise MergeBProjectionError(
                        "XML_ARTIFACT_INVALID",
                        f"value XML artifact {record.relative_path!r} is not parseable: {exc}",
                    ) from exc
            if role == "stub":
                try:
                    return role, self.stub_adapter.read(record.data)
                except Exception as exc:
                    raise MergeBProjectionError(
                        "XML_ARTIFACT_INVALID",
                        f"stub XML artifact {record.relative_path!r} is not parseable: {exc}",
                    ) from exc
            # A caller-owned XML file may have a generic kind/path.  Probe it
            # strictly so an unlabelled value/stub artifact cannot bypass the
            # attribution/metadata checks.  A malformed non-projection XML is
            # preserved as an unrelated artifact only when it does not look
            # like an XML projection at all.
            if not record.relative_path.lower().endswith(".xml"):
                return None, None
            value_snapshot: FunctionSnapshot | None = None
            stub_snapshot: StubSnapshot | None = None
            try:
                # Generic XML records are still candidate value/stub
                # artifacts even when they come from a legacy bootstrap
                # layout.  Parse them with the explicit legacy opt-in so the
                # caller cannot bypass attribution by omitting a role in the
                # artifact kind/path.  The metadata check below will reject
                # the legacy record for an accepted revision.
                value_snapshot = self.value_adapter.read(record.data, allow_legacy=True)
            except Exception:
                pass
            try:
                stub_snapshot = self.stub_adapter.read(record.data, allow_legacy=True)
            except Exception:
                pass
            if value_snapshot is not None and stub_snapshot is not None:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_UNATTRIBUTED",
                    f"XML artifact {record.relative_path!r} matches both value and stub formats",
                )
            if value_snapshot is not None:
                return "value", value_snapshot
            if stub_snapshot is not None:
                return "stub", stub_snapshot
            # Versioned XML that is not recognized is unsafe to carry forward;
            # unversioned unrelated XML remains outside the Merge B planes.
            if "utgen_version" in record.data or "utgen_updated_at" in record.data:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_UNATTRIBUTED",
                    f"versioned XML artifact {record.relative_path!r} has no recognized UTGen projection",
                )
            return None, None

        def old_artifact_state(plan: Mapping[str, Any]) -> FunctionIterationState | None:
            """Return the accepted state represented by an input artifact.

            A rename deliberately clears ``plan['old']`` so the new function
            receives fresh row/case identities.  ``old_artifact_state`` keeps
            the removed identity available solely for validating and then
            discarding its accepted XML evidence.
            """

            candidate = plan.get("old_artifact_state")
            if candidate is None:
                candidate = plan.get("old")
            return candidate if isinstance(candidate, FunctionIterationState) else None

        def old_expected_version(
            plan: Mapping[str, Any],
            *,
            path: str,
        ) -> DisplayVersion | None:
            state = old_artifact_state(plan)
            if state is None:
                return None
            manifest_version = accepted.module_manifest.function_versions.get(state.function)
            if manifest_version != state.display_version:
                raise MergeBProjectionError(
                    "VERSION_METADATA_MISMATCH",
                    f"accepted state/manifest version mismatch for {state.function!r} while checking {path!r}",
                )
            return state.display_version

        def snapshot_identity(snapshot: object, role: str) -> str | None:
            if role == "value" and isinstance(snapshot, FunctionSnapshot):
                return snapshot.name
            if role == "stub" and isinstance(snapshot, StubSnapshot):
                return snapshot.function_name
            return None

        def filtered_stub_states(
            states: Iterable[StubState],
            *,
            key: str,
            function_name: str,
        ) -> tuple[StubState, ...]:
            owners = {key, function_name}
            return tuple(
                state
                for state in states
                if state.status == "active"
                and (
                    not state.owners
                    or bool(owners.intersection(state.owners))
                )
            )

        def schema_projection(snapshot: object, expected: object, *, path: str) -> None:
            if not isinstance(snapshot, FunctionSnapshot):
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"value XML artifact {path!r} is not a FunctionSnapshot",
                )
            normalized, diagnostics = self._argument_states_from_value_snapshot(snapshot)
            expected_projection = self._argument_schema_projection(expected)
            actual_projection = self._argument_schema_projection(normalized)
            if (
                normalized is None
                or diagnostics
                or expected_projection is None
                or actual_projection != expected_projection
            ):
                detail = "; ".join(item[1] for item in diagnostics)
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"value XML artifact {path!r} argument schema disagrees with accepted state"
                    + (f": {detail}" if detail else ""),
                )

        def rows_projection(
            snapshot: object,
            expected_rows: object,
            *,
            path: str,
        ) -> None:
            if not isinstance(snapshot, FunctionSnapshot):
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"value XML artifact {path!r} is not a FunctionSnapshot",
                )
            try:
                rows = tuple(expected_rows or ())
            except TypeError as exc:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"candidate state rows for {path!r} are not iterable",
                ) from exc
            # A state with no active rows must not be represented by a value
            # stream containing hidden rows.  Conversely, a non-empty state is
            # checked through the same deterministic projection used to build
            # the candidate XML.
            if not rows:
                if snapshot.row_count:
                    raise MergeBProjectionError(
                        "XML_ARTIFACT_STATE_MISMATCH",
                        f"value XML artifact {path!r} contains rows absent from state",
                    )
                return
            try:
                projected = self._project_value_snapshot(
                    snapshot,
                    rows,
                    fallback=snapshot,
                    strict_expected=True,
                )
            except MergeBProjectionError as exc:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"value XML artifact {path!r} cannot represent state rows: {exc}",
                ) from exc
            if not isinstance(projected, FunctionSnapshot) or _value_semantics(projected) != _value_semantics(snapshot):
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"value XML artifact {path!r} represented fields disagree with state rows",
                )

        def stub_projection(
            snapshot: object,
            expected_states: Iterable[StubState],
            *,
            path: str,
        ) -> None:
            if not isinstance(snapshot, StubSnapshot):
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"stub XML artifact {path!r} is not a StubSnapshot",
                )
            expected = tuple(
                sorted(
                    (self._stub_signature_tuple(state) for state in expected_states),
                    key=repr,
                )
            )
            actual = tuple(sorted((called.signature for called in snapshot.called_functions), key=repr))
            if actual != expected:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"stub XML artifact {path!r} signatures disagree with state",
                )

        def validate_existing_projection(
            parsed: object,
            *,
            role: str,
            slot: str,
            key: str,
            plan: Mapping[str, Any],
            path: str,
        ) -> None:
            """Validate accepted XML before candidate bytes can replace it.

            This is intentionally a separate pass from candidate rendering.
            Re-rendering a stale accepted artifact could make a bad historical
            state look healthy and would violate the fail-closed publication
            rule.
            """

            state = old_artifact_state(plan)
            expected_version = old_expected_version(plan, path=path)
            if state is None:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"accepted {role} XML artifact {path!r} has no prior function state",
                )
            if not isinstance(getattr(parsed, "display_version", None), DisplayVersion):
                raise MergeBProjectionError(
                    "VERSION_METADATA_MISMATCH",
                    f"accepted {role} XML artifact {path!r} has no version metadata",
                )
            if parsed.display_version != expected_version:
                raise MergeBProjectionError(
                    "VERSION_METADATA_MISMATCH",
                    f"accepted {role} XML artifact {path!r} metadata does not match prior state version",
                )
            expected_identity = state.function
            if snapshot_identity(parsed, role) != expected_identity:
                raise MergeBProjectionError(
                    "IDENTITY_FUNCTION_MISMATCH",
                    f"accepted {role} XML artifact {path!r} identity disagrees with prior state",
                )
            if role == "value":
                schema_projection(parsed, state.working.arguments, path=path)
                rows = state.base.rows if slot == "base" else state.working.rows
                rows_projection(parsed, rows, path=path)
            else:
                expected_states = filtered_stub_states(
                    accepted.module_stub_state,
                    key=state.function_key,
                    function_name=state.function,
                )
                stub_projection(parsed, expected_states, path=path)

        def validate_candidate_projection(
            data: bytes,
            source_snapshot: object,
            *,
            role: str,
            slot: str,
            key: str,
            plan: Mapping[str, Any],
            version: DisplayVersion,
            path: str,
        ) -> None:
            """Round-trip candidate bytes and compare every represented field."""

            try:
                parsed = (
                    self.value_adapter.read(data)
                    if role == "value"
                    else self.stub_adapter.read(data)
                )
            except Exception as exc:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"rendered {slot} {role} XML artifact {path!r} is not strict-readable: {exc}",
                ) from exc
            if parsed.display_version != version:
                raise MergeBProjectionError(
                    "VERSION_METADATA_MISMATCH",
                    f"rendered {slot} {role} XML artifact {path!r} metadata disagrees with candidate version",
                )
            expected_identity = str(
                plan.get("new_name")
                or (
                    plan["old"].function
                    if isinstance(plan.get("old"), FunctionIterationState)
                    else key
                )
            )
            if snapshot_identity(parsed, role) != expected_identity:
                raise MergeBProjectionError(
                    "IDENTITY_FUNCTION_MISMATCH",
                    f"rendered {slot} {role} XML artifact {path!r} identity disagrees with candidate state",
                )
            if role == "value":
                expected_arguments = plan.get(f"{slot}_arguments", ())
                schema_projection(parsed, expected_arguments, path=path)
                rows_projection(parsed, plan.get(f"{slot}_rows", ()), path=path)
                if not isinstance(source_snapshot, FunctionSnapshot) or _value_semantics(parsed) != _value_semantics(source_snapshot):
                    raise MergeBProjectionError(
                        "XML_ARTIFACT_STATE_MISMATCH",
                        f"rendered {slot} value XML artifact {path!r} differs from candidate representation",
                    )
            else:
                stub_projection(parsed, plan.get(f"{slot}_stub_states", ()), path=path)
                if not isinstance(source_snapshot, StubSnapshot) or _stub_semantics(parsed) != _stub_semantics(source_snapshot):
                    raise MergeBProjectionError(
                        "XML_ARTIFACT_STATE_MISMATCH",
                        f"rendered {slot} stub XML artifact {path!r} differs from candidate representation",
                    )
            return None

        def unscoped_path_is_explicit(
            record: ArtifactRecord,
            *,
            role: str,
            identities_for_function: Iterable[str],
        ) -> bool:
            """Allow only known legacy paths when BASE/WORKING is absent."""

            parts = record.relative_path.split("/")
            filename = parts[-1]
            stem = filename.rsplit(".", 1)[0]
            placeholders = {
                "value": {"value", "values"},
                "stub": {"stub", "stubs"},
            }[role]
            known = {str(item) for item in identities_for_function if str(item)}
            if len(parts) == 1 and (stem in placeholders or stem in known):
                return True
            if len(parts) == 1 and any(stem.endswith("_" + item) for item in known):
                return True
            if parts and parts[0].lower() in placeholders:
                return stem in placeholders or stem in known or any(
                    stem.endswith("_" + item) for item in known
                )
            return False

        all_plans = {str(key): plan for key, plan in plans.items()}
        active_plans = {
            key: plan for key, plan in all_plans.items() if not plan.get("removed")
        }
        identities: dict[str, set[str]] = {}
        identity_to_keys: dict[str, set[str]] = {}
        for key, plan in all_plans.items():
            values = {
                key,
                str(plan.get("new_name") or ""),
                str(plan["old"].function) if plan.get("old") is not None else "",
            }
            values.update(
                str(item)
                for item in (plan.get("removed_identities") or ())
                if str(item)
            )
            values.discard("")
            identities[key] = values
            for identity in values:
                identity_to_keys.setdefault(identity, set()).add(key)

        records: list[ArtifactRecord] = []
        seen: set[tuple[str, str, str]] = set()
        for record in accepted.artifacts:
            path = record.relative_path
            if (
                path in _INTERNAL_ARTIFACTS
                or _STATE_ARTIFACT.fullmatch(path)
                or path in {"audit/merge_a.json", "audit/merge_b.json"}
            ):
                continue
            role, parsed = parse_role(record)
            if role is None:
                records.append(record)
                continue
            assert parsed is not None
            identity = (
                parsed.name if role == "value" else parsed.function_name
            )
            candidate_keys = sorted(identity_to_keys.get(str(identity), ()))
            if len(candidate_keys) != 1:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_UNATTRIBUTED",
                    f"{role} XML artifact {path!r} has function identity {identity!r} that does not resolve to exactly one function",
                )
            key = candidate_keys[0]
            plan = all_plans[key]
            hint = self._artifact_function_hint(record, identities[key])
            if hint is not None and hint not in identities[key]:
                raise MergeBProjectionError(
                    "IDENTITY_FUNCTION_MISMATCH",
                    f"{role} XML artifact {path!r} path identity {hint!r} conflicts with root identity {identity!r}",
                )
            slot = self._artifact_slot(record)
            if slot is None:
                if not unscoped_path_is_explicit(
                    record,
                    role=role,
                    identities_for_function=identities[key],
                ):
                    raise MergeBProjectionError(
                        "XML_ARTIFACT_UNATTRIBUTED",
                        f"versioned {role} XML artifact {path!r} has no explicit function/slot path",
                    )
                if len(active_plans) != 1:
                    raise MergeBProjectionError(
                        "XML_ARTIFACT_UNATTRIBUTED",
                        f"unscoped {role} XML artifact {path!r} cannot identify BASE/WORKING in a multi-function bundle",
                    )
                # Legacy one-function repositories have one unscoped mirror;
                # treat it as WORKING and synthesize BASE below.
                slot = "working"
            marker = (key, slot, role)
            if marker in seen:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_UNATTRIBUTED",
                    f"duplicate {slot} {role} XML artifacts are present for function {key!r}",
                )
            seen.add(marker)
            validate_existing_projection(
                parsed,
                role=role,
                slot=slot,
                key=key,
                plan=plan,
                path=path,
            )
            if (
                plan.get("renamed")
                and str(identity) in {str(item) for item in (plan.get("removed_identities") or ())}
            ):
                # Rename is remove+add.  Historical XML bearing the removed
                # root identity must not be projected under the new function
                # path; it is represented only by review/state artifacts.
                continue
            if plan.get("removed"):
                # Removed source functions must not leave stale XML in the
                # accepted projection; any manual state is in review JSON.
                continue
            version = versions.get(key) or plan.get("version")
            snapshot = plan.get(f"{slot}_{role}_snapshot")
            if not isinstance(version, DisplayVersion) or snapshot is None:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"{slot} {role} XML artifact {path!r} has no candidate state projection",
                )
            try:
                data = render_projection(snapshot, role, version)
            except MergeBProjectionError:
                raise
            except Exception as exc:
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"{slot} {role} XML artifact {path!r} cannot render from candidate state: {exc}",
                ) from exc
            validate_candidate_projection(
                data,
                snapshot,
                role=role,
                slot=slot,
                key=key,
                plan=plan,
                version=version,
                path=path,
            )
            records.append(
                ArtifactRecord(
                    path,
                    record.kind,
                    data,
                    self._value_validator if role == "value" else self._stub_validator,
                )
            )

        # Add missing mirrors only from a typed candidate snapshot.  Never
        # fabricate an XML payload from a state map alone.
        for key in sorted(active_plans):
            plan = active_plans[key]
            version = versions.get(key) or plan.get("version")
            if not isinstance(version, DisplayVersion):
                raise MergeBProjectionError(
                    "XML_ARTIFACT_STATE_MISMATCH",
                    f"active function {key!r} has no candidate display version",
                )
            for slot in ("base", "working"):
                for role in ("value", "stub"):
                    marker = (key, slot, role)
                    snapshot = plan.get(f"{slot}_{role}_snapshot")
                    if snapshot is None or marker in seen:
                        continue
                    try:
                        data = render_projection(snapshot, role, version)
                    except MergeBProjectionError:
                        raise
                    except Exception as exc:
                        raise MergeBProjectionError(
                            "XML_ARTIFACT_STATE_MISMATCH",
                            f"missing {slot} {role} XML for function {key!r} cannot be rendered: {exc}",
                        ) from exc
                    validate_candidate_projection(
                        data,
                        snapshot,
                        role=role,
                        slot=slot,
                        key=key,
                        plan=plan,
                        version=version,
                        path=f"{slot}/{key}/{role}.xml",
                    )
                    records.append(
                        ArtifactRecord(
                            f"{slot}/{key}/{role}.xml",
                            f"{role}_xml",
                            data,
                            self._value_validator if role == "value" else self._stub_validator,
                        )
                    )
                    seen.add(marker)

        for review in removed_reviews:
            key = str(review.get("function_key") or "function")
            records.append(
                ArtifactRecord(
                    f"orphaned/{key}/review.json",
                    "orphan_review",
                    canonical_json_bytes(dict(review)),
                )
            )
        if audit is not None:
            records.append(
                ArtifactRecord(
                    "audit/merge_b.json",
                    "audit",
                    canonical_json_bytes({"events": [audit.to_dict()]}),
                )
            )
        return tuple(records)


    @staticmethod
    def _pure_snapshot_changed(
        old: BaseSnapshot,
        new: BaseSnapshot,
        old_arguments: object | None,
        new_arguments: object | None,
    ) -> bool:
        """Compare generator-owned content, excluding persistent row identity."""

        def row_payload(row: Any) -> object:
            return (
                getattr(row, "kind", None),
                getattr(row, "description", None),
                deepcopy(getattr(row, "values", None)),
                deepcopy(getattr(row, "expected", None)),
                deepcopy(getattr(row, "intent", None)),
                tuple(sorted(_obligations(row))),
            )

        if len(old.rows) != len(new.rows):
            return True
        if any(row_payload(left) != row_payload(right) for left, right in zip(old.rows, new.rows)):
            return True
        if old.function_fingerprint != new.function_fingerprint:
            return True
        if old.dependency_fingerprint != new.dependency_fingerprint:
            return True
        if old_arguments is not None and new_arguments is not None and old_arguments != new_arguments:
            return True
        # An analysis ID can change because another function, an absolute
        # machine path, or a transient line number changed.  If normalized
        # function/dependency fingerprints and rows are equal, this object is
        # unaffected and does not receive a major display-version bump.
        if old.analysis_id != new.analysis_id and (
            old.function_fingerprint is None or new.function_fingerprint is None
        ):
            return True
        return False

    @staticmethod
    def _working_payload(snapshot: WorkingSnapshot) -> object:
        rows = []
        for row in snapshot.rows:
            meta = _row_meta(row)
            rows.append(
                (
                    getattr(row, "kind", None),
                    getattr(row, "description", None),
                    deepcopy(getattr(row, "values", None)),
                    deepcopy(getattr(row, "expected", None)),
                    deepcopy(getattr(row, "intent", None)),
                    None if meta is None else meta.status,
                    tuple(sorted(_obligations(row))),
                )
            )
        return (
            tuple(rows),
            tuple(snapshot.row_order),
            tuple(snapshot.arguments),
            tuple(snapshot.tombstones),
        )

    @staticmethod
    def _state_has_manual_changes(state: FunctionIterationState) -> bool:
        """Whether a removed function needs an orphan review artifact."""

        by_id = {
            _row_id(row): row
            for row in state.base.rows
            if _row_id(row) is not None
        }
        by_case = {
            _case_name(row): row
            for row in state.base.rows
            if _case_name(row) is not None
        }
        for row in state.working.rows:
            meta = _row_meta(row)
            if meta is not None and (meta.origin == "manual" or meta.status != "active"):
                return True
            old = by_id.get(_row_id(row)) if _row_id(row) is not None else None
            if old is None and _case_name(row) is not None:
                old = by_case.get(_case_name(row))
            if old is None:
                return True
            if (
                old.kind != row.kind
                or old.description != row.description
                or old.values != row.values
                or old.expected != row.expected
                or old.intent != row.intent
            ):
                return True
        working_keys = {_row_id(row) or _case_name(row) for row in state.working.rows}
        for row in state.base.rows:
            key = _row_id(row) or _case_name(row)
            if key not in working_keys:
                return True
        return False

    def _materialize_rows(
        self,
        rows: Iterable[Any],
        *,
        function_key: str,
        source_file: str,
        generation: int,
        force_fresh: bool = False,
    ) -> tuple[TestRow, ...]:
        """Ensure accepted rows have unique persistent IDs and case names."""

        used_ids: set[str] = set()
        used_cases: set[str] = set()
        prefix = _source_prefix(source_file, function_key, rows)
        next_number = 1
        result: list[TestRow] = []
        for source_row in tuple(rows):
            row = deepcopy(source_row)
            meta = _row_meta(row)
            candidate_id = None if force_fresh else _row_id(row)
            if candidate_id is None or not re.fullmatch(r"r_[0-9a-fA-F]{32}", candidate_id) or candidate_id in used_ids:
                candidate_id = _row_id_from_factory(self.uuid_factory, used_ids)
            else:
                used_ids.add(candidate_id)
            candidate_case = None if force_fresh else _case_name(row)
            if (
                candidate_case is None
                or not re.search(r"\.c[0-9]+$", candidate_case)
                or candidate_case in used_cases
            ):
                allocation = allocate_case_name(
                    f"{prefix}.c",
                    next_number,
                    used_cases,
                    on_collision="skip",
                )
                candidate_case = allocation.case_name
                next_number = allocation.next_case_number
            used_cases.add(candidate_case)
            number = _case_number(candidate_case)
            if number is not None:
                next_number = max(next_number, number + 1)
            origin = "generated" if force_fresh or meta is None else meta.origin
            status = "active" if force_fresh or meta is None else meta.status
            created = generation if force_fresh or meta is None else meta.generation_created
            row_meta = _new_meta(
                row,
                row_id=candidate_id,
                case_name=candidate_case,
                function_key=function_key,
                generation=generation,
                origin=origin,
                status=status,
                created=created,
            )
            result.append(_with_meta(row, row_meta))
        return tuple(result)

    def _align_base_working_rows(
        self,
        base_rows: tuple[TestRow, ...],
        working_rows: tuple[TestRow, ...],
        *,
        plan: Mapping[str, Any],
        function_key: str,
        source_file: str,
        generation: int,
    ) -> tuple[TestRow, ...]:
        """Keep matched BASE rows on the same persistent identity as WORKING.

        BASE contains pure NEW values while WORKING contains merged values, but
        the repository identity validator intentionally treats a row identity
        as one entity across both projections.  Align the exact engine matches
        first.  A NEW row without an explicit engine match is always treated
        as a fresh identity; structural similarity is deliberately not enough
        to infer lineage.  Any genuinely new BASE row receives a case number
        outside the WORKING range so two independent rows cannot reuse a case
        suffix.
        """

        working_by_id = {
            _row_id(row): row
            for row in working_rows
            if _row_id(row) is not None
        }
        consumed: set[str] = set()
        matched_by_new_index: dict[int, str] = {}
        for match in plan.get("matches", ()):
            new_index = getattr(match, "new_index", None)
            row_id = getattr(match, "base_row_id", None) or getattr(match, "new_row_id", None)
            if isinstance(new_index, int) and isinstance(row_id, str):
                matched_by_new_index[new_index] = row_id

        working_numbers = {
            number
            for number in (_case_number(_case_name(row)) for row in working_rows)
            if number is not None
        }
        next_number = max(working_numbers, default=0) + 1
        used_cases = {
            case
            for case in (_case_name(row) for row in working_rows)
            if case is not None
        }
        prefix = _source_prefix(source_file, function_key, (*base_rows, *working_rows))
        result: list[TestRow] = []
        for index, row in enumerate(base_rows):
            target: TestRow | None = None
            wanted_id = matched_by_new_index.get(index)
            if wanted_id is not None:
                target = working_by_id.get(wanted_id)
            if target is not None and _row_meta(target) is not None:
                target_meta = _row_meta(target)
                assert target_meta is not None
                consumed.add(target_meta.row_id)
                result.append(
                    _with_meta(
                        row,
                        replace(
                            target_meta,
                            origin="generated",
                            status="active",
                            generation_modified=generation,
                            function_key=function_key,
                            obligation_keys=tuple(sorted(_obligations(row))),
                        ),
                    )
                )
                continue

            meta = _row_meta(row)
            if meta is not None and (
                meta.case_name in used_cases
                or (_case_number(meta.case_name) is not None and _case_number(meta.case_name) in working_numbers)
            ):
                allocation = allocate_case_name(
                    f"{prefix}.c",
                    next_number,
                    used_cases,
                    on_collision="skip",
                )
                next_number = allocation.next_case_number
                meta = replace(meta, case_name=allocation.case_name, generation_modified=generation)
                row = _with_meta(row, meta)
            if meta is not None:
                used_cases.add(meta.case_name)
            result.append(row)
        return tuple(result)


    @staticmethod
    def _coerce_per_function(value: object | None, function_key: str) -> object | None:
        if isinstance(value, Mapping) and function_key in value:
            return value[function_key]
        return value


    def _coerce_stub_states(
        self,
        value: object | None,
        *,
        function_key: str,
        snapshot: StubSnapshot | None = None,
        fallback: tuple[StubState, ...] = (),
    ) -> tuple[StubState, ...]:
        selected = self._coerce_per_function(value, function_key) if value is not None else None
        if selected is None:
            selected = fallback
        if selected is None:
            selected = ()
        if isinstance(selected, StubState):
            return (deepcopy(selected),)
        if isinstance(selected, Mapping):
            if all(isinstance(item, StubState) for item in selected.values()):
                return tuple(deepcopy(item) for item in selected.values())
            if "stub_key" in selected and "function_name" in selected:
                try:
                    return (StubState(**dict(selected)),)
                except Exception:
                    return ()
            return ()
        try:
            result = tuple(selected)  # type: ignore[arg-type]
        except TypeError:
            result = ()
        states = tuple(deepcopy(item) for item in result if isinstance(item, StubState))
        if states:
            return states
        return self._stub_states_from_snapshot(snapshot, function_key)


    def prepare_stub_lifecycle(
        self,
        accepted: AcceptedBundle,
        plans: Mapping[str, dict[str, Any]],
        conflicts: list[MergeConflict],
        *,
        complete: bool,
        function_key: str,
        analysis_reference: AnalysisRef,
        analysis_source: object | None,
        base_stubs: object | None,
        working_stubs: object | None,
        new_stubs: object | None,
        new_stub_snapshot: object | None,
        required_stubs: Iterable[str] | Mapping[str, object] | None,
        deleted_stub_keys: Iterable[str] | None,
        removed_keys: set[str],
        body_validator: Callable[..., bool] | None,
        metrics: Any,
    ) -> tuple[
        tuple[StubState, ...],
        tuple[StubState, ...],
        tuple[StubState, ...],
        StubLifecycleResult,
        bool,
    ]:
        # Build source-derived module stub state.  A direct module tuple is
        # preferred; per-function maps and XML snapshots are accepted as
        # compatibility forms and are normalized into one ownership set.
        all_function_keys = set(plans)
        removed_keys.update(
            key
            for key, plan in plans.items()
            if plan.get("removed")
        )

        def selected(value: object | None, key: str) -> object | None:
            if isinstance(value, Mapping) and key in value:
                return value[key]
            return value

        def gather(value: object | None, snapshots: object | None, fallback: tuple[StubState, ...]) -> tuple[StubState, ...]:
            if value is None:
                if snapshots is None:
                    return tuple(deepcopy(fallback))
                if isinstance(snapshots, StubSnapshot):
                    return self._stub_states_from_snapshot(snapshots, function_key)
                snapshot_values: list[StubState] = []
                if isinstance(snapshots, Mapping):
                    items = snapshots.items()
                else:
                    items = ((key, snapshots) for key in sorted(all_function_keys))
                for raw_key, item in items:
                    key = str(raw_key)
                    snap = item if isinstance(item, StubSnapshot) else None
                    snapshot_values.extend(self._stub_states_from_snapshot(snap, key))
                return tuple(snapshot_values)
            if isinstance(value, Mapping) and not all(isinstance(item, StubState) for item in value.values()):
                collected: list[StubState] = []
                for raw_key, item in value.items():
                    key = str(raw_key)
                    if key not in all_function_keys and isinstance(item, (tuple, list)):
                        continue
                    snap = selected(snapshots, key)
                    collected.extend(self._coerce_stub_states(item, function_key=key, snapshot=snap))
                return tuple(collected)
            return self._coerce_stub_states(value, function_key=function_key, snapshot=(snapshots if isinstance(snapshots, StubSnapshot) else None))

        base_stub_tuple = gather(base_stubs, None, accepted.module_stub_state)
        working_stub_tuple = gather(working_stubs, None, accepted.module_stub_state)
        supplied_new_stub_tuple = gather(new_stubs, new_stub_snapshot, ())
        with metrics.phase("validation"):
            analysis_stub_tuple, analysis_required, analysis_stub_conflicts, analysis_available = self._analysis_stub_states(
                analysis_source,
                analysis_id=analysis_reference.analysis_id,
                module_key=accepted.module_key,
            )
        conflicts.extend(analysis_stub_conflicts)
        if analysis_available:
            # Analysis DBs describe source signatures/ownership, not mutable
            # Stub Body text.  Seed each decoded NEW schema record with the
            # corresponding generator BASE body so a user-edited WORKING body
            # is merged against a neutral source baseline rather than being
            # mistaken for a source body rewrite to the empty placeholder.
            base_by_alias: dict[str, StubState] = {}
            for state in base_stub_tuple:
                for alias in {state.stub_key, state.function_name}:
                    base_by_alias.setdefault(alias, state)
            analysis_stub_tuple = tuple(
                replace(
                    state,
                    body_raw=next(
                        (
                            base_by_alias[alias].body_raw
                            for alias in (state.stub_key, state.function_name)
                            if alias in base_by_alias
                        ),
                        "",
                    ),
                )
                for state in analysis_stub_tuple
            )
            # A normalized explicit tuple is accepted only as a consistency
            # check.  The immutable analysis DB remains the sole source of
            # NEW signatures and module-wide ownership.
            if (new_stubs is not None or new_stub_snapshot is not None) and not self._stub_inputs_agree(
                analysis_stub_tuple,
                supplied_new_stub_tuple,
            ):
                conflicts.append(
                    self._conflict(
                        module_key=accepted.module_key,
                        function_key=function_key,
                        code="ANALYSIS_DB_MISMATCH",
                        message="explicit NEW stub state disagrees with ANALYSIS_NEW signatures or ownership",
                    )
                )
            new_stub_tuple = analysis_stub_tuple
            effective_required_stubs: Iterable[str] | Mapping[str, object] | None = (
                required_stubs if required_stubs is not None else analysis_required
            )
        else:
            new_stub_tuple = supplied_new_stub_tuple
            effective_required_stubs = required_stubs
        if not analysis_available and new_stubs is None and not new_stub_tuple:
            # No stub projection/input means no source-stub lifecycle delta.
            if complete:
                # A complete source-function map is enough to remove owners
                # which no longer exist, even when a compatibility caller did
                # not provide a separate NEW stub projection.  Do not retain
                # a sole removed-function stub as an active record.
                fallback_states: list[StubState] = []
                for state in base_stub_tuple:
                    owners = frozenset(owner for owner in state.owners if owner not in removed_keys)
                    if owners:
                        fallback_states.append(replace(deepcopy(state), owners=owners))
                new_stub_tuple = tuple(fallback_states)
            else:
                new_stub_tuple = tuple(deepcopy(base_stub_tuple))
        try:
            with metrics.phase("matching"):
                stub_result = merge_stub_states(
                    base_stub_tuple,
                    working_stub_tuple,
                    new_stub_tuple,
                    function_key=function_key,
                    required_stubs=effective_required_stubs,
                    deleted_stub_keys=deleted_stub_keys,
                    removed_function_keys=removed_keys,
                    body_validator=body_validator,
                    # An immutable analysis model is module-wide even when the
                    # caller requested one function.  Its NEW ownership union is
                    # therefore authoritative; only compatibility inputs without
                    # a decoded DB retain the function-scoped preservation rule.
                    scope_complete=bool(complete or analysis_available),
                )
        except Exception as exc:
            conflicts.append(
                self._conflict(
                    module_key=accepted.module_key,
                    function_key=function_key,
                    code="ORCHESTRATION_STUB_FAILED",
                    message=f"stub lifecycle failed before acceptance: {exc}",
                )
            )
            stub_result = StubLifecycleResult(tuple(working_stub_tuple), (), False)
        conflicts.extend(stub_result.conflicts)

        def signature_map(states: Iterable[StubState]) -> dict[str, tuple[Any, ...]]:
            return {
                state.stub_key: stub_signature_tuple(state)
                for state in states
            }

        source_stub_changed = signature_map(base_stub_tuple) != signature_map(new_stub_tuple)
        ownership_changed = {
            state.stub_key: tuple(sorted(state.owners))
            for state in base_stub_tuple
        } != {
            state.stub_key: tuple(sorted(state.owners))
            for state in new_stub_tuple
        }
        if source_stub_changed or ownership_changed:
            owners = {
                owner
                for state in (*base_stub_tuple, *new_stub_tuple)
                for owner in state.owners
            }
            affected = {key for key in plans if key in owners and not plans[key].get("removed")}
            if not affected:
                affected = {key for key in plans if not plans[key].get("removed")}
            for key in affected:
                plans[key]["code_changed"] = True
                plans[key]["state_changed"] = True
        return base_stub_tuple, working_stub_tuple, new_stub_tuple, stub_result, analysis_available


    def assemble_candidate(
        self,
        accepted: AcceptedBundle,
        plans: Mapping[str, Mapping[str, Any]],
        removed_reviews: Iterable[Mapping[str, Any]],
        *,
        versions: Mapping[str, DisplayVersion],
        audit: AuditEvent | None,
        generation: int,
        base_snapshot_id: str,
        working_snapshot_id: str,
        effective_source: str,
        analysis_reference: AnalysisRef,
        analysis_available: bool,
        analysis_stub_tuple: tuple[StubState, ...],
        new_stubs: object | None,
        stub_result: StubLifecycleResult,
        changed_stub_aliases: set[str],
        conflicts: Iterable[MergeConflict],
        operation_id: str,
    ) -> CandidateBundle:
        # ``prepare_stub_lifecycle`` normalizes all NEW stub inputs into this
        # tuple.  Keep that normalized value explicit at the candidate
        # boundary; this method is intentionally a collaborator and must not
        # reach into the caller's closure for ``new_stub_tuple``.  The raw
        # ``new_stubs`` value is retained only for the historical rename
        # compatibility rule below (a supplied source projection permits the
        # corresponding renamed artifact to be derived).
        new_stub_tuple = tuple(analysis_stub_tuple)
        final_states: list[FunctionIterationState] = []
        artifact_plans: dict[str, dict[str, Any]] = {}
        review_records = list(removed_reviews)
        for key in sorted(plans):
            plan = plans[key]
            if plan.get("removed"):
                continue
            old_state = plan.get("old")
            base_raw = plan.get("base_raw")
            working_raw = plan.get("working_raw")
            if not isinstance(base_raw, BaseSnapshot):
                continue
            is_added = bool(plan.get("added"))
            base_source_rows = tuple(
                row for row in base_raw.rows
                if _row_meta(row) is None or _row_meta(row).status == "active"
            )
            base_rows = self._materialize_rows(
                base_source_rows,
                function_key=key,
                source_file=effective_source,
                generation=generation,
                force_fresh=is_added,
            )
            if is_added:
                working_rows = tuple(deepcopy(row) for row in base_rows)
                working_raw = None
                tombstones = ()
            else:
                if not isinstance(working_raw, WorkingSnapshot):
                    working_raw = old_state.working if old_state is not None else None
                raw_rows = () if working_raw is None else tuple(working_raw.rows)
                orphan_rows = tuple(
                    row for row in raw_rows
                    if _row_meta(row) is not None and _row_meta(row).status != "active"
                )
                if orphan_rows:
                    review_records.append(
                        {
                            "function_key": key,
                            "reason": "manual/orphan rows retained for review",
                            "rows": orphan_rows,
                        }
                    )
                active_source_rows = tuple(
                    row for row in raw_rows
                    if _row_meta(row) is None or _row_meta(row).status == "active"
                )
                working_rows = self._materialize_rows(
                    active_source_rows,
                    function_key=key,
                    source_file=effective_source,
                    generation=generation,
                    force_fresh=False,
                )
                base_rows = self._align_base_working_rows(
                    tuple(base_rows),
                    tuple(working_rows),
                    plan=plan,
                    function_key=key,
                    source_file=effective_source,
                    generation=generation,
                )
                tombstones = () if working_raw is None else working_raw.tombstones
            row_order = tuple(meta.row_id for meta in (_row_meta(row) for row in working_rows) if meta is not None)
            args = plan.get("new_arguments")
            if not isinstance(args, tuple) or not all(isinstance(item, ArgumentState) for item in args):
                args = old_state.working.arguments if old_state is not None else ()
            base_final = replace(
                base_raw,
                snapshot_id=base_snapshot_id,
                generation=generation,
                analysis_id=analysis_reference.analysis_id,
                rows=tuple(base_rows),
            )
            working_final = WorkingSnapshot(
                snapshot_id=working_snapshot_id,
                generation=generation,
                analysis_id=analysis_reference.analysis_id,
                row_order=row_order,
                rows=tuple(working_rows),
                arguments=tuple(args),
                tombstones=tuple(tombstones),
            )
            function_name = str(plan.get("new_name") or (old_state.function if old_state is not None else key))
            next_number = old_state.next_case_number if old_state is not None else 1
            for row in (*base_rows, *working_rows):
                number = _case_number(_case_name(row))
                if number is not None:
                    next_number = max(next_number, number + 1)
            version = versions[key]
            state = FunctionIterationState(
                function_key=key,
                source_file=effective_source,
                function=function_name,
                next_case_number=next_number,
                accepted_analysis_id=analysis_reference.analysis_id,
                display_version=version,
                base=base_final,
                working=working_final,
                schema_version=(old_state.schema_version if old_state is not None else 2),
            )
            final_states.append(state)

            # Value XML has two different semantic projections.  BASE is
            # the pure NEW generator result; WORKING is the merged state
            # (which may retain user-edited cells).  A single NEW XML
            # payload must therefore be re-projected for WORKING instead
            # of being reused verbatim for both slots.  The same display
            # version is supplied later by ``_merge_b_artifacts``.
            new_value = plan.get("new_value")
            base_value_source = self._snapshot_for_slot(new_value, "base", "value")
            working_value_source = self._snapshot_for_slot(new_value, "working", "value")
            old_value_base = plan.get("old_value_base")
            old_value_working = plan.get("old_value_working")
            if base_value_source is None and not (plan.get("renamed") and new_value is None):
                base_value_source = old_value_base or old_value_working
            if isinstance(base_value_source, FunctionSnapshot):
                base_value = self._project_value_snapshot(
                    base_value_source,
                    base_rows,
                    fallback=old_value_base if isinstance(old_value_base, FunctionSnapshot) else None,
                )
            else:
                base_value = base_value_source
            if working_value_source is None and not (plan.get("renamed") and new_value is None):
                working_value_source = base_value_source or old_value_working or base_value
            if isinstance(working_value_source, FunctionSnapshot):
                working_value = self._project_value_snapshot(
                    working_value_source,
                    working_rows,
                    fallback=old_value_working if isinstance(old_value_working, FunctionSnapshot) else None,
                    strict_expected=True,
                )
            else:
                working_value = working_value_source

            # Stub XML carries signature/ownership compatibility metadata;
            # arbitrary Stub Body text is persisted in module stub state.
            # Keep the slot selection explicit nevertheless.  A NEW
            # snapshot is a complete payload template for BASE, while an
            # accepted WORKING snapshot remains the template for the
            # merged WORKING projection.  Signature fields are overlaid
            # without reconstructing raw called-function payloads.
            new_stub = plan.get("new_stub")
            new_base_stub_template = self._snapshot_for_slot(new_stub, "base", "stub")
            # A plain NEW StubSnapshot is only a BASE/source payload.  If
            # it were selected for WORKING as well, an accepted WORKING
            # payload containing user/unknown XML children could be
            # silently replaced.  WORKING may use a NEW template only
            # when the caller explicitly supplied a per-slot working
            # projection; otherwise the accepted WORKING template below
            # is authoritative and NEW is merely a fallback for calls
            # that did not exist in that payload.
            new_working_stub_template = None
            if isinstance(new_stub, Mapping):
                for working_key in ("working_stub", "workingStub", "working"):
                    if working_key in new_stub and new_stub[working_key] is not None:
                        new_working_stub_template = new_stub[working_key]
                        break
            old_base_stub_template = plan.get("old_stub_base")
            old_working_stub_template = plan.get("old_stub_working")
            base_stub: StubSnapshot | None = None
            working_stub: StubSnapshot | None = None
            can_derive_stub_xml = bool(
                new_stub_tuple
                or old_base_stub_template is not None
                or old_working_stub_template is not None
                or new_stub is not None
            )
            if (
                can_derive_stub_xml
                and not (plan.get("renamed") and not (analysis_available or new_stubs is not None))
            ):
                # Explicit NEW XML wins for the BASE payload.  For
                # WORKING, prefer the accepted payload and use NEW as a
                # fallback for newly-added called functions.
                base_template = (
                    new_base_stub_template
                    or old_base_stub_template
                    or old_working_stub_template
                )
                working_template = (
                    old_working_stub_template
                    or new_working_stub_template
                    or new_base_stub_template
                    or old_base_stub_template
                )
                base_stub = self._stub_snapshot_from_states(
                    new_stub_tuple,
                    function_key=key,
                    function_name=function_name,
                    source_file=effective_source,
                    template=base_template if isinstance(base_template, StubSnapshot) else None,
                    fallback_template=(
                        new_working_stub_template
                        if isinstance(new_working_stub_template, StubSnapshot)
                        and new_working_stub_template is not base_template
                        else None
                    ),
                    require_payload_templates=changed_stub_aliases,
                )
                working_stub = self._stub_snapshot_from_states(
                    stub_result.states,
                    function_key=key,
                    function_name=function_name,
                    source_file=effective_source,
                    template=working_template if isinstance(working_template, StubSnapshot) else None,
                    fallback_template=(
                        new_base_stub_template
                        if isinstance(new_base_stub_template, StubSnapshot)
                        and new_base_stub_template is not working_template
                        else None
                    ),
                    require_payload_templates=changed_stub_aliases,
                )
            
            # A parseable XML document with another function's identity is
            # still the wrong artifact.  Reject it before staging so a
            # path/name collision cannot make the repository accept a
            # semantically unrelated projection.
            allowed_identities = {key, function_name}
            for slot, role, snapshot in (
                ("base", "value", base_value),
                ("working", "value", working_value),
                ("base", "stub", base_stub),
                ("working", "stub", working_stub),
            ):
                identity = None
                if role == "value" and isinstance(snapshot, FunctionSnapshot):
                    identity = snapshot.name
                elif role == "stub" and isinstance(snapshot, StubSnapshot):
                    identity = snapshot.function_name
                if identity is not None and identity not in allowed_identities:
                    raise MergeBProjectionError(
                        "IDENTITY_FUNCTION_MISMATCH",
                        f"{slot} {role} snapshot belongs to {identity!r}, expected one of {sorted(allowed_identities)!r}",
                    )
            artifact_plans[key] = {
                **plan,
                "base_value_snapshot": base_value,
                "working_value_snapshot": working_value,
                "base_stub_snapshot": base_stub,
                "working_stub_snapshot": working_stub,
                # Keep the exact state projections used to build the XML
                # beside the typed snapshots.  The artifact pass below
                # verifies that both wire views still represent these
                # candidate states after rendering.
                "base_arguments": tuple(args),
                "working_arguments": tuple(args),
                "base_stub_states": tuple(
                    state_item
                    for state_item in new_stub_tuple
                    if state_item.status == "active"
                    and (
                        not state_item.owners
                        or key in state_item.owners
                        or function_name in state_item.owners
                    )
                ),
                "working_stub_states": tuple(
                    state_item
                    for state_item in stub_result.states
                    if state_item.status == "active"
                    and (
                        not state_item.owners
                        or key in state_item.owners
                        or function_name in state_item.owners
                    )
                ),
                "base_rows": tuple(base_rows),
                "working_rows": tuple(working_rows),
                "version": version,
            }
        if not final_states:
            raise MergeAServiceError("Merge B candidate contains no function states")
        versions_by_name = {
            state.function: state.display_version
            for state in final_states
        }
        module_manifest = replace(
            accepted.module_manifest,
            source_file=effective_source,
            accepted_generation=generation,
            base_snapshot_id=base_snapshot_id,
            working_snapshot_id=working_snapshot_id,
            analysis=analysis_reference,
            function_versions=versions_by_name,
        )
        artifacts = self._merge_b_artifacts(
            accepted,
            artifact_plans,
            review_records,
            versions=versions,
            audit=audit,
        )
        return CandidateBundle(
            accepted.module_key,
            module_manifest,
            tuple(final_states),
            artifacts,
            tuple(stub_result.states),
            tuple(item for item in conflicts if item.severity != "blocking"),
            operation_id,
        )


    def _invoke_merge_b_engine(
        self,
        base: FunctionIterationState,
        new: BaseSnapshot,
        *,
        function_key: str,
        source_file: str,
        base_arguments: object | None,
        new_arguments: object | None,
        conflict_resolver: Callable[[MergeConflict], str | None] | None = None,
    ) -> Any:
        """Call production or lightweight MergeB engines compatibly."""

        method = self.engine.merge
        kwargs: dict[str, Any] = {
            "function_key": function_key,
            "source_file": source_file,
            "base_arguments": deepcopy(base_arguments),
            "new_arguments": deepcopy(new_arguments),
            "conflict_resolver": conflict_resolver,
        }
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            with self.metrics.phase("matching"):
                return method(deepcopy(base), deepcopy(base.working), deepcopy(new), **kwargs)
        parameters = signature.parameters.values()
        accepts_keywords = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters)
        if not accepts_keywords:
            names = {
                item.name
                for item in parameters
                if item.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            }
            kwargs = {name: value for name, value in kwargs.items() if name in names}
        # Deep-copy the complete state before invoking a custom engine.  A
        # failed/ill-behaved engine therefore cannot mutate accepted nested
        # values before the repository transaction boundary.
        with self.metrics.phase("matching"):
            return method(deepcopy(base), deepcopy(base.working), deepcopy(new), **kwargs)


    def _prepare_function_plans(
        self,
        accepted: AcceptedBundle,
        new_map: Mapping[str, BaseSnapshot],
        *,
        complete: bool,
        function_key: str,
        source_file: str | None,
        new_value_snapshot: object | None,
        new_stub_snapshot: object | None,
        value_snapshot: FunctionSnapshot | None,
        stub_snapshot: StubSnapshot | None,
        removed_function_keys: Iterable[str] | None,
        function_names: Mapping[str, str] | None,
        base_arguments: object | None,
        new_arguments: object | None,
        resolution_registry: Any | None,
        resolution_decisions: Mapping[str, ResolutionDecision] | Iterable[ResolutionDecision] | None,
        resolution_context: ResolutionContext | Mapping[str, ResolutionContext] | None,
        resolution_applier: Callable[[MergeConflict, ResolutionDecision, object | None], object] | None,
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[MergeConflict],
        list[dict[str, Any]],
        dict[str, FunctionIterationState],
        set[str],
        str,
    ]:
        old_states = {state.function_key: state for state in accepted.function_states}
        effective_source = source_file or accepted.module_manifest.source_file
        names = dict(function_names or {})
        conflicts: list[MergeConflict] = []
        plans: dict[str, dict[str, Any]] = {}
        removed_reviews: list[dict[str, Any]] = []
        removed_keys = {str(item) for item in (removed_function_keys or ()) if str(item)}

        def selected(value: object | None, key: str) -> object | None:
            if isinstance(value, Mapping) and key in value:
                return value[key]
            return value

        selected_new_value = lambda key: selected(new_value_snapshot, key)
        selected_new_stub = lambda key: selected(new_stub_snapshot, key)

        # A single-function invocation is intentionally scoped.  A complete
        # source map additionally enables exact removed-function policy.
        keys = set(old_states) | set(new_map)
        if not complete:
            keys.update(key for key in new_map)

        for key in sorted(keys):
            old_state = old_states.get(key)
            fresh = new_map.get(key)
            if fresh is None:
                if not complete:
                    # Omitted from a targeted invocation means "not touched".
                    if old_state is not None:
                        plans[key] = {
                            "key": key,
                            "old": old_state,
                            "new": None,
                            "working_raw": old_state.working,
                            "base_raw": old_state.base,
                            "new_arguments": old_state.working.arguments,
                            "new_value": None,
                            "new_stub": None,
                            "old_value_base": self._snapshot_projection(
                                accepted, key, "value", "base", self.value_adapter.read
                            ),
                            "old_value_working": self._snapshot_projection(
                                accepted, key, "value", "working", self.value_adapter.read
                            ) or selected(value_snapshot, key),
                            "old_stub_base": self._snapshot_projection(
                                accepted, key, "stub", "base", self.stub_adapter.read
                            ),
                            "old_stub_working": self._snapshot_projection(
                                accepted, key, "stub", "working", self.stub_adapter.read
                            ) or selected(stub_snapshot, key),
                            "added": False,
                            "removed": False,
                            "code_changed": False,
                            "state_changed": False,
                        }
                    continue
                # Complete maps classify source removals below.
                if old_state is None:
                    continue
                manual = self._state_has_manual_changes(old_state)
                if manual:
                    removed_reviews.append(
                        {
                            "function_key": key,
                            "function": old_state.function,
                            "reason": "source function removed; manual state preserved for review",
                            "state": old_state,
                        }
                    )
                plans[key] = {
                    "key": key,
                    "old": old_state,
                    "new": None,
                    "working_raw": None,
                    "base_raw": None,
                    "new_arguments": None,
                    "new_value": None,
                    "new_stub": None,
                    "old_value_base": self._snapshot_projection(
                        accepted, key, "value", "base", self.value_adapter.read
                    ),
                    "old_value_working": self._snapshot_projection(
                        accepted, key, "value", "working", self.value_adapter.read
                    ),
                    "old_stub_base": self._snapshot_projection(
                        accepted, key, "stub", "base", self.stub_adapter.read
                    ),
                    "old_stub_working": self._snapshot_projection(
                        accepted, key, "stub", "working", self.stub_adapter.read
                    ),
                    "added": False,
                    "removed": True,
                    "manual_removed": manual,
                    "code_changed": True,
                    "state_changed": True,
                }
                continue

            # Validate that a NEW row carrying function provenance does not
            # silently migrate to another function key.
            for row in fresh.rows:
                meta = _row_meta(row)
                if meta is not None and meta.function_key != key:
                    conflicts.append(
                        _merge_b_conflict(
                            module_key=accepted.module_key,
                            function_key=key,
                            code="IDENTITY_FUNCTION_MISMATCH",
                            entity_type="row",
                            row_id=meta.row_id,
                            case_name=meta.case_name,
                            message=f"NEW row belongs to function {meta.function_key!r}, expected {key!r}",
                            user=meta.function_key,
                            new=key,
                        )
                    )

            old_name = None if old_state is None else old_state.function
            new_name = names.get(key) or getattr(fresh, "function", None) or getattr(fresh, "function_name", None) or old_name or key
            renamed = old_state is not None and old_name != new_name
            old_base_value = self._snapshot_projection(
                accepted, key, "value", "base", self.value_adapter.read
            )
            old_working_value = self._snapshot_projection(
                accepted, key, "value", "working", self.value_adapter.read
            ) or selected(value_snapshot, key)
            old_base_stub = self._snapshot_projection(
                accepted, key, "stub", "base", self.stub_adapter.read
            )
            old_working_stub = self._snapshot_projection(
                accepted, key, "stub", "working", self.stub_adapter.read
            ) or selected(stub_snapshot, key)
            # A NEW value XML snapshot is also the generator's schema input.
            # Normalize it before invoking Merge B so omission of the
            # explicit ``new_arguments`` parameter cannot make the engine
            # fall back to the accepted WORKING schema.
            explicit_new_arguments = selected(new_arguments, key)
            new_value_input = self._snapshot_for_slot(
                selected_new_value(key), "base", "value"
            )
            expected_new_identities = {key, str(new_name)}
            if old_name:
                expected_new_identities.add(str(old_name))
            new_value_identity_conflict = (
                isinstance(new_value_input, FunctionSnapshot)
                and new_value_input.name not in expected_new_identities
            )
            if new_value_identity_conflict:
                # Report the root identity first.  Do not emit secondary
                # schema diagnostics from an unrelated function's argument
                # payload; those would obscure the deterministic ownership
                # failure and could make a valid schema look malformed.
                conflicts.append(
                    _merge_b_conflict(
                        module_key=accepted.module_key,
                        function_key=key,
                        code="IDENTITY_FUNCTION_MISMATCH",
                        entity_type="value_xml",
                        message=(
                            f"NEW value XML belongs to {new_value_input.name!r}, "
                            f"expected one of {sorted(expected_new_identities)!r}"
                        ),
                        user=new_value_input.name,
                        new=sorted(expected_new_identities),
                    )
                )
                normalized_new_arguments, argument_diagnostics = None, ()
            else:
                normalized_new_arguments, argument_diagnostics = self._argument_states_from_value_snapshot(
                    new_value_input
                )
            effective_new_arguments = explicit_new_arguments
            if normalized_new_arguments is not None:
                # The XML-derived tuple is authoritative.  An explicit tuple
                # is accepted only as an exact consistency check.
                effective_new_arguments = normalized_new_arguments
                explicit_projection = self._argument_schema_projection(explicit_new_arguments)
                if explicit_new_arguments is not None and explicit_projection is None:
                    argument_diagnostics = (
                        *argument_diagnostics,
                        (
                            "ARGUMENT_SCHEMA_CONFLICT",
                            "explicit new_arguments must be a tuple of ArgumentState records when NEW value XML is supplied",
                            None,
                        ),
                    )
                elif (
                    explicit_new_arguments is not None
                    and explicit_projection != self._argument_schema_projection(normalized_new_arguments)
                ):
                    argument_diagnostics = (
                        *argument_diagnostics,
                        (
                            "ARGUMENT_SCHEMA_CONFLICT",
                            "explicit new_arguments disagrees with NEW value XML schema",
                            None,
                        ),
                    )
                for diagnostic_code, diagnostic_message, diagnostic_key in argument_diagnostics:
                    conflicts.append(
                        _merge_b_conflict(
                            module_key=accepted.module_key,
                            function_key=key,
                            code=diagnostic_code,
                            entity_type="argument_schema",
                            logical_argument_key=(
                                str(diagnostic_key) if diagnostic_key is not None else None
                            ),
                            base=(old_state.working.arguments if old_state is not None else None),
                            user=explicit_new_arguments,
                            new=normalized_new_arguments,
                            message=diagnostic_message,
                        )
                    )
            plan: dict[str, Any] = {
                "key": key,
                "old": old_state,
                "new": fresh,
                "new_name": str(new_name),
                "new_arguments": effective_new_arguments,
                "new_value": selected_new_value(key),
                "new_stub": selected_new_stub(key),
                "old_value_base": old_base_value,
                "old_value_working": old_working_value,
                "old_stub_base": old_base_stub,
                "old_stub_working": old_working_stub,
                "added": old_state is None or renamed,
                "removed": False,
                "renamed": renamed,
                "code_changed": old_state is None or renamed,
                "state_changed": old_state is None or renamed,
            }

            if old_state is None or renamed:
                # Added/renamed functions are never matched against historical
                # rows.  Their BASE and WORKING identities are allocated fresh
                # during candidate materialization.
                plan["working_raw"] = None
                plan["base_raw"] = fresh
                plan["new_arguments"] = effective_new_arguments
                if renamed and old_state is not None:
                    # ``old`` is cleared below so the renamed function is
                    # materialized as a fresh add.  Keep the prior state
                    # separately for accepted-artifact evidence checks: an
                    # old XML projection may be discarded as remove+add, but
                    # stale metadata/state must still be diagnosed before it
                    # is silently dropped.
                    plan["old_artifact_state"] = old_state
                    plan["removed_identities"] = {
                        old_state.function_key,
                        old_state.function,
                    }
                    if self._state_has_manual_changes(old_state):
                        removed_reviews.append(
                            {
                                "function_key": key,
                                "function": old_state.function,
                                "reason": "function rename defaults to remove+add; old identity archived",
                                "state": old_state,
                            }
                        )
                    plan["old"] = None
                plans[key] = plan
                continue

            base_args = selected(base_arguments, key)
            if base_args is None:
                base_args = old_state.working.arguments
            new_args = effective_new_arguments
            resolution_active = any(
                item is not None
                for item in (
                    resolution_registry,
                    resolution_decisions,
                    resolution_context,
                    resolution_applier,
                )
            )
            resolution_errors: list[tuple[MergeConflict, ResolutionError]] = []

            def resolve_engine_conflict(
                conflict: MergeConflict,
                *,
                _old_state: FunctionIterationState = old_state,
                _fresh: BaseSnapshot = fresh,
                _key: str = key,
            ) -> str | None:
                """Select only a persisted value action for pure Merge B."""

                if not resolution_active:
                    return None
                try:
                    context = _resolution_context_for_conflict(
                        resolution_context,
                        conflict,
                        module_key=accepted.module_key,
                        base_snapshot_id=_old_state.base.snapshot_id,
                        working_snapshot_id=_old_state.working.snapshot_id,
                        new_snapshot_id=_fresh.snapshot_id,
                        revision=accepted.revision,
                    )
                    decision = decision_for_conflict(
                        conflict,
                        accepted.module_key,
                        context,
                        registry=resolution_registry,
                        decisions=resolution_decisions,
                    )
                    if decision is None:
                        return None
                    if resolution_applier is not None:
                        raise ResolutionConflictError(
                            "Merge B resolution_applier cannot create a candidate before engine merge"
                        )
                    if decision.action not in conflict.allowed_resolutions:
                        raise ResolutionConflictError(
                            "resolution action is not in conflict.allowed_resolutions"
                        )
                    action = decision.action.casefold()
                    if action in {"keep_user", "keep_new"}:
                        return action
                    raise ResolutionConflictError(
                        "resolution action has no deterministic pure Merge B semantics"
                    )
                except ResolutionError as exc:
                    resolution_errors.append((conflict, exc))
                    return None

            try:
                result = self._invoke_merge_b_engine(
                    old_state,
                    fresh,
                    function_key=key,
                    source_file=effective_source,
                    base_arguments=base_args,
                    new_arguments=new_args,
                    conflict_resolver=(resolve_engine_conflict if resolution_active else None),
                )
            except Exception as exc:
                conflicts.append(
                    _merge_b_conflict(
                        module_key=accepted.module_key,
                        function_key=key,
                        code="ORCHESTRATION_MERGE_FAILED",
                        message=f"Merge B engine failed before acceptance: {exc}",
                    )
                )
                continue

            if isinstance(result, MergeBResult):
                result_conflicts = tuple(result.conflicts)
                candidate_working = result.candidate_working
                result_changed = bool(result.changed)
            else:
                result_conflicts = tuple(getattr(result, "conflicts", ()) or ())
                candidate_working = getattr(result, "candidate_working", None)
                if candidate_working is None:
                    candidate_working = getattr(result, "candidate", None)
                result_changed = bool(getattr(result, "changed", False))
            if resolution_errors:
                result_conflicts = (
                    *result_conflicts,
                    *tuple(
                        _resolution_failure_conflict(
                            conflict,
                            _resolution_conflict_code(error),
                            str(error),
                        )
                        for conflict, error in resolution_errors
                    ),
                )
            if not all(isinstance(item, MergeConflict) for item in result_conflicts):
                conflicts.append(
                    _merge_b_conflict(
                        module_key=accepted.module_key,
                        function_key=key,
                        code="ORCHESTRATION_INVALID_RESULT",
                        message="Merge B engine returned non-MergeConflict diagnostics",
                    )
                )
                continue
            conflicts.extend(result_conflicts)
            if not isinstance(candidate_working, WorkingSnapshot):
                if not result_conflicts:
                    conflicts.append(
                        _merge_b_conflict(
                            module_key=accepted.module_key,
                            function_key=key,
                            code="ORCHESTRATION_EMPTY_CANDIDATE",
                            message="Merge B engine returned no WorkingSnapshot candidate",
                        )
                    )
                continue
            plan["working_raw"] = candidate_working
            plan["base_raw"] = fresh
            plan["matches"] = tuple(getattr(result, "matches", ()) or ())
            plan["new_arguments"] = new_args
            plan["state_changed"] = self._working_payload(candidate_working) != self._working_payload(old_state.working)
            plan["code_changed"] = self._pure_snapshot_changed(
                old_state.base,
                fresh,
                old_state.working.arguments,
                new_args,
            )
            # ``result.changed`` includes a generator baseline change even
            # when rows are equivalent.  Keep that signal when fingerprints
            # are absent; otherwise use normalized per-function comparison to
            # avoid bumping unrelated Test Objects.
            if result_changed and old_state.base.analysis_id == fresh.analysis_id:
                plan["code_changed"] = plan["code_changed"] or not self._working_payload(candidate_working) == self._working_payload(old_state.working)
            plans[key] = plan

        return plans, conflicts, removed_reviews, old_states, removed_keys, effective_source


    def _merge_loaded(
        self,
        operation_id: str,
        accepted: AcceptedBundle,
        new_map: Mapping[str, BaseSnapshot],
        *,
        complete: bool,
        function_key: str,
        source_file: str | None,
        analysis_reference: AnalysisRef,
        analysis_source: object | None,
        old_analysis_id: str,
        value_snapshot: FunctionSnapshot | None,
        new_value_snapshot: object | None,
        stub_snapshot: StubSnapshot | None,
        new_stub_snapshot: object | None,
        base_stubs: object | None,
        working_stubs: object | None,
        new_stubs: object | None,
        required_stubs: Iterable[str] | Mapping[str, object] | None,
        deleted_stub_keys: Iterable[str] | None,
        removed_function_keys: Iterable[str] | None,
        function_names: Mapping[str, str] | None,
        base_arguments: object | None,
        new_arguments: object | None,
        body_validator: Callable[..., bool] | None,
        resolution_registry: Any | None,
        resolution_decisions: Mapping[str, ResolutionDecision] | Iterable[ResolutionDecision] | None,
        resolution_context: ResolutionContext | Mapping[str, ResolutionContext] | None,
        resolution_applier: Callable[[MergeConflict, ResolutionDecision, object | None], object] | None,
    ) -> MergeOutcome:
        """Build and (in enforce mode) atomically accept a Merge B bundle."""
        effective_source = source_file or accepted.module_manifest.source_file
        (
            plans,
            conflicts,
            removed_reviews,
            old_states,
            removed_keys,
            effective_source,
        ) = self._prepare_function_plans(
            accepted,
            new_map,
            complete=complete,
            function_key=function_key,
            source_file=source_file,
            new_value_snapshot=new_value_snapshot,
            new_stub_snapshot=new_stub_snapshot,
            value_snapshot=value_snapshot,
            stub_snapshot=stub_snapshot,
            removed_function_keys=removed_function_keys,
            function_names=function_names,
            base_arguments=base_arguments,
            new_arguments=new_arguments,
            resolution_registry=resolution_registry,
            resolution_decisions=resolution_decisions,
            resolution_context=resolution_context,
            resolution_applier=resolution_applier,
        )

        def selected(value: object | None, key: str) -> object | None:
            if isinstance(value, Mapping) and key in value:
                return value[key]
            return value


        (
            base_stub_tuple,
            working_stub_tuple,
            new_stub_tuple,
            stub_result,
            analysis_available,
        ) = self.prepare_stub_lifecycle(
            accepted,
            plans,
            conflicts,
            complete=complete,
            function_key=function_key,
            analysis_reference=analysis_reference,
            analysis_source=analysis_source,
            base_stubs=base_stubs,
            working_stubs=working_stubs,
            new_stubs=new_stubs,
            new_stub_snapshot=new_stub_snapshot,
            required_stubs=required_stubs,
            deleted_stub_keys=deleted_stub_keys,
            removed_keys=removed_keys,
            body_validator=body_validator,
            metrics=self.metrics,
        )

        analysis_stub_tuple = new_stub_tuple

        # A changed source signature must not fall through to the adapter's
        # legacy bare-parameter constructor when neither accepted XML slot has
        # a lossless called-function template.  Keep additions without a
        # prior signature compatible with the existing state-only path, but
        # make every existing signature change fail closed in the projection
        # helper below.
        changed_stub_aliases: set[str] = set()
        base_stub_by_alias: dict[str, StubState] = {}
        for state in base_stub_tuple:
            for alias in {state.stub_key, state.function_name} - {""}:
                base_stub_by_alias.setdefault(alias, state)
        for state in new_stub_tuple:
            previous = next(
                (
                    base_stub_by_alias[alias]
                    for alias in (state.stub_key, state.function_name)
                    if alias in base_stub_by_alias
                ),
                None,
            )
            if previous is not None and self._stub_signature_tuple(previous) != self._stub_signature_tuple(state):
                changed_stub_aliases.update({state.stub_key, state.function_name} - {""})

        blocking = tuple(item for item in conflicts if item.severity == "blocking")
        if blocking:
            return self._failure(
                operation_id,
                accepted.module_key,
                function_key,
                accepted,
                tuple(conflicts),
                "blocking_conflict",
                semantic_change=False,
            )

        code_changed = {
            key for key, plan in plans.items()
            if plan.get("code_changed") and not plan.get("removed")
        }
        lifecycle_changed = any(plan.get("added") or plan.get("removed") for plan in plans.values())
        working_changed = any(plan.get("state_changed") for plan in plans.values())
        stub_changed = tuple(stub_result.states) != tuple(accepted.module_stub_state)
        analysis_changed = analysis_reference != accepted.module_manifest.analysis
        source_identity_changed = effective_source != accepted.module_manifest.source_file
        semantic_change = bool(code_changed or lifecycle_changed or working_changed or stub_changed or analysis_changed or source_identity_changed)
        if not semantic_change:
            previous = None
            for state in accepted.function_states:
                if state.function_key == function_key or function_key == "module":
                    previous = state.display_version
                    break
            event = self._event(
                operation_id,
                accepted.module_key,
                function_key or "module",
                accepted.revision,
                None,
                previous,
                previous,
                False,
                "noop",
                "source regeneration has no semantic change",
                0,
                conflict_codes=tuple(item.code for item in conflicts),
                validation_ok=None,
            )
            return MergeOutcome(None, False, tuple(conflicts), (event,), self.mode, False, accepted, event.reason)

        # Build one candidate bundle.  IDs are allocated only after deciding
        # that a semantic operation exists; no-op runs therefore cannot consume
        # persistent row/snapshot identities.
        def build_candidate(
            versions: Mapping[str, DisplayVersion],
            *,
            audit: AuditEvent | None,
            generation: int,
            base_snapshot_id: str,
            working_snapshot_id: str,
        ) -> CandidateBundle:
            return self.assemble_candidate(
                accepted,
                plans,
                removed_reviews,
                versions=versions,
                audit=audit,
                generation=generation,
                base_snapshot_id=base_snapshot_id,
                working_snapshot_id=working_snapshot_id,
                effective_source=effective_source,
                analysis_reference=analysis_reference,
                analysis_available=analysis_available,
                analysis_stub_tuple=analysis_stub_tuple,
                new_stubs=new_stubs,
                stub_result=stub_result,
                changed_stub_aliases=changed_stub_aliases,
                conflicts=conflicts,
                operation_id=operation_id,
            )

        # ``ArgumentState`` is a strict model, but lightweight callers may
        # pass a list.  Keep the predicate readable and avoid relying on a
        # positional element existing in the accepted argument tuple.
        def valid_argument_tuple(value: object) -> bool:
            if value is None or isinstance(value, (str, bytes, Mapping)):
                return False
            return isinstance(value, tuple) and all(isinstance(item, ArgumentState) for item in value)

        for plan in plans.values():
            if not valid_argument_tuple(plan.get("new_arguments")):
                plan["new_arguments"] = plan["old"].working.arguments if plan.get("old") is not None else ()

        changed_for_version = set(code_changed)
        changed_for_version.update(
            key for key, plan in plans.items()
            if plan.get("added") and not plan.get("removed")
        )

        def preview_timestamp() -> str:
            for state in accepted.function_states:
                return state.display_version.updated_at
            return "1970-01-01T00:00:00Z"

        def versions_for(timestamp: str) -> dict[str, DisplayVersion]:
            versions: dict[str, DisplayVersion] = {}
            for key, plan in plans.items():
                if plan.get("removed"):
                    continue
                old_state = plan.get("old")
                if key in changed_for_version:
                    if old_state is None:
                        versions[key] = DisplayVersion(1, 0, timestamp)
                    else:
                        versions[key] = DisplayVersion(old_state.display_version.major + 1, 0, timestamp)
                elif old_state is not None:
                    versions[key] = old_state.display_version
                else:
                    versions[key] = DisplayVersion(1, 0, timestamp)
            return versions

        delta_count = len(changed_for_version) + sum(1 for plan in plans.values() if plan.get("removed"))
        if self.mode == "observe":
            timestamp = preview_timestamp()
            versions = versions_for(timestamp)
            try:
                candidate = build_candidate(
                    versions,
                    audit=None,
                    generation=accepted.module_manifest.accepted_generation + 1,
                    base_snapshot_id=new_persistent_id("snap", self.uuid_factory),
                    working_snapshot_id=new_persistent_id("snap", self.uuid_factory),
                )
            except Exception as exc:
                conflict = _merge_b_conflict(
                    module_key=accepted.module_key,
                    function_key=function_key,
                    code=(
                        exc.code
                        if isinstance(exc, MergeBProjectionError)
                        else "ORCHESTRATION_CANDIDATE_BUILD_FAILED"
                    ),
                    message=f"observe candidate could not be assembled: {exc}",
                )
                return self._failure(operation_id, accepted.module_key, function_key, accepted, (conflict,), "candidate_build_failed", semantic_change=True, delta_count=delta_count)
            first_version = versions.get(function_key)
            event = self._event(
                operation_id,
                accepted.module_key,
                function_key or "module",
                accepted.revision,
                None,
                next((state.display_version for state in accepted.function_states if state.function_key == function_key), None),
                first_version,
                True,
                "observed",
                "semantic Merge B candidate built; observe mode does not accept writes",
                delta_count,
                conflict_codes=tuple(item.code for item in conflicts),
                validation_ok=None,
            )
            return MergeOutcome(candidate, True, tuple(conflicts), (event,), "observe", False, accepted, event.reason)

        transaction: Any | None = None
        candidate: CandidateBundle | None = None
        audit: AuditEvent | None = None
        try:
            # Acquire/CAS before reading the clock.  A stale operation cannot
            # consume a visible acceptance timestamp.
            transaction = self.repository.begin(accepted.module_key, accepted.revision)
            timestamp = preview_timestamp()
            next_version_for_clock: DisplayVersion | None = None
            if changed_for_version:
                first_changed = next(
                    (plans[key].get("old").display_version for key in sorted(changed_for_version) if plans[key].get("old") is not None),
                    None,
                )
                if first_changed is not None:
                    next_version_for_clock = advance_merge_b(first_changed, clock=self.clock)
                else:
                    next_version_for_clock = initial_display_version(clock=self.clock)
                timestamp = next_version_for_clock.updated_at
            versions = versions_for(timestamp)
            base_snapshot_id = new_persistent_id("snap", self.uuid_factory)
            working_snapshot_id = new_persistent_id("snap", self.uuid_factory)
            next_version = versions.get(function_key)
            previous_version = next(
                (state.display_version for state in accepted.function_states if state.function_key == function_key),
                None,
            )
            audit = self._event(
                operation_id,
                accepted.module_key,
                function_key or "module",
                accepted.revision,
                accepted.revision + 1,
                previous_version,
                next_version,
                True,
                "accepted",
                "semantic Merge B candidate accepted",
                delta_count,
                conflict_codes=tuple(item.code for item in conflicts),
                validation_ok=True,
            )
            candidate = build_candidate(
                versions,
                audit=audit,
                generation=accepted.module_manifest.accepted_generation + 1,
                base_snapshot_id=base_snapshot_id,
                working_snapshot_id=working_snapshot_id,
            )
            transaction.stage(candidate)
            with self.metrics.phase("verification"):
                validation = transaction.validate()
            if not getattr(validation, "ok", False):
                self._abort_transaction(transaction)
                validation_conflicts = self._validation_conflicts(
                    validation,
                    module_key=accepted.module_key,
                    function_key=function_key,
                )
                failed_event = replace(
                    audit,
                    commit_revision=None,
                    next_version=None,
                    acceptance_timestamp=None,
                    outcome="failed",
                    reason="candidate validation failed",
                    conflict_codes=tuple(item.code for item in validation_conflicts),
                    validation_ok=False,
                )
                return MergeOutcome(None, True, validation_conflicts, (failed_event,), "enforce", False, accepted, failed_event.reason)
            with self.metrics.phase("commit"):
                accepted_after = transaction.commit()
            if accepted_after is None:
                accepted_after = self.repository.load(accepted.module_key)
            committed_event = replace(
                audit,
                commit_revision=accepted_after.revision,
                outcome="accepted",
                validation_ok=True,
            )
            return MergeOutcome(
                candidate,
                True,
                tuple(conflicts),
                (committed_event,),
                "enforce",
                True,
                accepted_after,
                committed_event.reason,
            )
        except ConcurrentStateUpdate as exc:
            self._abort_transaction(transaction)
            conflict = _merge_b_conflict(
                module_key=accepted.module_key,
                function_key=function_key,
                code="CONCURRENT_STATE_UPDATE",
                message=str(exc),
            )
            return self._failure(
                operation_id,
                accepted.module_key,
                function_key,
                accepted,
                (conflict,),
                "concurrent_state_update",
                semantic_change=True,
                delta_count=delta_count,
            )
        except Exception as exc:
            error_code = getattr(exc, "code", None)
            published_error = isinstance(exc, (PublishedCommitDurabilityError, PublishedCommitLoadError)) or error_code in {
                PublishedCommitDurabilityError.code,
                PublishedCommitLoadError.code,
            } or getattr(transaction, "state", None) == "committed"
            if published_error and candidate is not None and audit is not None:
                try:
                    accepted_after = self.repository.load(accepted.module_key)
                except Exception:
                    accepted_after = None
                if accepted_after is not None and (
                    accepted_after.revision != accepted.revision + 1
                    or accepted_after.revision_id == accepted.revision_id
                ):
                    accepted_after = None
                code = error_code if isinstance(error_code, str) and error_code else PublishedCommitDurabilityError.code
                published_conflict = _published_commit_conflict(
                    code=code,
                    module_key=accepted.module_key,
                    function_key=function_key,
                    message=(
                        "repository pointer was published; accepted revision was reloaded"
                        if accepted_after is not None
                        else "repository pointer was published; accepted state is unknown and requires explicit reload"
                    ),
                )
                event = replace(
                    audit,
                    commit_revision=(accepted_after.revision if accepted_after is not None else accepted.revision + 1),
                    outcome="published",
                    reason=(
                        "candidate accepted; post-publication transaction diagnostic was reloaded"
                        if accepted_after is not None
                        else "candidate accepted; post-publication reload is required"
                    ),
                    conflict_codes=(published_conflict.code,),
                    validation_ok=True,
                )
                return MergeOutcome(
                    candidate,
                    True,
                    (*tuple(conflicts), published_conflict),
                    (event,),
                    "enforce",
                    True,
                    accepted_after,
                    event.reason,
                )
            if not published_error:
                self._abort_transaction(transaction)
            code = error_code if isinstance(error_code, str) and error_code else "ORCHESTRATION_COMMIT_FAILED"
            conflict = _merge_b_conflict(
                module_key=accepted.module_key,
                function_key=function_key,
                code=code,
                message=f"candidate was not accepted: {exc}",
            )
            return self._failure(
                operation_id,
                accepted.module_key,
                function_key,
                accepted,
                (conflict,),
                "repository_transaction_failed",
                semantic_change=True,
                delta_count=delta_count,
            )


    def _merge_impl(
        self,
        module_key: str,
        new: object | None = None,
        *positional: object,
        function_key: str | None = None,
        new_snapshot: object | None = None,
        new_snapshots: object | None = None,
        snapshots: object | None = None,
        analysis_new: object | None = None,
        analysis_ref: object | None = None,
        analysis_id: str | None = None,
        value_snapshot: FunctionSnapshot | None = None,
        new_value_snapshot: object | None = None,
        value_snapshots: object | None = None,
        stub_snapshot: StubSnapshot | None = None,
        new_stub_snapshot: object | None = None,
        stub_snapshots: object | None = None,
        base_stub_state: object | None = None,
        working_stub_state: object | None = None,
        new_stub_state: object | None = None,
        base_stubs: object | None = None,
        working_stubs: object | None = None,
        new_stubs: object | None = None,
        required_stubs: Iterable[str] | Mapping[str, object] | None = None,
        deleted_stub_keys: Iterable[str] | None = None,
        removed_function_keys: Iterable[str] | None = None,
        function_names: Mapping[str, str] | None = None,
        complete: bool | None = None,
        base_arguments: object | None = None,
        new_arguments: object | None = None,
        arguments: object | None = None,
        source_file: str | None = None,
        body_validator: Callable[..., bool] | None = None,
        resolution_registry: Any | None = None,
        resolution_decisions: Mapping[str, ResolutionDecision] | Iterable[ResolutionDecision] | None = None,
        resolution_context: ResolutionContext | Mapping[str, ResolutionContext] | None = None,
        resolution_applier: Callable[[MergeConflict, ResolutionDecision, object | None], object] | None = None,
        **kwargs: Any,
    ) -> MergeOutcome:
        """Run one exact Merge B operation.

        Normal use is ``merge(module_key, new_snapshot, function_key="fn")``.
        A complete module source map can be supplied as
        ``new_snapshots={"fn": snapshot, ...}`` to enable function add/remove
        policy.  The compatibility positional spelling
        ``(module_key, function_key, new_snapshot)`` is also accepted.
        """

        if positional:
            if len(positional) != 1 or not isinstance(new, str):
                raise TypeError("Merge B positional form is (module_key, function_key, new_snapshot)")
            if function_key is not None:
                raise TypeError("function_key was supplied twice")
            function_key, new = new, positional[0]
        if new is None:
            for alias in (new_snapshot, new_snapshots, snapshots, kwargs.pop("new_generation", None)):
                if alias is not None:
                    new = alias
                    break
        elif any(alias is not None for alias in (new_snapshot, new_snapshots, snapshots)):
            raise TypeError("NEW snapshot was supplied more than once")
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise TypeError(f"unsupported Merge B option(s): {unknown}")
        if resolution_applier is not None and not callable(resolution_applier):
            raise TypeError("resolution_applier must be callable")
        effective_resolution_registry = (
            self.resolution_registry if resolution_registry is None else resolution_registry
        )
        effective_resolution_decisions = (
            self.resolution_decisions if resolution_decisions is None else resolution_decisions
        )
        effective_resolution_applier = (
            self.resolution_applier if resolution_applier is None else resolution_applier
        )
        if type(module_key) is not str or not module_key:
            raise TypeError("module_key must be a non-empty string")

        operation_id = _operation_id(self.uuid_factory)
        if self.mode == "off":
            event = self._event(
                operation_id,
                module_key,
                function_key or "unknown_function",
                None,
                None,
                None,
                None,
                False,
                "off",
                "iterative Merge B is disabled",
                0,
            )
            return MergeOutcome(None, False, (), (event,), "off", False, None, event.reason)

        try:
            accepted = self.repository.load(module_key)
        except Exception as exc:
            key = function_key or "unknown_function"
            conflict = _merge_b_conflict(
                module_key=module_key,
                function_key=key,
                code="ORCHESTRATION_LOAD_FAILED",
                message=f"accepted bundle could not be loaded: {exc}",
            )
            return self._failure(operation_id, module_key, key, None, (conflict,), "load_failed")

        try:
            with self.metrics.phase("validation"):
                new_map, complete_map = _coerce_generation_snapshots(new, fallback_function_key=function_key)
        except Exception as exc:
            key = function_key or "unknown_function"
            conflict = _merge_b_conflict(
                module_key=module_key,
                function_key=key,
                code="ORCHESTRATION_INVALID_INPUT",
                message=f"NEW snapshot input is invalid: {exc}",
            )
            return self._failure(operation_id, module_key, key, accepted, (conflict,), "invalid_input")
        if complete is None:
            complete = complete_map
        if function_key is not None and function_key not in new_map and not complete:
            conflict = _merge_b_conflict(
                module_key=module_key,
                function_key=function_key,
                code="IDENTITY_FUNCTION_MISMATCH",
                message=f"NEW snapshot map has no function {function_key!r}",
            )
            return self._failure(operation_id, module_key, function_key, accepted, (conflict,), "function_not_found")
        if function_key is None:
            if len(new_map) == 1:
                function_key = next(iter(new_map))
            elif len(accepted.function_states) == 1 and len(new_map) == 1:
                function_key = next(iter(new_map))
            else:
                function_key = "module"

        old_analysis_id = accepted.module_manifest.analysis.analysis_id
        supplied_analysis = analysis_new if analysis_new is not None else analysis_ref
        analysis_source = supplied_analysis
        if analysis_id is not None:
            supplied_id = _analysis_id_from(supplied_analysis)
            if supplied_id is not None and supplied_id != analysis_id:
                conflict = _merge_b_conflict(
                    module_key=module_key,
                    function_key=function_key,
                    code="ANALYSIS_DB_MISMATCH",
                    message="analysis_id disagrees with analysis_new reference",
                    user=analysis_id,
                    new=supplied_id,
                )
                return self._failure(operation_id, module_key, function_key, accepted, (conflict,), "analysis_mismatch")
            supplied_analysis = analysis_id
        new_analysis_ids = {snapshot.analysis_id for snapshot in new_map.values()}
        # A complete NEW map may legitimately be empty when the source no
        # longer contains any active functions.  In that case there is no
        # snapshot from which to infer ANALYSIS_NEW, so an explicit
        # ``analysis_id``/``analysis_new`` reference is required.  Do not
        # turn the valid all-functions-deleted lifecycle into a misleading
        # "all snapshots disagree" diagnostic.
        if not new_analysis_ids:
            supplied_id = _analysis_id_from(supplied_analysis)
            if supplied_id is None and isinstance(supplied_analysis, str):
                supplied_id = supplied_analysis
            if supplied_id is not None:
                new_analysis_ids = {supplied_id}
        if len(new_analysis_ids) != 1:
            conflict = _merge_b_conflict(
                module_key=module_key,
                function_key=function_key,
                code="ANALYSIS_DB_MISMATCH",
                message="all NEW snapshots must reference exactly one analysis_id",
                base=old_analysis_id,
                new=tuple(sorted(new_analysis_ids)),
            )
            return self._failure(operation_id, module_key, function_key, accepted, (conflict,), "analysis_mismatch")
        new_analysis_id = next(iter(new_analysis_ids), old_analysis_id)
        if supplied_analysis is not None:
            supplied_id = _analysis_id_from(supplied_analysis)
            if supplied_id is None and isinstance(supplied_analysis, str):
                supplied_id = supplied_analysis
            if supplied_id != new_analysis_id:
                conflict = _merge_b_conflict(
                    module_key=module_key,
                    function_key=function_key,
                    code="ANALYSIS_DB_MISMATCH",
                    message="NEW rows are not bound to supplied ANALYSIS_NEW",
                    user=supplied_id,
                    new=new_analysis_id,
                )
                return self._failure(operation_id, module_key, function_key, accepted, (conflict,), "analysis_mismatch")

        with self.metrics.phase("validation"):
            analysis_reference, analysis_conflicts = self._resolve_analysis_reference(
                accepted,
                new_analysis_id,
                supplied_analysis,
                module_key=module_key,
                function_key=function_key,
            )
        if analysis_conflicts:
            return self._failure(operation_id, module_key, function_key, accepted, analysis_conflicts, "analysis_mismatch")
        if analysis_reference is None:
            if new_analysis_id == old_analysis_id:
                analysis_reference = accepted.module_manifest.analysis
            else:
                conflict = _merge_b_conflict(
                    module_key=module_key,
                    function_key=function_key,
                    code="ANALYSIS_DB_MISMATCH",
                    message="ANALYSIS_NEW must provide a resolvable AnalysisRef",
                    base=old_analysis_id,
                    new=new_analysis_id,
                )
                return self._failure(operation_id, module_key, function_key, accepted, (conflict,), "analysis_mismatch")

        return self._merge_loaded(
            operation_id,
            accepted,
            new_map,
            complete=bool(complete),
            function_key=function_key,
            source_file=source_file,
            analysis_reference=analysis_reference,
            analysis_source=analysis_source,
            old_analysis_id=old_analysis_id,
            value_snapshot=value_snapshot,
            new_value_snapshot=value_snapshots if value_snapshots is not None else new_value_snapshot,
            stub_snapshot=stub_snapshot,
            new_stub_snapshot=stub_snapshots if stub_snapshots is not None else new_stub_snapshot,
            base_stubs=base_stub_state if base_stub_state is not None else base_stubs,
            working_stubs=working_stub_state if working_stub_state is not None else working_stubs,
            new_stubs=new_stub_state if new_stub_state is not None else new_stubs,
            required_stubs=required_stubs,
            deleted_stub_keys=deleted_stub_keys,
            removed_function_keys=removed_function_keys,
            function_names=function_names,
            base_arguments=base_arguments,
            new_arguments=new_arguments if new_arguments is not None else arguments,
            body_validator=body_validator or self.body_validator,
            resolution_registry=effective_resolution_registry,
            resolution_decisions=effective_resolution_decisions,
            resolution_context=resolution_context,
            resolution_applier=effective_resolution_applier,
        )


    def merge(self, *args: Any, **kwargs: Any) -> MergeOutcome:
        """Run one Merge B operation and expose its internal phase report.

        The implementation method has many fail-closed return paths.  Keeping
        this thin boundary around all of them guarantees that malformed input,
        no-op, validation failure, and post-publication uncertainty carry the
        same per-operation metrics without duplicating outcome construction.
        It does not measure the whole call as ``commit``; only the explicit
        phase scopes inside ``_merge_impl`` contribute to that phase.
        """

        if self._metrics_started:
            clock = getattr(self.metrics, "_clock", None)
            self.metrics = PhaseMetrics(clock=clock if callable(clock) else None)
        else:
            self.metrics = self._initial_metrics
            self._metrics_started = True
        outcome = self._merge_impl(*args, **kwargs)
        if isinstance(outcome, MergeOutcome) and outcome.metrics is not self.metrics:
            return replace(outcome, metrics=self.metrics)
        return outcome


__all__ = [
    "AuditEvent",
    "MergeAService",
    "MergeAServiceError",
    "MergeBProjectionError",
    "MergeBService",
    "MergeOutcome",
    "Mode",
]
