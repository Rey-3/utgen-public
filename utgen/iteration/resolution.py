"""Explicit, replayable conflict resolutions.

This module deliberately owns only the resolution record and its audit file.
It never edits an accepted bundle, and it does not attempt fuzzy matching or
orphan cleanup.  A resolution is tied to the exact BASE/WORKING/NEW snapshot
context and to the complete conflict payload which was shown to the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping
import uuid

from .model import MergeConflict, UnknownFieldError, canonical_json_bytes, from_dict
from .transaction import AdvisoryFileLock, WriterLockError, fsync_directory


_SCHEMA_VERSION = 1
_TIMESTAMP = re.compile(
    r"^(?P<local>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})$"
)
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


class ResolutionError(ValueError):
    """Base class for structured, fail-closed resolution errors."""

    code = "RESOLUTION_ERROR"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


class ResolutionValidationError(ResolutionError):
    code = "RESOLUTION_INVALID"


class ResolutionUnknownFieldError(ResolutionValidationError):
    code = "RESOLUTION_UNKNOWN_FIELD"


class ResolutionConflictError(ResolutionError):
    code = "RESOLUTION_CONFLICT"


class ResolutionPersistenceError(ResolutionError):
    code = "RESOLUTION_PERSISTENCE_ERROR"


class ResolutionConcurrentUpdate(ResolutionPersistenceError):
    code = "CONCURRENT_STATE_UPDATE"


class ResolutionUnresolvedError(ResolutionError):
    code = "UNRESOLVED_CONFLICT"


# Names used by callers which prefer the service-oriented spelling.
ConflictResolutionError = ResolutionError
ConflictResolutionValidationError = ResolutionValidationError
ConflictResolutionPersistenceError = ResolutionPersistenceError
ConflictResolutionConcurrentUpdate = ResolutionConcurrentUpdate


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ResolutionUnknownFieldError(
            f"{name} contains unknown field(s): {', '.join(unknown)}",
            details={"field": name, "unknown": unknown},
        )


def _require(data: Mapping[str, Any], key: str, name: str) -> Any:
    value = data.get(key, _MISSING)
    if value is _MISSING:
        raise ResolutionValidationError(f"{name} is missing required field {key!r}")
    return value


def _string(value: Any, field: str, *, nonempty: bool = False) -> str:
    if type(value) is not str:
        raise ResolutionValidationError(f"{field} must be a string")
    if nonempty and not value:
        raise ResolutionValidationError(f"{field} must not be empty")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ResolutionValidationError("revision must be a non-negative integer")
    return value


def _timestamp(value: Any) -> str:
    """Validate the same second-precision timestamp form used by iteration."""

    value = _string(value, "timestamp", nonempty=True)
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ResolutionValidationError(
            "timestamp must use YYYY-MM-DDTHH:MM:SSZ or an explicit offset"
        )
    local = match.group("local")
    zone = match.group("zone")
    try:
        datetime.strptime(local, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ResolutionValidationError("timestamp is not a valid date/time") from exc
    if zone != "Z":
        hours = int(zone[1:3])
        minutes = int(zone[4:6])
        if hours > 23 or minutes > 59:
            raise ResolutionValidationError("timestamp has an invalid timezone offset")
        if hours == 0 and minutes == 0:
            return f"{local}Z"
    return f"{local}{zone}"


@dataclass(frozen=True)
class ResolutionContext:
    """Exact accepted context against which a decision may be replayed."""

    module_key: str
    base_snapshot_id: str
    working_snapshot_id: str
    new_snapshot_id: str
    revision: int

    def __post_init__(self) -> None:
        _string(self.module_key, "module_key", nonempty=True)
        _string(self.base_snapshot_id, "base_snapshot_id", nonempty=True)
        _string(self.working_snapshot_id, "working_snapshot_id", nonempty=True)
        _string(self.new_snapshot_id, "new_snapshot_id", nonempty=True)
        _revision(self.revision)

    @property
    def snapshot_ids(self) -> tuple[str, str, str]:
        return (self.base_snapshot_id, self.working_snapshot_id, self.new_snapshot_id)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "module_key": self.module_key,
            "base_snapshot_id": self.base_snapshot_id,
            "working_snapshot_id": self.working_snapshot_id,
            "new_snapshot_id": self.new_snapshot_id,
            "revision": self.revision,
        }
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResolutionContext":
        if not isinstance(data, Mapping):
            raise ResolutionValidationError("ResolutionContext must be an object")
        _reject_unknown(
            data,
            {
                "module_key", "base_snapshot_id", "working_snapshot_id", "new_snapshot_id", "revision",
            },
            "ResolutionContext",
        )
        return cls(
            _require(data, "module_key", "ResolutionContext"),
            _require(data, "base_snapshot_id", "ResolutionContext"),
            _require(data, "working_snapshot_id", "ResolutionContext"),
            _require(data, "new_snapshot_id", "ResolutionContext"),
            _require(data, "revision", "ResolutionContext"),
        )


@dataclass(frozen=True)
class ResolutionDecision:
    """A user's explicit action for one stable conflict key."""

    conflict_key: str
    action: str
    actor: str
    reason: str
    timestamp: str
    context: ResolutionContext

    def __post_init__(self) -> None:
        _string(self.conflict_key, "conflict_key", nonempty=True)
        _string(self.action, "action", nonempty=True)
        _string(self.actor, "actor", nonempty=True)
        _string(self.reason, "reason")
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))
        if not isinstance(self.context, ResolutionContext):
            raise ResolutionValidationError("context must be a ResolutionContext")

    # Explicitly expose every binding required by the resolution contract.
    # Keeping these as projections makes the exact context available to
    # Merge A/B integrations and API callers without duplicating context
    # fields in the decision record.
    @property
    def module_key(self) -> str:
        return self.context.module_key

    @property
    def base_snapshot_id(self) -> str:
        return self.context.base_snapshot_id

    @property
    def working_snapshot_id(self) -> str:
        return self.context.working_snapshot_id

    @property
    def new_snapshot_id(self) -> str:
        return self.context.new_snapshot_id

    @property
    def accepted_revision(self) -> int:
        return self.context.revision

    @property
    def reason_text(self) -> str:
        return self.reason

    def to_dict(self) -> dict[str, Any]:
        result = {
            "conflict_key": self.conflict_key,
            "action": self.action,
            "actor": self.actor,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "context": self.context.to_dict(),
        }
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResolutionDecision":
        if not isinstance(data, Mapping):
            raise ResolutionValidationError("ResolutionDecision must be an object")
        _reject_unknown(
            data,
            {
                "conflict_key", "action", "actor", "reason", "timestamp", "context",
            },
            "ResolutionDecision",
        )
        return cls(
            _require(data, "conflict_key", "ResolutionDecision"),
            _require(data, "action", "ResolutionDecision"),
            _require(data, "actor", "ResolutionDecision"),
            _require(data, "reason", "ResolutionDecision"),
            _require(data, "timestamp", "ResolutionDecision"),
            ResolutionContext.from_dict(_require(data, "context", "ResolutionDecision")),
        )


