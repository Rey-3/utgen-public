"""Read-only orphan review and non-authoritative fuzzy suggestions.

The helpers in this module deliberately stop at a review plan.  They never
change a row, accepted bundle, repository revision, or resolution registry
unless a caller explicitly records a review decision through
``OrphanReviewService``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import math
from pathlib import Path
import os
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .model import DisplayVersion, FunctionIterationState, MergeConflict, canonical_json_bytes
from .resolution import (
    ConflictResolutionRegistry,
    ConflictResolutionService,
    ResolutionConflictError,
    ResolutionContext,
    ResolutionDecision,
    ResolutionError,
    ResolutionRecord,
    ResolutionUnresolvedError,
    ResolutionValidationError,
    stable_conflict_key,
)


class OrphanReviewError(ResolutionValidationError):
    code = "ORPHAN_REVIEW_INVALID"


class FuzzySuggestionError(ResolutionValidationError):
    code = "FUZZY_SUGGESTION_INVALID"


_ORPHAN_ENTITY_TYPES = frozenset({"row", "stub", "function", "artifact"})
_ORPHAN_ACTIONS = frozenset({"retain", "cleanup"})
_CLEANUP_INTERNAL_ARTIFACTS = frozenset(
    {"bundle_manifest.json", "module_manifest.json", "module_stub_state.json", "conflicts.json"}
)
_CLEANUP_STATE_ARTIFACT = re.compile(r"^(?:base|working)/[^/]+/state\.json\Z")
_MISSING = object()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _nonempty_string(value: Any, name: str) -> str:
    if type(value) is not str or not value:
        raise OrphanReviewError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is not None and (type(value) is not str or not value):
        raise OrphanReviewError(f"{name} must be a non-empty string or null")
    return value


def _status(value: Any) -> Any:
    direct = _field(value, "status", _MISSING)
    if direct is not _MISSING:
        return direct
    meta = _field(value, "meta", None)
    if meta is not None:
        return _field(meta, "status", None)
    return None


def _row_id(value: Any) -> Any:
    direct = _field(value, "row_id", _MISSING)
    if direct is not _MISSING:
        return direct
    meta = _field(value, "meta", None)
    return None if meta is None else _field(meta, "row_id", None)


def _function_key(value: Any, fallback: str | None = None) -> str | None:
    result = _field(value, "function_key", _MISSING)
    if result is _MISSING or result is None:
        meta = _field(value, "meta", None)
        result = None if meta is None else _field(meta, "function_key", None)
    return result if result is not None else fallback


def _payload(value: Any) -> Any:
    """Make a complete, JSON-valid review payload without retaining aliases."""

    # ArtifactRecord is intentionally handled without importing transaction;
    # this keeps the read-only assistance layer usable with lightweight test
    # doubles as well as repository artifacts.
    if hasattr(value, "relative_path") and hasattr(value, "data"):
        raw = _field(value, "data")
        if type(raw) is bytes:
            encoded: Any = {"encoding": "hex", "value": raw.hex()}
        else:
            encoded = deepcopy(raw)
        result = {
            "relative_path": deepcopy(_field(value, "relative_path")),
            "kind": deepcopy(_field(value, "kind")),
            "data": encoded,
        }
        if _status(value) is not None:
            result["status"] = deepcopy(_status(value))
        return result
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    try:
        result = deepcopy(value)
        canonical_json_bytes(result)
    except Exception as exc:
        raise OrphanReviewError("orphan payload is not canonical JSON") from exc
    return result


@dataclass(frozen=True)
class OrphanReviewItem:
    """One explicitly orphaned entity captured for human review."""

    module_key: str
    function_key: str | None
    entity_type: str
    identity: str
    payload: Any
    status: str = "orphaned"

    def __post_init__(self) -> None:
        _nonempty_string(self.module_key, "module_key")
        _optional_string(self.function_key, "function_key")
        if self.entity_type not in _ORPHAN_ENTITY_TYPES:
            raise OrphanReviewError("entity_type must be row, stub, function, or artifact")
        _nonempty_string(self.identity, "identity")
        if self.status != "orphaned":
            raise OrphanReviewError("OrphanReviewItem status must be 'orphaned'")
        copied = _payload(self.payload)
        object.__setattr__(self, "payload", copied)

    @property
    def stable_identity(self) -> str:
        return self.identity

    @property
    def function(self) -> str | None:
        return self.function_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_key": self.module_key,
            "function_key": self.function_key,
            "entity_type": self.entity_type,
            "identity": self.identity,
            "payload": deepcopy(self.payload),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OrphanReviewItem":
        if not isinstance(data, Mapping):
            raise OrphanReviewError("OrphanReviewItem must be an object")
        allowed = {"module_key", "function_key", "entity_type", "identity", "payload", "status"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise OrphanReviewError(f"OrphanReviewItem contains unknown field(s): {', '.join(unknown)}")
        missing = [name for name in allowed if name not in data and name != "status"]
        if missing:
            raise OrphanReviewError(f"OrphanReviewItem is missing required field(s): {', '.join(sorted(missing))}")
        return cls(
            data["module_key"],
            data["function_key"],
            data["entity_type"],
            data["identity"],
            data["payload"],
            data.get("status", "orphaned"),
        )


@dataclass(frozen=True)
class OrphanActionPlan:
    """One explicit orphan action and its publication status.

    ``plan()`` is intentionally read-only and therefore returns plans with
    ``committed=False``.  The production cleanup transaction may return the
    same typed record with ``committed=True`` only after the repository has
    published the candidate revision.
    """

    module_key: str
    function_key: str | None
    entity_type: str
    identity: str
    action: str
    tombstone_required: bool
    committed: bool = False
    conflict_key: str | None = None

    def __post_init__(self) -> None:
        _nonempty_string(self.module_key, "module_key")
        _optional_string(self.function_key, "function_key")
        if self.entity_type not in _ORPHAN_ENTITY_TYPES:
            raise OrphanReviewError("invalid action-plan entity_type")
        _nonempty_string(self.identity, "identity")
        if self.action not in _ORPHAN_ACTIONS:
            raise OrphanReviewError("action must be retain or cleanup")
        if type(self.tombstone_required) is not bool or self.tombstone_required != (self.action == "cleanup"):
            raise OrphanReviewError("tombstone_required must be true only for cleanup")
        if type(self.committed) is not bool:
            raise OrphanReviewError("committed must be a boolean")
        _optional_string(self.conflict_key, "conflict_key")

    @property
    def cleanup(self) -> bool:
        return self.action == "cleanup"

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_key": self.module_key,
            "function_key": self.function_key,
            "entity_type": self.entity_type,
            "identity": self.identity,
            "action": self.action,
            "tombstone_required": self.tombstone_required,
            "committed": self.committed,
            "conflict_key": self.conflict_key,
        }


@dataclass(frozen=True)
class OrphanCleanupOutcome:
    """Typed result of one accepted-bundle orphan cleanup attempt.

    ``committed`` is true only when the repository pointer was published (it
    may be accompanied by a post-publication reload diagnostic).  A blocking
    pre-publication result always carries the original accepted bundle so a
    caller can continue to use the old state without guessing whether a
    partial write happened.
    """

    candidate: Any | None
    plans: tuple[OrphanActionPlan, ...]
    conflicts: tuple[MergeConflict, ...]
    committed: bool
    accepted: Any | None
    reason: str
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if self.candidate is not None:
            # Importing CandidateBundle at module import time would make this
            # read-only helper needlessly depend on repository setup.  The
            # runtime check below still keeps the public outcome typed when a
            # production transaction returns a malformed object.
            if not hasattr(self.candidate, "module_key"):
                raise OrphanReviewError("cleanup candidate must expose module_key")
        if type(self.plans) is not tuple or not all(isinstance(item, OrphanActionPlan) for item in self.plans):
            raise OrphanReviewError("cleanup plans must contain OrphanActionPlan records")
        if type(self.conflicts) is not tuple or not all(isinstance(item, MergeConflict) for item in self.conflicts):
            raise OrphanReviewError("cleanup conflicts must contain MergeConflict records")
        if type(self.committed) is not bool:
            raise OrphanReviewError("cleanup committed must be a boolean")
        _nonempty_string(self.reason, "reason")
        _optional_string(self.operation_id, "operation_id")

    @property
    def blocked(self) -> bool:
        return any(item.severity == "blocking" for item in self.conflicts)

    @property
    def no_op(self) -> bool:
        return not self.committed and self.candidate is None and not self.blocked

    @property
    def conflict_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.conflicts)


def _module_of(value: Any, fallback: str | None) -> str:
    module = _field(value, "module_key", None)
    if module is None:
        module = fallback
    return _nonempty_string(module, "module_key")


def _add_item(items: list[OrphanReviewItem], item: OrphanReviewItem) -> None:
    marker = (item.module_key, item.entity_type, item.identity)
    if any((old.module_key, old.entity_type, old.identity) == marker for old in items):
        raise OrphanReviewError("duplicate orphan identity")
    items.append(item)


def _row_item(row: Any, module_key: str, function_key: str | None) -> OrphanReviewItem | None:
    if _status(row) != "orphaned":
        return None
    identity = _row_id(row)
    if type(identity) is not str or not identity:
        raise OrphanReviewError("orphaned row is missing row_id")
    return OrphanReviewItem(module_key, _function_key(row, function_key), "row", identity, row)


def _stub_item(stub: Any, module_key: str, function_key: str | None = None) -> OrphanReviewItem | None:
    if _status(stub) != "orphaned":
        return None
    identity = _field(stub, "stub_key", None)
    if type(identity) is not str or not identity:
        raise OrphanReviewError("orphaned stub is missing stub_key")
    return OrphanReviewItem(module_key, _field(stub, "function_name", function_key), "stub", identity, stub)


def _artifact_item(artifact: Any, module_key: str, function_key: str | None = None) -> OrphanReviewItem | None:
    status = _status(artifact)
    identity = _field(artifact, "relative_path", None)
    kind = _field(artifact, "kind", None)
    generated_review = (
        type(identity) is str
        and identity.startswith("orphaned/")
        and identity.endswith("/review.json")
        and kind == "orphan_review"
    )
    # Repository-generated orphan review records do not carry a second
    # status field: their exact kind/path is the explicit orphan marker.  A
    # caller-provided artifact still needs status=orphaned, and an explicit
    # active status must never be collected merely because its path is close.
    if status is not None and status != "orphaned":
        return None
    if status != "orphaned" and not generated_review:
        return None
    if type(identity) is not str or not identity:
        raise OrphanReviewError("orphaned artifact is missing relative_path")
    attributed_function = function_key
    if generated_review:
        parts = identity.split("/")
        # The ArtifactRecord path validator already rejects unsafe components;
        # keep the shape check here because the path is also the ownership
        # evidence used by cleanup.
        if len(parts) != 3 or parts[0] != "orphaned" or not parts[1]:
            raise OrphanReviewError("orphan review artifact path has no exact function owner")
        attributed_function = parts[1]
    return OrphanReviewItem(module_key, attributed_function, "artifact", identity, artifact)


def collect_orphans(source: Any, *, module_key: str | None = None) -> tuple[OrphanReviewItem, ...]:
    """Collect only entities whose explicit status is exactly ``orphaned``."""

    items: list[OrphanReviewItem] = []

    def visit_state(state: Any, current_module: str) -> None:
        function = _function_key(state)
        if _status(state) == "orphaned":
            if function is None:
                raise OrphanReviewError("orphaned function state is missing function_key")
            _add_item(items, OrphanReviewItem(current_module, function, "function", function, state))
        working = _field(state, "working", None)
        if working is not None:
            rows = _field(working, "rows", ())
            if rows is None:
                rows = ()
            for row in rows:
                item = _row_item(row, current_module, function)
                if item is not None:
                    _add_item(items, item)

    def visit(value: Any, current_module: str | None = None, function: str | None = None) -> None:
        if value is None:
            return
        if isinstance(value, (str, bytes, bytearray)):
            return
        # AcceptedBundle-like containers expose all three collections used by
        # review.  Keeping this explicit avoids interpreting arbitrary active
        # attributes as orphan records.
        states = _field(value, "function_states", _MISSING)
        stubs = _field(value, "module_stub_state", _MISSING)
        artifacts = _field(value, "artifacts", _MISSING)
        if states is not _MISSING or stubs is not _MISSING or artifacts is not _MISSING:
            module = _module_of(value, current_module)
            if states is not _MISSING:
                for state in states or ():
                    visit_state(state, module)
            if stubs is not _MISSING:
                for stub in stubs or ():
                    item = _stub_item(stub, module, function)
                    if item is not None:
                        _add_item(items, item)
            if artifacts is not _MISSING:
                for artifact in artifacts or ():
                    item = _artifact_item(artifact, module, function)
                    if item is not None:
                        _add_item(items, item)
            explicit = _field(value, "orphans", ())
            for orphan in explicit or ():
                visit(orphan, module, function)
            return

        # A single FunctionIterationState-like object.
        if _field(value, "working", _MISSING) is not _MISSING and _field(value, "function_key", _MISSING) is not _MISSING:
            visit_state(value, _module_of(value, current_module))
            return

        if isinstance(value, Mapping) and "entity_type" in value:
            module = _module_of(value, current_module)
            kind = value.get("entity_type")
            if kind == "row":
                item = _row_item(value, module, function)
            elif kind == "stub":
                item = _stub_item(value, module, function)
            elif kind == "artifact":
                item = _artifact_item(value, module, function)
            elif kind == "function":
                if value.get("status") != "orphaned":
                    item = None
                else:
                    identity = value.get("function_key") or value.get("function")
                    if not isinstance(identity, str) or not identity:
                        raise OrphanReviewError("orphaned function is missing function_key")
                    item = OrphanReviewItem(module, identity, "function", identity, value)
            else:
                raise OrphanReviewError("unsupported orphan entity_type")
            if item is not None:
                _add_item(items, item)
            return

        if _field(value, "stub_key", _MISSING) is not _MISSING:
            item = _stub_item(value, _module_of(value, current_module), function)
            if item is not None:
                _add_item(items, item)
            return
        if _field(value, "relative_path", _MISSING) is not _MISSING and _field(value, "data", _MISSING) is not _MISSING:
            item = _artifact_item(value, _module_of(value, current_module), function)
            if item is not None:
                _add_item(items, item)
            return
        if _field(value, "meta", _MISSING) is not _MISSING or _field(value, "values", _MISSING) is not _MISSING:
            item = _row_item(value, _module_of(value, current_module), function)
            if item is not None:
                _add_item(items, item)
            return
        if isinstance(value, Mapping):
            # Explicit accepted-like dictionaries may omit empty collections.
            for key in ("function_states", "module_stub_state", "artifacts", "orphans"):
                if key in value:
                    visit(value[key], current_module, function)
            return
        if isinstance(value, Iterable):
            for child in value:
                visit(child, current_module, function)

    visit(source, module_key)
    return tuple(
        sorted(
            items,
            key=lambda item: (item.module_key, item.function_key or "", item.entity_type, item.identity),
        )
    )


def _orphan_conflict(item: OrphanReviewItem) -> MergeConflict:
    function_key = item.function_key or item.module_key
    row_id = item.identity if item.entity_type == "row" else None
    stub_key = item.identity if item.entity_type == "stub" else None
    # MergeConflict has no dedicated artifact identity field.  Use the
    # exact relative path in ``case_name`` for artifact conflicts so stable
    # conflict keys cannot collapse two review artifacts belonging to one
    # function.  This field is identity material here, not a TESSY case name.
    case_name = item.identity if item.entity_type == "artifact" else None
    return MergeConflict(
        "ORPHAN_REVIEW",
        "blocking",
        item.entity_type,
        function_key,
        row_id,
        case_name,
        None,
        stub_key,
        None,
        item.payload,
        None,
        f"orphaned {item.entity_type} {item.identity!r} requires explicit review",
        ("retain", "cleanup"),
    )


class OrphanReviewService:
    """Map orphan records to explicit resolution conflicts and plans."""

    def __init__(
        self,
        resolutions: ConflictResolutionService | ConflictResolutionRegistry | str | os.PathLike[str] | None = None,
        *,
        module_key: str | None = None,
        path: str | os.PathLike[str] | None = None,
        state_root: str | os.PathLike[str] | None = None,
        state_path: str | os.PathLike[str] | None = None,
        expected_digest: str | None = None,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.module_key = module_key
        if isinstance(resolutions, ConflictResolutionService):
            if any(value is not None for value in (path, state_root, state_path, expected_digest, failure_injector)):
                raise OrphanReviewError("resolution service cannot be combined with path options")
            self.resolutions = resolutions
        elif isinstance(resolutions, ConflictResolutionRegistry):
            self.resolutions = ConflictResolutionService(resolutions)
        else:
            selected = path if path is not None else resolutions
            self.resolutions = ConflictResolutionService(
                selected,
                state_root=state_root,
                state_path=state_path,
                expected_digest=expected_digest,
                failure_injector=failure_injector,
            )
        self._items: tuple[OrphanReviewItem, ...] = ()
        self._conflicts: dict[str, MergeConflict] = {}

    @property
    def registry(self) -> ConflictResolutionRegistry:
        return self.resolutions.registry

    @property
    def audit(self):
        return self.resolutions.audit

    def list_orphans(self, source: Any | None = None, *, module_key: str | None = None) -> tuple[OrphanReviewItem, ...]:
        selected_module = module_key if module_key is not None else self.module_key
        if source is not None:
            self._items = collect_orphans(source, module_key=selected_module)
            self._conflicts = {}
            for item in self._items:
                conflict = _orphan_conflict(item)
                key = stable_conflict_key(conflict, item.module_key)
                if key in self._conflicts:
                    raise OrphanReviewError("duplicate orphan conflict identity")
                self._conflicts[key] = conflict
        return self._items

    def list_conflicts(self, source: Any | None = None, *, module_key: str | None = None) -> tuple[MergeConflict, ...]:
        self.list_orphans(source, module_key=module_key)
        return tuple(self._conflicts[key] for key in sorted(self._conflicts))

    def _as_conflict(self, item_or_conflict: OrphanReviewItem | MergeConflict) -> MergeConflict:
        if isinstance(item_or_conflict, MergeConflict):
            return item_or_conflict
        if not isinstance(item_or_conflict, OrphanReviewItem):
            raise OrphanReviewError("expected OrphanReviewItem or MergeConflict")
        conflict = _orphan_conflict(item_or_conflict)
        module_key = self.module_key
        if isinstance(item_or_conflict, OrphanReviewItem):
            module_key = item_or_conflict.module_key
        if module_key is None:
            raise OrphanReviewError("module_key is required to scope orphan conflict")
        self._conflicts[stable_conflict_key(conflict, module_key)] = conflict
        return conflict

    def record(
        self,
        item_or_conflict: OrphanReviewItem | MergeConflict,
        decision: ResolutionDecision | None = None,
        **kwargs: Any,
    ) -> ResolutionRecord:
        conflict = self._as_conflict(item_or_conflict)
        if isinstance(item_or_conflict, OrphanReviewItem):
            decision_context = decision.context if decision is not None else kwargs.get("context")
            if decision_context is not None and decision_context.module_key != item_or_conflict.module_key:
                raise ResolutionConflictError("orphan decision context module does not match orphan module")
        return self.resolutions.record(conflict, decision, **kwargs)

    def lookup(self, item_or_conflict: OrphanReviewItem | MergeConflict | str, context: ResolutionContext | None = None):
        if isinstance(item_or_conflict, OrphanReviewItem):
            item_or_conflict = self._as_conflict(item_or_conflict)
        return self.resolutions.lookup(item_or_conflict, context)

    def replay(
        self,
        source: Any | None = None,
        context: ResolutionContext | None = None,
        *,
        require_all: bool = True,
    ) -> dict[str, ResolutionDecision]:
        conflicts = self.list_conflicts(source)
        return self.resolutions.replay(conflicts, context, require_all=require_all)

    def plan(self, source: Any | None = None, context: ResolutionContext | None = None) -> tuple[OrphanActionPlan, ...]:
        if context is None:
            raise OrphanReviewError("ResolutionContext is required to build an orphan action plan")
        self.list_conflicts(source)
        plans: list[OrphanActionPlan] = []
        for item in self._items:
            conflict = _orphan_conflict(item)
            record = self.resolutions.lookup(conflict, context)
            if record is None:
                raise ResolutionUnresolvedError(
                    "orphan action plan has an unresolved item",
                    details={"conflict_key": stable_conflict_key(conflict, item.module_key), "identity": item.identity},
                )
            plans.append(
                OrphanActionPlan(
                    item.module_key,
                    item.function_key,
                    item.entity_type,
                    item.identity,
                    record.action,
                    record.action == "cleanup",
                    False,
                    stable_conflict_key(conflict, item.module_key),
                )
            )
        return tuple(plans)

    def cleanup(
        self,
        repository: Any,
        context: ResolutionContext | None = None,
        *,
        module_key: str | None = None,
        clock: Callable[[], str] | None = None,
        uuid_factory: Callable[[], Any] | None = None,
    ) -> OrphanCleanupOutcome:
        """Apply persisted orphan decisions through one repository transaction.

        Orphan review has no independent pure NEW snapshot.  Its explicit
        context rule is therefore ``new_snapshot_id == working_snapshot_id``:
        the accepted WORKING snapshot is named as NEW solely to make the
        absence of a third snapshot explicit and replay-safe.  The rule is
        checked before every decision lookup and is never inferred from a
        fuzzy suggestion or from the review payload.
        """

        if not hasattr(repository, "load") or not hasattr(repository, "begin"):
            raise TypeError("repository must implement load() and begin()")
        selected_module = module_key or self.module_key
        if selected_module is None and isinstance(context, ResolutionContext):
            selected_module = context.module_key
        if type(selected_module) is not str or not selected_module:
            raise OrphanReviewError("module_key is required for orphan cleanup")

        try:
            accepted = repository.load(selected_module)
        except Exception:
            # Loading an accepted bundle is an external repository boundary;
            # there is no safe accepted object to attach to a typed outcome.
            raise

        def anchor_conflict(code: str, message: str, item: OrphanReviewItem | None = None) -> MergeConflict:
            if item is not None:
                return replace(
                    _orphan_conflict(item),
                    code=code,
                    message=message,
                    allowed_resolutions=(),
                )
            return MergeConflict(
                code,
                "blocking",
                "orphan_review",
                selected_module,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                message,
                (),
            )

        def blocked(
            code: str,
            message: str,
            *,
            item: OrphanReviewItem | None = None,
            plans: tuple[OrphanActionPlan, ...] = (),
            candidate: Any | None = None,
            operation_id: str | None = None,
        ) -> OrphanCleanupOutcome:
            return OrphanCleanupOutcome(
                candidate,
                plans,
                (anchor_conflict(code, message, item),),
                False,
                accepted,
                message,
                operation_id,
            )

        try:
            items = self.list_orphans(accepted, module_key=selected_module)
        except Exception as exc:
            return blocked("ORPHAN_CLEANUP_DISCOVERY_FAILED", f"accepted orphan discovery failed: {exc}")

        # A second invocation after a successful cleanup is deliberately a
        # no-op even when it carries the now-stale context that was used for
        # the first invocation.  There is no orphan decision to replay and no
        # write is possible, so accepting the no-op cannot mutate state.
        if not items:
            return OrphanCleanupOutcome(None, (), (), False, accepted, "no orphan cleanup required")

        if not isinstance(context, ResolutionContext):
            return blocked("ORPHAN_CLEANUP_CONTEXT_REQUIRED", "ResolutionContext is required for orphan cleanup")

        manifest = accepted.module_manifest
        expected_context = (
            selected_module,
            manifest.base_snapshot_id,
            manifest.working_snapshot_id,
            accepted.revision,
        )
        if context.module_key != expected_context[0]:
            return blocked("ORPHAN_CLEANUP_CONTEXT_MISMATCH", "cleanup context module does not match accepted module")
        if context.base_snapshot_id != expected_context[1]:
            return blocked("ORPHAN_CLEANUP_CONTEXT_MISMATCH", "cleanup context BASE snapshot does not match accepted manifest")
        if context.working_snapshot_id != expected_context[2]:
            return blocked("ORPHAN_CLEANUP_CONTEXT_MISMATCH", "cleanup context WORKING snapshot does not match accepted manifest")
        if context.revision != expected_context[3]:
            return blocked("ORPHAN_CLEANUP_CONTEXT_STALE", "cleanup context revision is stale")
        if context.new_snapshot_id != context.working_snapshot_id:
            return blocked(
                "ORPHAN_CLEANUP_CONTEXT_MISMATCH",
                "cleanup without an independent NEW snapshot requires new_snapshot_id == working_snapshot_id",
            )

        decisions: list[tuple[OrphanReviewItem, ResolutionRecord]] = []
        for item in items:
            conflict = _orphan_conflict(item)
            try:
                record = self.resolutions.lookup(conflict, context)
            except ResolutionError as exc:
                return blocked(
                    "ORPHAN_CLEANUP_RESOLUTION_INVALID",
                    f"persisted orphan decision is not usable for {item.identity!r}: {exc}",
                    item=item,
                )
            if record is None:
                return blocked(
                    "ORPHAN_CLEANUP_RESOLUTION_MISSING",
                    f"orphan {item.identity!r} has no persisted decision for the exact cleanup context",
                    item=item,
                )
            if record.context != context:
                return blocked(
                    "ORPHAN_CLEANUP_CONTEXT_MISMATCH",
                    f"persisted decision for {item.identity!r} has a different cleanup context",
                    item=item,
                )
            if record.action not in _ORPHAN_ACTIONS or record.action not in conflict.allowed_resolutions:
                return blocked(
                    "ORPHAN_CLEANUP_ACTION_INVALID",
                    f"persisted action for {item.identity!r} is not retain or cleanup",
                    item=item,
                )
            decisions.append((item, record))

        plans = tuple(
            OrphanActionPlan(
                item.module_key,
                item.function_key,
                item.entity_type,
                item.identity,
                record.action,
                record.action == "cleanup",
                False,
                stable_conflict_key(_orphan_conflict(item), item.module_key),
            )
            for item, record in decisions
        )

        states = tuple(accepted.function_states)

        def state_matches(identity: str | None) -> tuple[Any, ...]:
            if not isinstance(identity, str) or not identity:
                return ()
            return tuple(
                state
                for state in states
                if state.function_key == identity or state.function == identity
            )

        def owner_states(stub: Any) -> tuple[Any, ...] | None:
            owners = tuple(sorted(str(item) for item in getattr(stub, "owners", frozenset()) if str(item)))
            if not owners:
                function_name = getattr(stub, "function_name", None)
                owners = (function_name,) if isinstance(function_name, str) and function_name else ()
            if not owners:
                return None
            selected: list[Any] = []
            for owner in owners:
                matches = state_matches(owner)
                if len(matches) != 1:
                    return None
                selected.append(matches[0])
            unique = {state.function_key: state for state in selected}
            return tuple(unique[key] for key in sorted(unique))

        rows_to_remove: dict[str, set[str]] = {}
        stubs_to_remove: dict[str, set[str]] = {}
        # State identity uses ``stub_key`` while the XML projection identifies
        # called functions by ``function_name``.  Keep the two representations
        # separate so an alias/synthetic key cannot leave stale XML behind.
        stub_aliases_to_remove: dict[str, set[str]] = {}
        artifacts_to_remove: dict[str, set[str]] = {}
        affected_keys: set[str] = set()
        orphan_stub_by_key: dict[str, Any] = {}
        orphan_artifact_by_path: dict[str, Any] = {}

        for item, record in decisions:
            if record.action != "cleanup":
                continue
            if item.entity_type == "row":
                matches = state_matches(item.function_key)
                if len(matches) != 1:
                    return blocked(
                        "ORPHAN_CLEANUP_OWNERSHIP_INVALID",
                        f"orphan row {item.identity!r} has no unique function owner",
                        item=item,
                    )
                state = matches[0]
                row_matches = tuple(
                    row
                    for row in state.working.rows
                    if getattr(getattr(row, "meta", None), "row_id", None) == item.identity
                )
                if len(row_matches) != 1 or getattr(row_matches[0].meta, "status", None) != "orphaned":
                    return blocked(
                        "ORPHAN_CLEANUP_IDENTITY_INVALID",
                        f"orphan row {item.identity!r} is not an exact orphan in accepted WORKING state",
                        item=item,
                    )
                if row_matches[0].meta.function_key != state.function_key:
                    return blocked(
                        "ORPHAN_CLEANUP_OWNERSHIP_INVALID",
                        f"orphan row {item.identity!r} provenance disagrees with its state owner",
                        item=item,
                    )
                rows_to_remove.setdefault(state.function_key, set()).add(item.identity)
                affected_keys.add(state.function_key)
            elif item.entity_type == "stub":
                stub_matches = tuple(
                    stub for stub in accepted.module_stub_state if getattr(stub, "stub_key", None) == item.identity
                )
                if len(stub_matches) != 1 or getattr(stub_matches[0], "status", None) != "orphaned":
                    return blocked(
                        "ORPHAN_CLEANUP_IDENTITY_INVALID",
                        f"orphan stub {item.identity!r} is not an exact orphan in accepted module state",
                        item=item,
                    )
                owners = owner_states(stub_matches[0])
                if not owners:
                    return blocked(
                        "ORPHAN_CLEANUP_OWNERSHIP_INVALID",
                        f"orphan stub {item.identity!r} has no provable active function owner",
                        item=item,
                    )
                orphan_stub = stub_matches[0]
                aliases = {
                    getattr(orphan_stub, "stub_key", None),
                    getattr(orphan_stub, "function_name", None),
                }
                if not all(type(alias) is str and alias for alias in aliases):
                    return blocked(
                        "ORPHAN_CLEANUP_IDENTITY_INVALID",
                        f"orphan stub {item.identity!r} has invalid XML aliases",
                        item=item,
                    )
                # An alias is an XML identity, not merely a state key.  If an
                # active stub owned by this same function uses either alias,
                # removing that XML element would be ambiguous.  If the active
                # stub cannot itself be attributed, fail closed as well.
                for state in owners:
                    for active_stub in accepted.module_stub_state:
                        if active_stub is orphan_stub or getattr(active_stub, "status", None) != "active":
                            continue
                        active_aliases = {
                            getattr(active_stub, "stub_key", None),
                            getattr(active_stub, "function_name", None),
                        }
                        if not aliases.intersection(active_aliases):
                            continue
                        active_owners = owner_states(active_stub)
                        if active_owners is None or any(
                            owner.function_key == state.function_key for owner in active_owners
                        ):
                            return blocked(
                                "ORPHAN_CLEANUP_ALIAS_AMBIGUOUS",
                                f"orphan stub {item.identity!r} shares an XML alias with an active stub",
                                item=item,
                            )
                orphan_stub_by_key[item.identity] = orphan_stub
                for state in owners:
                    stubs_to_remove.setdefault(state.function_key, set()).add(item.identity)
                    stub_aliases_to_remove.setdefault(state.function_key, set()).update(aliases)
                    affected_keys.add(state.function_key)
            elif item.entity_type == "artifact":
                artifact_matches = tuple(
                    artifact
                    for artifact in accepted.artifacts
                    if getattr(artifact, "relative_path", None) == item.identity
                )
                if len(artifact_matches) != 1:
                    return blocked(
                        "ORPHAN_CLEANUP_IDENTITY_INVALID",
                        f"orphan review artifact {item.identity!r} is not an exact accepted artifact",
                        item=item,
                    )
                matches = state_matches(item.function_key)
                if len(matches) != 1:
                    return blocked(
                        "ORPHAN_CLEANUP_OWNERSHIP_INVALID",
                        f"orphan review artifact {item.identity!r} has no unique function owner",
                        item=item,
                    )
                owner = matches[0]
                orphan_artifact_by_path[item.identity] = artifact_matches[0]
                artifacts_to_remove.setdefault(owner.function_key, set()).add(item.identity)
                affected_keys.add(owner.function_key)
            else:
                # Function-level orphan records have no independent accepted
                # payload that can be removed while retaining a valid state
                # owner.  Refuse rather than silently deleting an entire
                # function state from a module bundle.
                return blocked(
                    "ORPHAN_FUNCTION_CLEANUP_UNSUPPORTED",
                    f"function orphan {item.identity!r} has no lossless accepted-state cleanup boundary",
                    item=item,
                )

        if not affected_keys:
            return OrphanCleanupOutcome(None, plans, (), False, accepted, "all orphan decisions retain state")

        # Allocate the timestamp only after exact context, payload, action,
        # and ownership checks have passed.  One timestamp is shared by every
        # affected function in this mixed cleanup transaction.
        if clock is None:
            clock = lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if not callable(clock):
            return blocked("ORPHAN_CLEANUP_CLOCK_INVALID", "cleanup clock must be callable")
        if uuid_factory is None:
            uuid_factory = getattr(repository, "uuid_factory", None)
        if not callable(uuid_factory):
            return blocked("ORPHAN_CLEANUP_UUID_FACTORY_INVALID", "cleanup uuid_factory must be callable")
        try:
            from .versioning import canonicalize_timestamp

            timestamp = canonicalize_timestamp(clock())
        except Exception as exc:
            return blocked("ORPHAN_CLEANUP_CLOCK_INVALID", f"cleanup timestamp is invalid: {exc}")

        # Local imports keep the read-only assistance module independent of
        # the high-level merge service until a real XML projection is present.
        from .adapters._xml_common import clone_element
        from .adapters.stub_xml import StubSnapshot, StubXmlAdapter
        from .adapters.value_xml import FunctionSnapshot, ValueXmlAdapter
        from .service import MergeBService
        from .transaction import (
            ArtifactRecord,
            CandidateBundle,
            PublishedCommitDurabilityError,
            PublishedCommitLoadError,
            new_persistent_id,
        )

        value_adapter = ValueXmlAdapter()
        stub_adapter = StubXmlAdapter()
        projection_service = MergeBService(
            repository,
            value_adapter=value_adapter,
            stub_adapter=stub_adapter,
        )

        transaction: Any | None = None
        candidate: CandidateBundle | None = None
        operation_id: str | None = None
        try:
            # begin() performs the repository CAS while the accepted revision
            # is still the one whose resolutions were replayed.
            transaction = repository.begin(selected_module, accepted.revision)
            operation_id = getattr(transaction, "operation_id", None)
            generation = manifest.accepted_generation + 1
            working_snapshot_id = new_persistent_id("snap", uuid_factory)
            versions: dict[str, DisplayVersion] = {
                state.function: state.display_version for state in states
            }
            for state in states:
                if state.function_key in affected_keys:
                    old = state.display_version
                    versions[state.function] = DisplayVersion(old.major, old.minor + 1, timestamp)

            candidate_states: list[FunctionIterationState] = []
            for state in states:
                removed_rows = rows_to_remove.get(state.function_key, set())
                rows = tuple(
                    row
                    for row in state.working.rows
                    if getattr(getattr(row, "meta", None), "row_id", None) not in removed_rows
                )
                row_order = tuple(row.meta.row_id for row in rows if getattr(row, "meta", None) is not None)
                tombstones = list(state.working.tombstones)

                def add_tombstone(entity_type: str, identity: str) -> None:
                    marker = (entity_type, identity, generation)
                    if not any(
                        (
                            getattr(existing, "entity_type", None),
                            getattr(existing, "entity_key", None),
                            getattr(existing, "generation_deleted", None),
                        )
                        == marker
                        for existing in tombstones
                    ):
                        from .model import Tombstone

                        tombstones.append(Tombstone(entity_type, identity, generation))

                for row_id in sorted(removed_rows):
                    add_tombstone("row", row_id)
                for stub_key in sorted(stubs_to_remove.get(state.function_key, set())):
                    add_tombstone("stub", stub_key)
                for artifact_path in sorted(artifacts_to_remove.get(state.function_key, set())):
                    add_tombstone("artifact", artifact_path)

                new_base = replace(state.base, generation=generation)
                new_working = replace(
                    state.working,
                    snapshot_id=working_snapshot_id,
                    generation=generation,
                    rows=rows,
                    row_order=row_order,
                    tombstones=tuple(tombstones),
                )
                candidate_states.append(
                    replace(
                        state,
                        display_version=versions[state.function],
                        base=new_base,
                        working=new_working,
                    )
                )

            candidate_manifest = replace(
                manifest,
                accepted_generation=generation,
                working_snapshot_id=working_snapshot_id,
                function_versions=versions,
            )

            # Validate and, where necessary, re-render every recognized XML
            # projection.  State-only artifacts remain byte-for-byte copies;
            # a malformed or ambiguous versioned XML artifact is a blocking
            # candidate error rather than something to carry forward.
            def artifact_role(record: ArtifactRecord) -> str | None:
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
                # Mirror MergeB's generic XML probe.  A caller-owned artifact
                # may omit role/path hints, but a supported value/stub root may
                # not bypass identity and metadata validation merely because it
                # is stored under an opaque or legacy filename.
                if not path.endswith(".xml"):
                    return None
                value_snapshot: FunctionSnapshot | None = None
                stub_snapshot: StubSnapshot | None = None
                try:
                    value_snapshot = value_adapter.read(record.data, allow_legacy=True)
                except Exception:
                    pass
                try:
                    stub_snapshot = stub_adapter.read(record.data, allow_legacy=True)
                except Exception:
                    pass
                if value_snapshot is not None and stub_snapshot is not None:
                    raise OrphanReviewError(
                        f"XML artifact {record.relative_path!r} matches both value and stub formats"
                    )
                if value_snapshot is not None:
                    return "value"
                if stub_snapshot is not None:
                    return "stub"
                raw = record.data
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                if isinstance(raw, (bytes, bytearray, memoryview)) and (
                    b"utgen_version" in bytes(raw) or b"utgen_updated_at" in bytes(raw)
                ):
                    raise OrphanReviewError(
                        f"versioned XML artifact {record.relative_path!r} has no recognized UTGen projection"
                    )
                return None

            def unscoped_path_is_explicit(
                record: ArtifactRecord,
                *,
                role: str,
                identities: Iterable[str],
            ) -> bool:
                """Accept only deterministic legacy paths without BASE/WORKING."""

                parts = record.relative_path.split("/")
                filename = parts[-1]
                stem = filename.rsplit(".", 1)[0]
                placeholders = {
                    "value": {"value", "values"},
                    "stub": {"stub", "stubs"},
                }[role]
                known = {str(identity) for identity in identities if str(identity)}
                if len(parts) == 1 and (stem in placeholders or stem in known):
                    return True
                if len(parts) == 1 and any(stem.endswith("_" + identity) for identity in known):
                    return True
                if parts and parts[0].lower() in placeholders:
                    return stem in placeholders or stem in known or any(
                        stem.endswith("_" + identity) for identity in known
                    )
                return False

            def xml_owner(record: ArtifactRecord, parsed: Any, role: str) -> tuple[FunctionIterationState, str]:
                identity = parsed.name if isinstance(parsed, FunctionSnapshot) else parsed.function_name
                matches = tuple(
                    state
                    for state in candidate_states
                    if identity in {state.function_key, state.function}
                )
                if len(matches) != 1:
                    raise OrphanReviewError(
                        f"XML artifact {record.relative_path!r} identity {identity!r} does not resolve to one function"
                    )
                identities = {matches[0].function_key, matches[0].function}
                hint = projection_service._artifact_function_hint(record, identities)
                if hint is not None and hint not in identities:
                    raise OrphanReviewError(
                        f"XML artifact {record.relative_path!r} path identity {hint!r} disagrees with root identity"
                    )
                slot = projection_service._artifact_slot(record)
                if slot is None:
                    if not unscoped_path_is_explicit(record, role=role, identities=identities):
                        raise OrphanReviewError(
                            f"XML artifact {record.relative_path!r} has no explicit function/slot path"
                        )
                    if len(candidate_states) != 1:
                        raise OrphanReviewError(
                            f"unscoped {role} XML artifact {record.relative_path!r} cannot identify BASE/WORKING in a multi-function bundle"
                        )
                    slot = "working"
                return matches[0], slot

            def read_value(data: bytes) -> FunctionSnapshot:
                try:
                    return value_adapter.read(data)
                except Exception:
                    return value_adapter.read(data, allow_legacy=True)

            def read_stub(data: bytes) -> StubSnapshot:
                try:
                    return stub_adapter.read(data)
                except Exception:
                    return stub_adapter.read(data, allow_legacy=True)

            def render_xml(record: ArtifactRecord, role: str, state: FunctionIterationState, slot: str) -> ArtifactRecord:
                previous_state = next(
                    (item for item in states if item.function_key == state.function_key),
                    None,
                )
                if previous_state is None:
                    raise OrphanReviewError(
                        f"XML artifact {record.relative_path!r} has no accepted function state"
                    )
                if role == "value":
                    parsed = read_value(record.data)
                    if parsed.display_version != previous_state.display_version:
                        raise OrphanReviewError(
                            f"value XML artifact {record.relative_path!r} version disagrees with accepted state"
                        )
                    source_rows = state.base.rows if slot == "base" else state.working.rows
                    active_rows = tuple(
                        row
                        for row in source_rows
                        if getattr(getattr(row, "meta", None), "status", "active") == "active"
                    )
                    if not active_rows:
                        # The generic projection helper intentionally keeps a
                        # one-empty-group stream for some compatibility
                        # shapes.  An empty accepted WORKING state must be
                        # represented by absent streams, never by one blank
                        # testcase row.
                        projected = replace(
                            parsed,
                            arguments=tuple(
                                argument.with_values(range=None, output=None)
                                for argument in parsed.arguments
                            ),
                        )
                    else:
                        projected = projection_service._project_value_snapshot(
                            parsed,
                            active_rows,
                            fallback=parsed,
                            strict_expected=True,
                        )
                    if not isinstance(projected, FunctionSnapshot):
                        raise OrphanReviewError(
                            f"value XML artifact {record.relative_path!r} cannot be projected from typed rows"
                        )
                    data = value_adapter.render(projected, display_version=state.display_version)
                    checked = value_adapter.read(data)
                    if checked.name != parsed.name or checked.display_version != state.display_version:
                        raise OrphanReviewError(
                            f"rendered value XML artifact {record.relative_path!r} failed identity/version validation"
                        )
                    return ArtifactRecord(record.relative_path, record.kind, data, value_adapter.read)

                parsed = read_stub(record.data)
                if parsed.display_version != previous_state.display_version:
                    raise OrphanReviewError(
                        f"stub XML artifact {record.relative_path!r} version disagrees with accepted state"
                    )
                # XML called-function names are authoritative aliases from the
                # exact orphan StubState, not the state cleanup key alone.
                remove_names = stub_aliases_to_remove.get(state.function_key, set())
                root = parsed.raw_root
                if remove_names and root is None:
                    raise OrphanReviewError(
                        f"stub XML artifact {record.relative_path!r} has no lossless raw payload"
                    )
                remaining = tuple(
                    function
                    for function in parsed.called_functions
                    if function.name not in remove_names
                )
                if root is not None and remove_names:
                    called_parent = root.find("called_functions")
                    if called_parent is None:
                        raise OrphanReviewError(
                            f"stub XML artifact {record.relative_path!r} has no called_functions container"
                        )
                    for child in list(called_parent):
                        if child.tag == "function" and child.attrib.get("name") in remove_names:
                            called_parent.remove(child)
                candidate_snapshot = replace(
                    parsed,
                    called_functions=remaining,
                    display_version=state.display_version,
                    legacy_metadata=False,
                    _raw_root=clone_element(root),
                )
                data = stub_adapter.render(candidate_snapshot, display_version=state.display_version)
                checked = stub_adapter.read(data)
                if checked.function_name != parsed.function_name or checked.display_version != state.display_version:
                    raise OrphanReviewError(
                        f"rendered stub XML artifact {record.relative_path!r} failed identity/version validation"
                    )
                return ArtifactRecord(record.relative_path, record.kind, data, stub_adapter.read)

            candidate_artifacts: list[ArtifactRecord] = []
            seen_xml_projections: set[tuple[str, str, str]] = set()
            for record in accepted.artifacts:
                path = record.relative_path
                if path in _CLEANUP_INTERNAL_ARTIFACTS or _CLEANUP_STATE_ARTIFACT.fullmatch(path):
                    continue
                if path in orphan_artifact_by_path:
                    continue
                if path == "audit/orphan_cleanup.json":
                    continue
                role = artifact_role(record)
                if role is None:
                    candidate_artifacts.append(record)
                    continue
                parsed = read_value(record.data) if role == "value" else read_stub(record.data)
                owner, slot = xml_owner(record, parsed, role)
                marker = (owner.function_key, slot, role)
                if marker in seen_xml_projections:
                    raise OrphanReviewError(
                        f"duplicate {slot} {role} XML artifacts are present for function {owner.function_key!r}"
                    )
                seen_xml_projections.add(marker)
                candidate_artifacts.append(render_xml(record, role, owner, slot))

            action_payload = [
                {
                    "entity_type": item.entity_type,
                    "identity": item.identity,
                    "function_key": item.function_key,
                    "action": record.action,
                }
                for item, record in decisions
            ]
            candidate_artifacts.append(
                ArtifactRecord(
                    "audit/orphan_cleanup.json",
                    "audit",
                    canonical_json_bytes(
                        {
                            "operation_id": operation_id,
                            "module_key": selected_module,
                            "accepted_revision": accepted.revision,
                            "accepted_generation": manifest.accepted_generation,
                            "timestamp": timestamp,
                            "actions": action_payload,
                        }
                    ),
                )
            )
            candidate = CandidateBundle(
                selected_module,
                candidate_manifest,
                tuple(candidate_states),
                tuple(candidate_artifacts),
                tuple(
                    stub
                    for stub in accepted.module_stub_state
                    if getattr(stub, "stub_key", None) not in set(orphan_stub_by_key)
                ),
                accepted.conflicts,
                operation_id,
            )
            transaction.stage(candidate)
            validation = transaction.validate()
            if not getattr(validation, "ok", False):
                transaction.abort()
                issue_conflicts = tuple(
                    anchor_conflict(issue.code, issue.message, items[0] if items else None)
                    for issue in getattr(validation, "issues", ())
                )
                if not issue_conflicts:
                    issue_conflicts = (anchor_conflict("ORPHAN_CLEANUP_VALIDATION_FAILED", "cleanup candidate validation failed"),)
                return OrphanCleanupOutcome(
                    None,
                    plans,
                    issue_conflicts,
                    False,
                    accepted,
                    "cleanup candidate validation failed",
                    operation_id,
                )
            accepted_after = transaction.commit()
            if accepted_after is None:
                accepted_after = repository.load(selected_module)
            if (
                getattr(accepted_after, "revision", None) != accepted.revision + 1
                or getattr(accepted_after, "revision_id", None) == accepted.revision_id
            ):
                raise PublishedCommitLoadError(
                    "published cleanup revision did not match the expected repository transition"
                )
            # A successful transaction has executed and durably recorded every
            # action, including retain decisions.  Retain-only is handled by
            # the no-op branch above and therefore remains uncommitted.
            committed_plans = tuple(replace(plan, committed=True) for plan in plans)
            return OrphanCleanupOutcome(
                candidate,
                committed_plans,
                (),
                True,
                accepted_after,
                "orphan cleanup committed",
                operation_id,
            )
        except Exception as exc:
            error_code = getattr(exc, "code", None)
            published = (
                getattr(transaction, "state", None) == "committed"
                or isinstance(exc, (PublishedCommitDurabilityError, PublishedCommitLoadError))
                or error_code in {
                    PublishedCommitDurabilityError.code,
                    PublishedCommitLoadError.code,
                }
            )
            if published:
                try:
                    accepted_after = repository.load(selected_module)
                except Exception:
                    accepted_after = None
                conflict = anchor_conflict(
                    error_code if isinstance(error_code, str) and error_code else "ORPHAN_CLEANUP_POST_POINTER_UNCERTAIN",
                    (
                        "cleanup pointer was published; accepted revision was reloaded"
                        if accepted_after is not None
                        else "cleanup pointer was published; accepted state is unknown and requires explicit reload"
                    ),
                )
                committed_plans = tuple(replace(plan, committed=True) for plan in plans)
                return OrphanCleanupOutcome(
                    candidate,
                    committed_plans,
                    (conflict,),
                    True,
                    accepted_after,
                    conflict.message,
                    operation_id,
                )
            if transaction is not None and getattr(transaction, "state", None) != "committed":
                try:
                    transaction.abort()
                except Exception:
                    pass
            code = error_code if isinstance(error_code, str) and error_code else "ORPHAN_CLEANUP_COMMIT_FAILED"
            return OrphanCleanupOutcome(
                None,
                plans,
                (anchor_conflict(code, f"cleanup candidate was not accepted: {exc}"),),
                False,
                accepted,
                f"cleanup candidate was not accepted: {exc}",
                operation_id,
            )


@dataclass(frozen=True)
class FuzzySuggestion:
    """A ranked, non-authoritative candidate pair."""

    left_index: int
    right_index: int
    left_key: str
    right_key: str
    confidence: float
    authoritative: bool = False
    auto_commit: bool = False

    def __post_init__(self) -> None:
        if type(self.left_index) is not int or self.left_index < 0:
            raise FuzzySuggestionError("left_index must be non-negative")
        if type(self.right_index) is not int or self.right_index < 0:
            raise FuzzySuggestionError("right_index must be non-negative")
        _nonempty_string(self.left_key, "left_key")
        _nonempty_string(self.right_key, "right_key")
        if type(self.confidence) not in (int, float) or isinstance(self.confidence, bool) or not math.isfinite(float(self.confidence)):
            raise FuzzySuggestionError("confidence must be finite")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise FuzzySuggestionError("confidence must be between 0 and 1")
        if self.authoritative is not False or self.auto_commit is not False:
            raise FuzzySuggestionError("fuzzy suggestions are never authoritative or auto-committed")

    @property
    def score(self) -> float:
        return float(self.confidence)

    @property
    def source_index(self) -> int:
        return self.left_index

    @property
    def candidate_index(self) -> int:
        return self.right_index


def _extract(value: Any, names: Sequence[str]) -> Any:
    for name in names:
        item = _field(value, name, _MISSING)
        if item is not _MISSING and item is not None:
            return item
    meta = _field(value, "meta", None)
    if meta is not None:
        for name in names:
            item = _field(meta, name, _MISSING)
            if item is not _MISSING and item is not None:
                return item
    return None


def _call_key(key: Callable[[Any], Any] | str | None, value: Any) -> Any:
    if key is None:
        return None
    candidate = deepcopy(value)
    try:
        result = key(candidate) if callable(key) else _field(candidate, key, None)
    except Exception as exc:
        raise FuzzySuggestionError("exact/similarity key failed") from exc
    return result


def _canonical_key(value: Any) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    except Exception:
        return f"{type(value).__module__}.{type(value).__qualname__}:{repr(value)}"


def _identity_tokens(value: Any, exact_key: Callable[[Any], Any] | str | None) -> tuple[tuple[str, Any], ...]:
    tokens: list[tuple[str, Any]] = []
    row_id = _extract(value, ("row_id",))
    if isinstance(row_id, str) and row_id:
        tokens.append(("row_id", row_id))
    obligations = _extract(value, ("obligation_keys", "obligations"))
    if obligations:
        try:
            normalized = tuple(sorted(str(item) for item in obligations))
        except TypeError as exc:
            raise FuzzySuggestionError("obligation identity must be iterable") from exc
        if normalized:
            tokens.append(("obligations", normalized))
    stub_key = _extract(value, ("stub_key",))
    if isinstance(stub_key, str) and stub_key:
        tokens.append(("stub_key", stub_key))
    # A function key is an exact identity only for function-level candidates;
    # row metadata also carries its owning function and must not collapse all
    # rows in that function into one exact match.
    entity_type = _field(value, "entity_type", None)
    is_function = entity_type == "function" or (
        _field(value, "working", _MISSING) is not _MISSING
        and _field(value, "values", _MISSING) is _MISSING
        and _field(value, "meta", _MISSING) is _MISSING
    )
    function_key = _extract(value, ("function_key",))
    if is_function and isinstance(function_key, str) and function_key:
        tokens.append(("function_key", function_key))
    # Explicit exact_key is deliberately last.  It is a conservative escape
    # hatch and must never steal a row_id/obligation identity tier.
    explicit = _call_key(exact_key, value)
    if explicit is not None:
        tokens.append(("explicit", _canonical_key(explicit)))
    return tuple(tokens)


def _identity_token(value: Any, exact_key: Callable[[Any], Any] | str | None) -> tuple[str, Any] | None:
    """Compatibility helper returning the highest-precedence token only."""

    tokens = _identity_tokens(value, exact_key)
    return tokens[0] if tokens else None


def _sort_key(value: Any, identities: Sequence[tuple[str, Any]], index: int) -> str:
    if identities:
        identity = identities[0]
        return f"{identity[0]}:{_canonical_key(identity[1])}"
    return f"value:{_canonical_key(value)}:{index}"


def _similarity_value(
    left: Any,
    right: Any,
    scorer: Callable[[Any, Any], float] | None,
    similarity_key: Callable[[Any], Any] | str | None,
) -> float:
    try:
        if scorer is not None:
            score = scorer(deepcopy(left), deepcopy(right))
        else:
            left_value = _call_key(similarity_key, left)
            right_value = _call_key(similarity_key, right)
            if left_value is None or right_value is None:
                return 0.0
            score = SequenceMatcher(None, str(left_value), str(right_value)).ratio()
    except Exception as exc:
        raise FuzzySuggestionError("fuzzy scorer failed") from exc
    if type(score) not in (int, float) or isinstance(score, bool) or not math.isfinite(float(score)):
        raise FuzzySuggestionError("fuzzy scorer must return a finite number")
    score = float(score)
    if not 0.0 <= score <= 1.0:
        raise FuzzySuggestionError("fuzzy confidence must be between 0 and 1")
    return score


def suggest_fuzzy_matches(
    left: Iterable[Any],
    right: Iterable[Any],
    *,
    scorer: Callable[[Any, Any], float] | None = None,
    threshold: float = 0.5,
    exact_key: Callable[[Any], Any] | str | None = None,
    similarity_key: Callable[[Any], Any] | str | None = None,
) -> tuple[FuzzySuggestion, ...]:
    """Suggest pairs only after deterministic exact identities are removed."""

    if type(threshold) not in (int, float) or isinstance(threshold, bool) or not math.isfinite(float(threshold)):
        raise FuzzySuggestionError("threshold must be finite")
    threshold = float(threshold)
    if not 0.0 <= threshold <= 1.0:
        raise FuzzySuggestionError("threshold must be between 0 and 1")
    if scorer is not None and not callable(scorer):
        raise FuzzySuggestionError("scorer must be callable")
    left_values = tuple(left)
    right_values = tuple(right)
    left_tokens = [_identity_tokens(value, exact_key) for value in left_values]
    right_tokens = [_identity_tokens(value, exact_key) for value in right_values]
    matched_left: set[int] = set()
    matched_right: set[int] = set()

    # Existing exact precedence: persistent row identity, structured
    # obligations, then caller-supplied conservative exact identity.
    for tier in ("row_id", "obligations", "stub_key", "function_key", "explicit"):
        left_map: dict[Any, list[int]] = {}
        right_map: dict[Any, list[int]] = {}
        for index, tokens in enumerate(left_tokens):
            if index not in matched_left:
                for token in tokens:
                    if token[0] == tier:
                        left_map.setdefault(token[1], []).append(index)
        for index, tokens in enumerate(right_tokens):
            if index not in matched_right:
                for token in tokens:
                    if token[0] == tier:
                        right_map.setdefault(token[1], []).append(index)
        for token in sorted(set(left_map) & set(right_map), key=lambda value: _canonical_key(value)):
            if len(left_map[token]) > 1 or len(right_map[token]) > 1:
                raise FuzzySuggestionError("duplicate exact identity is ambiguous")
            matched_left.add(left_map[token][0])
            matched_right.add(right_map[token][0])

    unmatched_left = [index for index in range(len(left_values)) if index not in matched_left]
    unmatched_right = [index for index in range(len(right_values)) if index not in matched_right]
    suggestions: list[FuzzySuggestion] = []
    for left_index in unmatched_left:
        for right_index in unmatched_right:
            confidence = _similarity_value(left_values[left_index], right_values[right_index], scorer, similarity_key)
            if confidence < threshold:
                continue
            suggestions.append(
                FuzzySuggestion(
                    left_index,
                    right_index,
                    _sort_key(left_values[left_index], left_tokens[left_index], left_index),
                    _sort_key(right_values[right_index], right_tokens[right_index], right_index),
                    confidence,
                )
            )
    suggestions.sort(
        key=lambda item: (-float(item.confidence), item.left_key, item.right_key, item.left_index, item.right_index)
    )
    return tuple(suggestions)


__all__ = [
    "FuzzySuggestion",
    "FuzzySuggestionError",
    "OrphanActionPlan",
    "OrphanCleanupOutcome",
    "OrphanReviewError",
    "OrphanReviewItem",
    "OrphanReviewService",
    "collect_orphans",
    "suggest_fuzzy_matches",
]