@dataclass(frozen=True)
class ResolutionAudit:
    """One append-only operational audit event."""

    conflict_key: str
    action: str
    actor: str
    reason: str
    timestamp: str
    context: ResolutionContext
    payload_digest: str

    def __post_init__(self) -> None:
        _string(self.conflict_key, "conflict_key", nonempty=True)
        _string(self.action, "action", nonempty=True)
        _string(self.actor, "actor", nonempty=True)
        _string(self.reason, "reason")
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))
        if not isinstance(self.context, ResolutionContext):
            raise ResolutionValidationError("audit.context must be a ResolutionContext")
        _digest(self.payload_digest, "payload_digest")

    @classmethod
    def from_decision(cls, decision: ResolutionDecision, payload_digest: str) -> "ResolutionAudit":
        return cls(
            decision.conflict_key,
            decision.action,
            decision.actor,
            decision.reason,
            decision.timestamp,
            decision.context,
            payload_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_key": self.conflict_key,
            "action": self.action,
            "actor": self.actor,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "context": self.context.to_dict(),
            "payload_digest": self.payload_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResolutionAudit":
        if not isinstance(data, Mapping):
            raise ResolutionValidationError("ResolutionAudit must be an object")
        _reject_unknown(
            data,
            {"conflict_key", "action", "actor", "reason", "timestamp", "context", "payload_digest"},
            "ResolutionAudit",
        )
        return cls(
            _require(data, "conflict_key", "ResolutionAudit"),
            _require(data, "action", "ResolutionAudit"),
            _require(data, "actor", "ResolutionAudit"),
            _require(data, "reason", "ResolutionAudit"),
            _require(data, "timestamp", "ResolutionAudit"),
            ResolutionContext.from_dict(_require(data, "context", "ResolutionAudit")),
            _require(data, "payload_digest", "ResolutionAudit"),
        )


@dataclass(frozen=True)
class ResolutionRecord:
    """Complete conflict payload plus its explicit decision and audit event."""

    conflict: MergeConflict
    decision: ResolutionDecision
    payload_digest: str
    audit: ResolutionAudit | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.conflict, MergeConflict):
            raise ResolutionValidationError("conflict must be a MergeConflict")
        if not isinstance(self.decision, ResolutionDecision):
            raise ResolutionValidationError("decision must be a ResolutionDecision")
        _digest(self.payload_digest, "payload_digest")
        if self.audit is not None and not isinstance(self.audit, ResolutionAudit):
            raise ResolutionValidationError("audit must be a ResolutionAudit or null")
        _validate_record(self)

    @property
    def conflict_key(self) -> str:
        return self.decision.conflict_key

    @property
    def action(self) -> str:
        return self.decision.action

    @property
    def actor(self) -> str:
        return self.decision.actor

    @property
    def reason(self) -> str:
        return self.decision.reason

    @property
    def timestamp(self) -> str:
        return self.decision.timestamp

    @property
    def context(self) -> ResolutionContext:
        return self.decision.context

    @property
    def base(self) -> object:
        return self.conflict.base

    @property
    def user(self) -> object:
        return self.conflict.user

    @property
    def new(self) -> object:
        return self.conflict.new

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "conflict": self.conflict.to_dict(),
            "decision": self.decision.to_dict(),
            "payload_digest": self.payload_digest,
        }
        if self.audit is not None:
            result["audit"] = self.audit.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResolutionRecord":
        if not isinstance(data, Mapping):
            raise ResolutionValidationError("ResolutionRecord must be an object")
        _reject_unknown(data, {"conflict", "decision", "payload_digest", "audit"}, "ResolutionRecord")
        try:
            conflict = from_dict(_require(data, "conflict", "ResolutionRecord"), MergeConflict)
        except UnknownFieldError as exc:
            raise ResolutionUnknownFieldError(str(exc)) from exc
        decision = ResolutionDecision.from_dict(_require(data, "decision", "ResolutionRecord"))
        payload_digest = _require(data, "payload_digest", "ResolutionRecord")
        audit_data = data.get("audit", None)
        audit = None if audit_data is None else ResolutionAudit.from_dict(audit_data)
        record = cls(conflict, decision, payload_digest, audit)
        _validate_record(record)
        return record


def _digest(value: Any, field: str = "digest") -> str:
    value = _string(value, field, nonempty=True)
    if _HEX_DIGEST.fullmatch(value) is None:
        raise ResolutionValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _conflict_payload(conflict: MergeConflict) -> dict[str, Any]:
    if not isinstance(conflict, MergeConflict):
        raise ResolutionValidationError("conflict must be a MergeConflict")
    # Calling to_dict validates the complete BASE/USER/NEW payload and keeps
    # those three values available in every persisted record.
    return conflict.to_dict()


def conflict_payload_digest(conflict: MergeConflict) -> str:
    return hashlib.sha256(canonical_json_bytes(_conflict_payload(conflict))).hexdigest()


def unscoped_conflict_key(conflict: MergeConflict) -> str:
    """Return the legacy identity hash without a module namespace.

    This helper is intentionally explicit.  Persisted resolutions must use
    :func:`stable_conflict_key`, because identical row/function identities in
    two modules are independent conflicts.
    """

    payload = _conflict_payload(conflict)
    identity = {
        key: payload[key]
        for key in (
            "code",
            "entity_type",
            "function_key",
            "row_id",
            "case_name",
            "logical_argument_key",
            "stub_key",
        )
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def stable_conflict_key(conflict: MergeConflict, module_key: str | None = None) -> str:
    """Return a deterministic key for the conflict identity.

    Human messages and allowed actions are intentionally excluded: a policy
    may improve its wording while the same logical conflict remains pending.
    The complete payload is separately hashed and checked on record/replay.
    ``module_key`` is mandatory for persisted/scoped identity.
    """

    module_key = _string(module_key, "module_key", nonempty=True)
    payload = _conflict_payload(conflict)
    identity = {
        "module_key": module_key,
        **{
            key: payload[key]
            for key in (
                "code",
                "entity_type",
                "function_key",
                "row_id",
                "case_name",
                "logical_argument_key",
                "stub_key",
            )
        },
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


# Friendly aliases used by integrations and tests.
conflict_key = stable_conflict_key
make_conflict_key = stable_conflict_key


def _json_load(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ResolutionValidationError("resolution state must not contain a UTF-8 BOM")

    def reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ResolutionValidationError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ResolutionValidationError(f"non-standard JSON constant {value!r} is not accepted")

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_pairs,
            parse_constant=reject_constant,
        )
    except ResolutionError:
        raise
    except Exception as exc:
        raise ResolutionValidationError("resolution state is not valid UTF-8 JSON") from exc


def _state_from_bytes(data: bytes) -> tuple[dict[str, ResolutionRecord], list[ResolutionAudit]]:
    parsed = _json_load(data)
    if not isinstance(parsed, Mapping):
        raise ResolutionValidationError("resolution state root must be an object")
    _reject_unknown(parsed, {"schema_version", "records", "audit"}, "resolution state")
    if parsed.get("schema_version") != _SCHEMA_VERSION:
        raise ResolutionValidationError("unsupported resolution state schema_version")
    records_raw = parsed.get("records", _MISSING)
    audits_raw = parsed.get("audit", _MISSING)
    if type(records_raw) is not list or type(audits_raw) is not list:
        raise ResolutionValidationError("resolution state records and audit must be arrays")
    records: dict[str, ResolutionRecord] = {}
    for raw in records_raw:
        record = ResolutionRecord.from_dict(raw)
        key = stable_conflict_key(record.conflict, record.decision.context.module_key)
        if key != record.decision.conflict_key:
            raise ResolutionConflictError("record conflict key does not match conflict identity")
        if key in records:
            raise ResolutionConflictError("resolution state contains a duplicate conflict key")
        expected_payload = conflict_payload_digest(record.conflict)
        if expected_payload != record.payload_digest:
            raise ResolutionConflictError("record conflict payload digest does not match payload")
        _validate_record(record, expected_payload)
        records[key] = record
    audits: list[ResolutionAudit] = []
    for raw in audits_raw:
        audit = ResolutionAudit.from_dict(raw)
        audits.append(audit)
    for audit in audits:
        record = records.get(audit.conflict_key)
        if record is None:
            raise ResolutionConflictError("audit references an unknown conflict key")
        expected_audit_key = stable_conflict_key(record.conflict, audit.context.module_key)
        if expected_audit_key != audit.conflict_key or audit.context != record.context:
            raise ResolutionConflictError("audit context/module does not match its resolution record")
        if record.audit is not None and audit != record.audit:
            raise ResolutionConflictError("audit does not match its resolution record")
    # A record's audit is optional for direct model round-trips, but persisted
    # state always carries precisely one matching audit event.
    for record in records.values():
        if record.audit is None or record.audit not in audits:
            raise ResolutionConflictError("persisted record is missing its audit event")
    expected = canonical_json_bytes(parsed)
    if expected != data:
        raise ResolutionValidationError("resolution state is not canonical JSON")
    return records, audits


def _validate_record(record: ResolutionRecord, payload_digest: str | None = None) -> None:
    key = stable_conflict_key(record.conflict, record.decision.context.module_key)
    if key != record.decision.conflict_key:
        raise ResolutionConflictError(
            "decision conflict_key does not match conflict identity",
            details={"expected": key, "actual": record.decision.conflict_key},
        )
    if payload_digest is None:
        payload_digest = conflict_payload_digest(record.conflict)
    if payload_digest != record.payload_digest:
        raise ResolutionConflictError("resolution payload has changed")
    if record.decision.action not in record.conflict.allowed_resolutions:
        raise ResolutionConflictError(
            "resolution action is not allowed for this conflict",
            details={"action": record.decision.action, "allowed": list(record.conflict.allowed_resolutions)},
        )
    if record.audit is not None:
        expected_audit = ResolutionAudit.from_decision(record.decision, record.payload_digest)
        if record.audit != expected_audit:
            raise ResolutionConflictError("resolution audit does not match its decision")


def _state_bytes(records: Mapping[str, ResolutionRecord], audits: Iterable[ResolutionAudit]) -> bytes:
    ordered_records = [records[key].to_dict() for key in sorted(records)]
    # Audit order is an observable append-only history.  Keep the caller's
    # insertion order; canonical JSON sorts object keys, not event history.
    ordered_audits = [audit.to_dict() for audit in audits]
    return canonical_json_bytes({"schema_version": _SCHEMA_VERSION, "records": ordered_records, "audit": ordered_audits})


FailureInjector = Callable[[str], None]


class ConflictResolutionRegistry:
    """In-memory resolution registry with optional one-file atomic storage."""

    DEFAULT_FILENAME = "resolutions.json"

    def __init__(
        self,
        path: str | os.PathLike[str] | Iterable[MergeConflict] | None = None,
        *,
        state_root: str | os.PathLike[str] | None = None,
        state_path: str | os.PathLike[str] | None = None,
        conflicts: Iterable[MergeConflict] | None = None,
        context: ResolutionContext | None = None,
        expected_digest: str | None = None,
        failure_injector: FailureInjector | None = None,
        clock: Callable[[], str] | None = None,
        lock_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if conflicts is None and path is not None and not isinstance(path, (str, os.PathLike)):
            conflicts = path
            path = None
        supplied_paths = [item for item in (path, state_path) if item is not None]
        if len(supplied_paths) > 1:
            raise ResolutionValidationError("provide only one resolution state path")
        selected = supplied_paths[0] if supplied_paths else None
        if selected is not None:
            selected_path = Path(selected)
            if selected_path.exists() and selected_path.is_dir():
                selected_path = selected_path / self.DEFAULT_FILENAME
        elif state_root is not None:
            selected_path = Path(state_root) / self.DEFAULT_FILENAME
        else:
            selected_path = None
        self.path: Path | None = selected_path
        self.state_path = selected_path
        self.lock_path: Path | None = (
            Path(lock_path)
            if lock_path is not None
            else (selected_path.with_name(selected_path.name + ".lock") if selected_path is not None else None)
        )
        self.failure_injector = failure_injector
        self.clock = clock or (lambda: datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._records: dict[str, ResolutionRecord] = {}
        self._audit: list[ResolutionAudit] = []
        self._known_conflicts: dict[str, MergeConflict] = {}
        self._digest: str | None = None
        if self.path is not None and self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise ResolutionPersistenceError("resolution state path is not a regular file")
            try:
                raw = self.path.read_bytes()
            except OSError as exc:
                raise ResolutionPersistenceError("cannot read resolution state") from exc
            self._records, self._audit = _state_from_bytes(raw)
            self._known_conflicts = {key: record.conflict for key, record in self._records.items()}
            self._digest = hashlib.sha256(raw).hexdigest()
        if expected_digest is not None and expected_digest != self._digest:
            raise ResolutionConcurrentUpdate(
                "expected resolution state digest does not match",
                details={"expected": expected_digest, "actual": self._digest},
            )
        if context is not None and not isinstance(context, ResolutionContext):
            raise ResolutionValidationError("context must be a ResolutionContext")
        if conflicts is not None:
            self._remember_conflicts(conflicts, None if context is None else context.module_key)

    @property
    def digest(self) -> str | None:
        return self._digest

    @property
    def expected_digest(self) -> str | None:
        return self._digest

    @property
    def records(self) -> tuple[ResolutionRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    @property
    def audit(self) -> tuple[ResolutionAudit, ...]:
        return tuple(self._audit)

    @property
    def audits(self) -> tuple[ResolutionAudit, ...]:
        return self.audit

    def _inject(self, stage: str) -> None:
        if self.failure_injector is not None:
            self.failure_injector(stage)

    def _remember_conflicts(
        self,
        conflicts: Iterable[MergeConflict],
        module_key: str | None = None,
    ) -> tuple[MergeConflict, ...]:
        if module_key is None:
            raise ResolutionValidationError(
                "module_key/context is required when registering conflicts"
            )
        _string(module_key, "module_key", nonempty=True)
        try:
            items = tuple(conflicts)
        except TypeError as exc:
            raise ResolutionValidationError("conflicts must be iterable") from exc
        for conflict in items:
            key = stable_conflict_key(conflict, module_key)
            conflict_payload_digest(conflict)
            previous = self._known_conflicts.get(key)
            if previous is not None and conflict_payload_digest(previous) != conflict_payload_digest(conflict):
                raise ResolutionConflictError("same conflict key has different payload")
            self._known_conflicts[key] = conflict
        return tuple(self._known_conflicts[key] for key in sorted(self._known_conflicts))

    def list_conflicts(
        self,
        conflicts: Iterable[MergeConflict] | ResolutionContext | None = None,
        *,
        context: ResolutionContext | None = None,
    ) -> tuple[MergeConflict, ...]:
        if isinstance(conflicts, ResolutionContext):
            if context is not None and context != conflicts:
                raise ResolutionConflictError("two different resolution contexts were supplied")
            context = conflicts
            conflicts = None
        if context is not None and not isinstance(context, ResolutionContext):
            raise ResolutionValidationError("context must be a ResolutionContext")
        if conflicts is not None:
            if context is None:
                raise ResolutionValidationError(
                    "context is required to scope supplied conflicts"
                )
            return self._remember_conflicts(conflicts, context.module_key)
        return tuple(self._known_conflicts[key] for key in sorted(self._known_conflicts))

    def _current_disk_digest(self) -> str | None:
        if self.path is None:
            return None
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise ResolutionPersistenceError("cannot read resolution state for CAS") from exc
        return hashlib.sha256(raw).hexdigest()

    def _atomic_write(self, data: bytes, expected_digest: str | None) -> str:
        if self.path is None:
            return hashlib.sha256(data).hexdigest()
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ResolutionPersistenceError("cannot create resolution state directory") from exc
        if self.lock_path is None:
            raise ResolutionPersistenceError("resolution state has no writer lock path")
        writer_lock = AdvisoryFileLock(self.lock_path)
        try:
            try:
                writer_lock.acquire()
            except WriterLockError as exc:
                raise ResolutionConcurrentUpdate(
                    "resolution state writer lock is unavailable",
                    details={"lock_path": str(self.lock_path)},
                ) from exc

            current = self._current_disk_digest()
            if current != expected_digest:
                raise ResolutionConcurrentUpdate(
                    "resolution state changed before commit",
                    details={"expected": expected_digest, "actual": current},
                )
            if self.path.is_symlink():
                raise ResolutionPersistenceError("resolution state path must not be a symlink")
            temporary = parent / f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            replaced = False

            def candidate_visible() -> bool:
                try:
                    return (
                        not self.path.is_symlink()
                        and self.path.is_file()
                        and self.path.read_bytes() == data
                    )
                except OSError:
                    return False

            try:
                self._inject("before_write")
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._inject("after_write")
                self._inject("before_replace")
                os.replace(temporary, self.path)
                replaced = True
                self._inject("after_replace")
                self._inject("before_directory_fsync")
                fsync_directory(parent)
                self._inject("after_directory_fsync")
            except Exception as exc:
                # os.replace and post-publication durability hooks can report
                # an error after the candidate is already visible.  Confirm
                # the exact bytes and treat that complete candidate as a
                # committed operation; never attempt a second replacement.
                if candidate_visible():
                    return hashlib.sha256(data).hexdigest()
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                if isinstance(exc, ResolutionError):
                    raise
                raise ResolutionPersistenceError(
                    "atomic resolution state write failed",
                    details={"uncertain": bool(replaced)},
                ) from exc
            return hashlib.sha256(data).hexdigest()
        finally:
            writer_lock.release()

    def record(
        self,
        conflict: MergeConflict,
        decision: ResolutionDecision | None = None,
        *,
        expected_digest: str | None = None,
        context: ResolutionContext | None = None,
        action: str | None = None,
        actor: str | None = None,
        reason: str | None = None,
        timestamp: str | None = None,
    ) -> ResolutionRecord:
        if not isinstance(conflict, MergeConflict):
            raise ResolutionValidationError("conflict must be a MergeConflict")
        payload_digest = conflict_payload_digest(conflict)
        if decision is None:
            if action is None:
                raise ResolutionConflictError("an explicit action is required; no default action exists")
            if context is None:
                raise ResolutionValidationError("context is required when constructing a decision")
            if actor is None or reason is None or timestamp is None:
                raise ResolutionValidationError("actor, reason, and timestamp are required")
            decision = ResolutionDecision(
                stable_conflict_key(conflict, context.module_key),
                action,
                actor,
                reason,
                timestamp,
                context,
            )
        elif not isinstance(decision, ResolutionDecision):
            raise ResolutionValidationError("decision must be a ResolutionDecision")
        if context is not None and decision.context != context:
            raise ResolutionConflictError("decision context does not match supplied context")
        key = stable_conflict_key(conflict, decision.context.module_key)
        if decision.conflict_key != key:
            raise ResolutionConflictError(
                "decision conflict_key does not match conflict identity",
                details={"expected": key, "actual": decision.conflict_key},
            )
        if decision.action not in conflict.allowed_resolutions:
            raise ResolutionConflictError(
                "resolution action is not in conflict.allowed_resolutions",
                details={"action": decision.action, "allowed": list(conflict.allowed_resolutions)},
            )
        audit = ResolutionAudit.from_decision(decision, payload_digest)
        candidate = ResolutionRecord(conflict, decision, payload_digest, audit)
        _validate_record(candidate, payload_digest)
        expected = self._digest if expected_digest is None else expected_digest
        # Even an idempotent retry must not silently succeed against a file
        # that changed since this registry was loaded.  New records perform
        # the authoritative check again while holding the writer lock.
        if self.path is not None:
            current = self._current_disk_digest()
            if current != expected:
                raise ResolutionConcurrentUpdate(
                    "resolution state changed before resolution lookup",
                    details={"expected": expected, "actual": current},
                )
        previous = self._records.get(key)
        if previous is not None:
            if previous.payload_digest == payload_digest and previous.decision == decision:
                return previous
            mismatch = {
                "payload": previous.payload_digest != payload_digest,
                "decision": previous.decision != decision,
                "context": previous.context != decision.context,
                "revision": previous.context.revision != decision.context.revision,
            }
            raise ResolutionConflictError(
                "a conflict key already has a different resolution/context/payload",
                details={name: value for name, value in mismatch.items() if value},
            )
        next_records = dict(self._records)
        next_records[key] = candidate
        next_audit = [*self._audit, audit]
        data = _state_bytes(next_records, next_audit)
        new_digest = self._atomic_write(data, expected)
        self._records = next_records
        self._audit = next_audit
        self._known_conflicts[key] = conflict
        self._digest = new_digest
        return candidate

    def lookup(
        self,
        conflict_or_key: MergeConflict | str,
        context: ResolutionContext | None = None,
        *,
        payload_digest: str | None = None,
    ) -> ResolutionRecord | None:
        if isinstance(conflict_or_key, MergeConflict):
            if context is None:
                raise ResolutionValidationError(
                    "context is required to scope a conflict lookup"
                )
            key = stable_conflict_key(conflict_or_key, context.module_key)
            actual_payload = conflict_payload_digest(conflict_or_key)
            if payload_digest is not None and payload_digest != actual_payload:
                raise ResolutionConflictError("lookup payload digest does not match conflict")
            payload_digest = actual_payload
        else:
            key = _string(conflict_or_key, "conflict_key", nonempty=True)
        record = self._records.get(key)
        if record is None:
            # A conflict with the same unscoped identity may legitimately be
            # recorded for another module.  Surface that as a context
            # mismatch instead of pretending it is unresolved; callers must
            # never replay module A's decision in module B.
            if isinstance(conflict_or_key, MergeConflict):
                identity = unscoped_conflict_key(conflict_or_key)
                if any(
                    unscoped_conflict_key(candidate.conflict) == identity
                    for candidate in self._records.values()
                ):
                    raise ResolutionConflictError(
                        "lookup context/module does not match recorded context"
                    )
            return None
        if context is not None and record.context != context:
            raise ResolutionConflictError("lookup context does not match recorded context")
        if payload_digest is not None and record.payload_digest != payload_digest:
            raise ResolutionConflictError("lookup conflict payload does not match recorded payload")
        return record

    def replay(
        self,
        conflicts: Iterable[MergeConflict] | ResolutionContext | None = None,
        context: ResolutionContext | None = None,
        *,
        require_all: bool = True,
    ) -> dict[str, ResolutionDecision]:
        if isinstance(conflicts, ResolutionContext):
            if context is not None and context != conflicts:
                raise ResolutionConflictError("two different resolution contexts were supplied")
            context = conflicts
            conflicts = None
        if context is not None and not isinstance(context, ResolutionContext):
            raise ResolutionValidationError("context must be a ResolutionContext")
        items = self.list_conflicts(conflicts, context=context)
        result: dict[str, ResolutionDecision] = {}
        for conflict in items:
            if context is None:
                raise ResolutionValidationError(
                    "context is required to replay supplied conflicts"
                )
            key = stable_conflict_key(conflict, context.module_key)
            record = self.lookup(conflict, context)
            if record is None:
                if require_all:
                    raise ResolutionUnresolvedError(
                        "replay encountered an unresolved conflict",
                        details={"conflict_key": key},
                    )
                continue
            result[key] = record.decision
        return result


class ConflictResolutionService:
    """Small façade suitable for a UI/API layer; persistence stays in registry."""

    def __init__(
        self,
        registry: ConflictResolutionRegistry | str | os.PathLike[str] | None = None,
        *,
        path: str | os.PathLike[str] | None = None,
        state_root: str | os.PathLike[str] | None = None,
        state_path: str | os.PathLike[str] | None = None,
        conflicts: Iterable[MergeConflict] | None = None,
        context: ResolutionContext | None = None,
        expected_digest: str | None = None,
        failure_injector: FailureInjector | None = None,
        clock: Callable[[], str] | None = None,
        lock_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if isinstance(registry, ConflictResolutionRegistry):
            if any(value is not None for value in (path, state_root, state_path, conflicts, context, expected_digest, failure_injector, clock, lock_path)):
                raise ResolutionValidationError("registry cannot be combined with construction options")
            self.registry = registry
        else:
            selected_path = path if path is not None else registry
            self.registry = ConflictResolutionRegistry(
                selected_path,
                state_root=state_root,
                state_path=state_path,
                conflicts=conflicts,
                context=context,
                expected_digest=expected_digest,
                failure_injector=failure_injector,
                clock=clock,
                lock_path=lock_path,
            )

    @property
    def path(self) -> Path | None:
        return self.registry.path

    @property
    def digest(self) -> str | None:
        return self.registry.digest

    @property
    def lock_path(self) -> Path | None:
        return self.registry.lock_path

    @property
    def audit(self) -> tuple[ResolutionAudit, ...]:
        return self.registry.audit

    @property
    def records(self) -> tuple[ResolutionRecord, ...]:
        return self.registry.records

    def list_conflicts(self, *args: Any, **kwargs: Any) -> tuple[MergeConflict, ...]:
        return self.registry.list_conflicts(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> ResolutionRecord:
        return self.registry.record(*args, **kwargs)

    def lookup(self, *args: Any, **kwargs: Any) -> ResolutionRecord | None:
        return self.registry.lookup(*args, **kwargs)

    def replay(self, *args: Any, **kwargs: Any) -> dict[str, ResolutionDecision]:
        return self.registry.replay(*args, **kwargs)


# ---------------------------------------------------------------------------
# Merge-service integration
# ---------------------------------------------------------------------------

# Merge services use only the typed context and decision lookup helpers below.
# Candidate mutation stays inside the production Merge B engine, where the
# original typed BASE/USER/NEW rows remain available; schema, identity,
# metadata, and validation conflicts remain blocking.
def resolution_context_for(
    module_key: str,
    *,
    base_snapshot_id: str,
    working_snapshot_id: str,
    new_snapshot_id: str,
    revision: int,
) -> ResolutionContext:
    """Construct a typed context at the exact Merge A/B boundary."""

    return ResolutionContext(
        module_key,
        base_snapshot_id,
        working_snapshot_id,
        new_snapshot_id,
        revision,
    )


def decision_for_conflict(
    conflict: MergeConflict,
    module_key: str,
    context: ResolutionContext,
    *,
    registry: Any | None = None,
    decisions: Mapping[str, ResolutionDecision] | Iterable[ResolutionDecision] | None = None,
) -> ResolutionDecision | None:
    """Resolve a conflict to one explicit persisted decision, if present."""

    if not isinstance(context, ResolutionContext):
        raise ResolutionValidationError("resolution context is required")
    if context.module_key != module_key:
        raise ResolutionConflictError("resolution context module does not match merge module")
    key = stable_conflict_key(conflict, module_key)
    found: ResolutionDecision | None = None
    if decisions is not None:
        if isinstance(decisions, Mapping):
            value = decisions.get(key)
            if isinstance(value, ResolutionRecord):
                if conflict_payload_digest(value.conflict) != conflict_payload_digest(conflict):
                    raise ResolutionConflictError(
                        "resolution decision payload does not match current conflict"
                    )
                value = value.decision
            if value is not None and not isinstance(value, ResolutionDecision):
                raise ResolutionValidationError("resolution_decisions must contain ResolutionDecision records")
            found = value
        else:
            for item in decisions:
                if isinstance(item, ResolutionRecord):
                    if conflict_payload_digest(item.conflict) != conflict_payload_digest(conflict):
                        # A stable identity can be reused when BASE/USER/NEW
                        # changed; never replay that decision against the new
                        # payload merely because the identity hash is equal.
                        if item.decision.conflict_key == key:
                            raise ResolutionConflictError(
                                "resolution decision payload does not match current conflict"
                            )
                    item = item.decision
                if not isinstance(item, ResolutionDecision):
                    raise ResolutionValidationError("resolution_decisions must contain ResolutionDecision records")
                if item.conflict_key == key:
                    found = item
                    break
    if found is None and registry is not None:
        lookup = getattr(registry, "lookup", None)
        if not callable(lookup):
            raise ResolutionValidationError("resolution registry must implement lookup()")
        try:
            result = lookup(conflict, context)
        except TypeError:
            result = lookup(key, context)
        if isinstance(result, ResolutionRecord):
            found = result.decision
        elif isinstance(result, ResolutionDecision):
            found = result
        elif result is not None:
            raise ResolutionValidationError("resolution registry lookup returned an invalid record")
    if found is None:
        return None
    if found.conflict_key != key:
        raise ResolutionConflictError("resolution decision conflict key does not match current conflict")
    if found.context != context:
        raise ResolutionConflictError("resolution decision context does not match current merge context")
    return found


__all__ = [
    "ConflictResolutionConcurrentUpdate",
    "ConflictResolutionError",
    "ConflictResolutionPersistenceError",
    "ConflictResolutionRegistry",
    "ConflictResolutionService",
    "ConflictResolutionValidationError",
    "ResolutionAudit",
    "ResolutionConcurrentUpdate",
    "ResolutionConflictError",
    "ResolutionContext",
    "ResolutionDecision",
    "ResolutionError",
    "ResolutionPersistenceError",
    "ResolutionRecord",
    "ResolutionUnknownFieldError",
    "ResolutionUnresolvedError",
    "ResolutionValidationError",
    "conflict_key",
    "conflict_payload_digest",
    "make_conflict_key",
    "stable_conflict_key",
    "unscoped_conflict_key",
]
