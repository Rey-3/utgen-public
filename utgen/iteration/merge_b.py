"""Exact source-regeneration (Merge B) semantics.

This module contains the pure part of the second iteration merge.  It accepts
one pure generator BASE, the accepted user-facing WORKING snapshot, and one
fresh pure generator NEW snapshot and returns a candidate WORKING snapshot.
There is deliberately no repository, XML, workbook, service, or version
transition code here.  PR13 owns lifecycle/orchestration and publication.

The important property of this implementation is that every automatic match
is exact.  The matcher uses persistent lineage when it is available, then
structured obligation provenance, and finally exact conservative signatures.
It never computes a similarity score and never uses a fuzzy suggestion to
pair rows.  A tie is represented as ``AMBIGUOUS_ROW_MATCH`` and the candidate
is rejected.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
import re
import uuid
from typing import Any, Callable

from .identity import allocate_case_name
from .model import (
    ArgumentState,
    BaseSnapshot,
    CellValue,
    FunctionIterationState,
    LogicalArgumentKey,
    MergeConflict,
    RowMeta,
    StubState,
    Tombstone,
    WorkingSnapshot,
)
from utgen.model import TestRow


_DELETED = object()
_UNKNOWN = object()
_DEFAULT_FUNCTION = "unknown_function"
_ROW_ID_RE = re.compile(r"^r_[0-9a-fA-F]{32}$")
_CASE_NUMBER_RE = re.compile(r"\.c([0-9]+)$")
_INTEGER_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
_NUMERIC_TYPE_RE = re.compile(
    r"(?P<unsigned>u|unsigned\s*)?(?:int|integer|short|long|char|byte|word)?"
    r"(?P<bits>8|16|24|32|64|128)?(?:_t)?$",
    re.IGNORECASE,
)


class MergeBError(ValueError):
    """Invalid Merge B input or an invalid explicit merge option."""


@dataclass(frozen=True)
class RowMatch:
    """One exact BASE-to-NEW match selected by :class:`MergeBEngine`.

    ``base_index`` and ``new_index`` are source tuple indexes and therefore
    are useful for deterministic audit output, but are not persisted identity.
    ``method`` is one of the documented exact precedence steps.
    """

    base_index: int
    new_index: int
    base_row_id: str | None
    new_row_id: str | None
    method: str
    score: int

    @property
    def row_id(self) -> str | None:
        return self.base_row_id or self.new_row_id


@dataclass(frozen=True)
class MergeBResult:
    """Pure result of one Merge B attempt.

    A blocking conflict always produces ``candidate=None``.  The diagnostic
    collections remain populated so callers can show deterministic match and
    orphan information without inspecting mutable state.
    """

    candidate: WorkingSnapshot | FunctionIterationState | None
    conflicts: tuple[MergeConflict, ...]
    changed: bool
    matches: tuple[RowMatch, ...] = ()
    added_row_ids: tuple[str, ...] = ()
    removed_row_ids: tuple[str, ...] = ()
    orphaned_row_ids: tuple[str, ...] = ()
    superseded_row_ids: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return any(conflict.severity == "blocking" for conflict in self.conflicts)

    @property
    def can_emit(self) -> bool:
        return self.candidate is not None and not self.blocked

    @property
    def candidate_working(self) -> WorkingSnapshot | None:
        if isinstance(self.candidate, FunctionIterationState):
            return self.candidate.working
        return self.candidate

    @property
    def candidate_state(self) -> WorkingSnapshot | FunctionIterationState | None:
        return self.candidate

    @property
    def working(self) -> WorkingSnapshot | None:
        return self.candidate_working

    @property
    def semantic_change(self) -> bool:
        return self.changed

    @property
    def conflict_codes(self) -> tuple[str, ...]:
        return tuple(conflict.code for conflict in self.conflicts)

    @property
    def row_matches(self) -> tuple[RowMatch, ...]:
        return self.matches

    @property
    def conflicts_by_code(self) -> dict[str, tuple[MergeConflict, ...]]:
        grouped: dict[str, list[MergeConflict]] = {}
        for conflict in self.conflicts:
            grouped.setdefault(conflict.code, []).append(conflict)
        return {code: tuple(items) for code, items in grouped.items()}


@dataclass(frozen=True)
class StubLifecycleResult:
    """Pure module-stub lifecycle result used by the Merge B service.

    Stub signatures and ownership are source-derived, while ``body_raw`` is
    mutable WORKING data.  Keeping this result separate from the row merge
    makes it possible to inspect a candidate without mutating the accepted
    module-level stub state.
    """

    states: tuple[StubState, ...]
    conflicts: tuple[MergeConflict, ...] = ()
    changed: bool = False
    orphaned_stub_keys: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return any(item.severity == "blocking" for item in self.conflicts)


def _stub_identity(state: StubState) -> tuple[str, ...]:
    """Return exact aliases for one module-scoped stub state."""

    return tuple(sorted({state.stub_key, state.function_name} - {""}))


def _stub_signature(state: StubState | None) -> tuple[object, ...] | None:
    if state is None:
        return None
    return (
        state.function_name,
        state.return_type,
        state.return_storage_name,
        tuple((item.type_name, item.name) for item in state.parameters),
    )


def _stub_modified(base: StubState | None, working: StubState | None) -> bool:
    """Whether a WORKING body is user-owned relative to the BASE state."""

    if working is None:
        return False
    if working.origin == "manual":
        return True
    if base is None:
        return working.origin != "generated"
    return working.body_raw != base.body_raw


def _body_validates_against_signature(
    body: str,
    old: StubState | None,
    new: StubState,
    validator: Callable[..., bool] | None,
) -> bool:
    """Validate a preserved body against a changed source signature.

    A caller-supplied validator is authoritative.  The fallback deliberately
    remains conservative and syntactic: references to parameters/return
    storage that disappeared from the NEW signature invalidate the body.  It
    does not attempt to parse C and therefore never invents a signature.
    """

    if validator is not None:
        try:
            result = validator(body, new)
        except TypeError:
            result = validator(new, body)
        return bool(result)
    # A body cannot be proven compatible with a changed C signature by looking
    # only at identifier spelling.  Type/qualifier/return changes can invalidate
    # an otherwise identical body, so the no-validator path is deliberately
    # fail-closed as required by the Merge B policy.
    del body, old, new
    return False


def _stub_conflict(
    code: str,
    function_key: str,
    *,
    stub_key: str,
    message: str,
    base: object = None,
    user: object = None,
    new: object = None,
) -> MergeConflict:
    return MergeConflict(
        code,
        "blocking",
        "stub",
        function_key or _DEFAULT_FUNCTION,
        None,
        None,
        None,
        stub_key,
        _safe_value(base),
        _safe_value(user),
        _safe_value(new),
        message,
        (),
    )


def merge_stub_states(
    base: Iterable[StubState] | None,
    working: Iterable[StubState] | None,
    new: Iterable[StubState] | None,
    *,
    function_key: str = _DEFAULT_FUNCTION,
    required_stubs: Iterable[str] | Mapping[str, object] | None = None,
    deleted_stub_keys: Iterable[str] | None = None,
    removed_function_keys: Iterable[str] | None = None,
    body_validator: Callable[..., bool] | None = None,
    scope_complete: bool = True,
) -> StubLifecycleResult:
    """Merge source-required module stubs with independent body ownership.

    ``base`` is the pure source signature/body baseline, ``working`` is the
    accepted module-level state, and ``new`` is the source-derived signature
    set for the next generation.  All three inputs are copied by value; the
    returned state never aliases an accepted ``StubState`` instance.

    The helper is intentionally exact.  Stub names are matched only by their
    persisted key/function-name aliases and duplicate aliases are blocking.
    """

    base_states = tuple(base or ())
    working_states = tuple(working or ())
    new_states = tuple(new or ())
    conflicts: list[MergeConflict] = []
    orphaned: list[str] = []
    deleted = {str(item) for item in (deleted_stub_keys or ()) if str(item)}
    removed_owners = {str(item) for item in (removed_function_keys or ()) if str(item)}

    required_names = {
        str(name)
        for name in (required_stubs.keys() if isinstance(required_stubs, Mapping) else (required_stubs or ()))
        if str(name)
    }

    # Build identity groups across generations.  Matching only by canonical
    # ``stub_key`` would duplicate a record when a source adapter changes the
    # key spelling but retains the called-function alias.  Conversely, two
    # records sharing either alias are ambiguous and must fail closed.
    groups: list[dict[str, Any]] = []
    alias_groups: dict[str, int] = {}

    def add_states(states: tuple[StubState, ...], label: str) -> None:
        for state in states:
            if not isinstance(state, StubState):
                conflicts.append(_stub_conflict(
                    "INVALID_STUB_STATE", function_key, stub_key="",
                    message=f"{label} contains a non-StubState record",
                ))
                continue
            aliases_for_state = _stub_identity(state)
            matching = sorted({alias_groups[item] for item in aliases_for_state if item in alias_groups})
            if len(matching) > 1:
                conflicts.append(_stub_conflict(
                    "DUPLICATE_STUB_NAME", function_key, stub_key=state.stub_key,
                    message=f"stub aliases identify multiple records in {label}",
                    new=state,
                ))
                continue
            if matching:
                group = groups[matching[0]]
                if label in group:
                    conflicts.append(_stub_conflict(
                        "DUPLICATE_STUB_NAME", function_key, stub_key=state.stub_key,
                        message=f"{label} contains duplicate stub aliases",
                        base=group[label], new=state,
                    ))
                    continue
            else:
                group = {"aliases": set()}
                groups.append(group)
                matching = [len(groups) - 1]
            group[label] = state
            group["aliases"].update(aliases_for_state)
            for alias in aliases_for_state:
                previous = alias_groups.get(alias)
                if previous is not None and previous != matching[0]:
                    conflicts.append(_stub_conflict(
                        "DUPLICATE_STUB_NAME", function_key, stub_key=alias,
                        message=f"stub alias {alias!r} identifies multiple records",
                        base=groups[previous].get(label), new=state,
                    ))
                else:
                    alias_groups[alias] = matching[0]

    add_states(base_states, "BASE")
    add_states(working_states, "WORKING")
    add_states(new_states, "NEW")

    # ``required_stubs`` is source authority, not merely a filter over the
    # states that happened to be decoded.  A brand-new required name which is
    # absent from NEW must therefore block instead of looking like an empty
    # (and consequently successful) generation.  Existing required records
    # which disappeared are diagnosed by the per-group deletion branch below;
    # avoid emitting a second copy of that diagnostic here.
    new_aliases = {
        alias
        for state in new_states
        if isinstance(state, StubState)
        for alias in _stub_identity(state)
    }
    base_working_aliases = {
        alias
        for state in (*base_states, *working_states)
        if isinstance(state, StubState)
        for alias in _stub_identity(state)
    }
    for required in sorted(required_names):
        if required in new_aliases:
            continue
        existing_group = next(
            (group for group in groups if required in group.get("aliases", set())),
            None,
        )
        if existing_group is not None and existing_group.get("NEW") is None:
            # The normal source-removal branch reports REQUIRED_STUB_DELETED.
            continue
        conflicts.append(_stub_conflict(
            "REQUIRED_STUB_DELETED" if required in base_working_aliases else "MISSING_STUB_SIGNATURE",
            function_key,
            stub_key=required,
            message=(
                f"source-required stub {required!r} is missing from NEW"
                if required in base_working_aliases
                else f"required NEW stub {required!r} has no complete signature"
            ),
            new=None,
        ))

    # A decoded state can carry an explicit unresolved/conflict metadata mark.
    # It must not satisfy a source-required entry merely because its name is
    # present; accepting it would make later body/signature checks guess.
    for state in new_states:
        if not isinstance(state, StubState) or state.metadata_status == "complete":
            continue
        conflicts.append(_stub_conflict(
            "MISSING_STUB_SIGNATURE",
            function_key,
            stub_key=state.stub_key,
            message="NEW stub signature metadata is unresolved",
            new=state,
        ))

    result: list[StubState] = []
    for group in groups:
        before = group.get("BASE")
        user = group.get("WORKING")
        after = group.get("NEW")
        representative = after or user or before
        if representative is None:
            continue
        canonical = representative.stub_key
        required = canonical in required_names or any(alias in required_names for alias in group["aliases"])

        if after is None:
            if required:
                conflicts.append(_stub_conflict(
                    "REQUIRED_STUB_DELETED", function_key, stub_key=canonical,
                    message=f"source-required stub {canonical!r} is missing from NEW",
                    base=before, user=user, new=None,
                ))
                if user is not None:
                    result.append(deepcopy(user))
                continue
            if user is None:
                # A source-removed generated record has no active state.
                continue
            modified = _stub_modified(before, user)
            if modified:
                orphan = replace(deepcopy(user), status="orphaned", owners=frozenset())
                result.append(orphan)
                orphaned.append(orphan.stub_key)
            # An untouched generated body is intentionally removed from active
            # module state and remains recoverable through repository history.
            continue

        if user is None:
            # An explicit user deletion is represented by ``deleted_stub_keys``
            # because absence alone is indistinguishable from first addition.
            if canonical in deleted or any(alias in deleted for alias in _stub_identity(after)):
                conflicts.append(_stub_conflict(
                    "REQUIRED_STUB_DELETED", function_key, stub_key=canonical,
                    message=f"required NEW stub {canonical!r} was deleted in WORKING",
                    base=before, user=None, new=after,
                ))
                result.append(deepcopy(after))
                continue
            result.append(deepcopy(after))
            continue

        merged = deepcopy(after)
        body_changed = _stub_modified(before, user)
        source_body_changed = before is not None and before.body_raw != after.body_raw
        if body_changed and source_body_changed and user.body_raw != after.body_raw:
            conflicts.append(_stub_conflict(
                "STUB_BODY_CONFLICT", function_key, stub_key=canonical,
                message="Stub Body changed differently by USER and NEW",
                base=None if before is None else before.body_raw,
                user=user.body_raw,
                new=after.body_raw,
            ))
            merged.body_raw = user.body_raw
        elif body_changed:
            merged.body_raw = user.body_raw
        elif source_body_changed:
            merged.body_raw = after.body_raw
        else:
            merged.body_raw = user.body_raw

        signature_changed = _stub_signature(before) != _stub_signature(after)
        if signature_changed and body_changed:
            if not _body_validates_against_signature(merged.body_raw, before, after, body_validator):
                conflicts.append(_stub_conflict(
                    "STUB_BODY_SIGNATURE_CONFLICT", function_key, stub_key=canonical,
                    message="modified Stub Body does not validate against NEW signature",
                    base=_stub_signature(before),
                    user=merged.body_raw,
                    new=_stub_signature(after),
                ))

        owners = set(after.owners)
        # Source ownership is authoritative when NEW is a complete module
        # analysis.  For a deliberately function-scoped/incomplete update we
        # may preserve unrelated accepted owners, but never infer or add the
        # current function when NEW explicitly omitted it.
        if not scope_complete:
            owners.update(
                owner
                for owner in user.owners
                if owner not in removed_owners and owner != function_key
            )
        merged.owners = frozenset(owners)
        merged.origin = "manual" if body_changed else "generated"
        merged.status = "active"
        result.append(merged)

    # Stable canonical ordering is required for deterministic state bytes.
    result.sort(key=lambda item: (item.stub_key, item.function_name))
    changed = tuple(result) != working_states or bool(conflicts)
    return StubLifecycleResult(tuple(result), tuple(conflicts), changed, tuple(sorted(set(orphaned))))


# ``GenerationSnapshot`` is the conceptual name used by the specification.
# The current PR02 model calls the concrete pure snapshot ``BaseSnapshot``.
GenerationSnapshot = BaseSnapshot


@dataclass(frozen=True)
class _Edge:
    base_index: int
    new_index: int
    score: int
    method: str


def _first_attr(value: object, names: Sequence[str], default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        # Do not fall through to Mapping methods such as ``dict.values`` when
        # a schema field is absent.  Those methods are not type metadata and
        # would otherwise be mistaken for an enum/domain value.
        return default
    for name in names:
        try:
            result = getattr(value, name)
        except AttributeError:
            continue
        if result is not None:
            return result
    return default


def _row_meta(row: TestRow) -> RowMeta | None:
    meta = getattr(row, "meta", None)
    return meta if isinstance(meta, RowMeta) else None


def _row_id(row: TestRow) -> str | None:
    meta = _row_meta(row)
    value = None if meta is None else meta.row_id
    return value if isinstance(value, str) and value else None


def _case_name(row: TestRow) -> str | None:
    meta = _row_meta(row)
    value = None if meta is None else meta.case_name
    return value if isinstance(value, str) and value else None


def _function_key(row: TestRow) -> str | None:
    meta = _row_meta(row)
    value = None if meta is None else meta.function_key
    return value if isinstance(value, str) and value else None


def _obligations(row: TestRow) -> frozenset[str]:
    meta = _row_meta(row)
    if meta is not None:
        # RowMeta is the single persisted provenance authority.  The runtime
        # field is a generator staging hook only and must never silently union
        # stale data into a persisted row's identity.
        return frozenset(str(item) for item in meta.obligation_keys if str(item))
    runtime = getattr(row, "obligation_keys", ())
    return frozenset(str(item) for item in runtime if str(item))


def _obligation_consistency_error(row: TestRow) -> tuple[frozenset[str], frozenset[str]] | None:
    """Return a persisted/runtime provenance mismatch, if one is present."""

    meta = _row_meta(row)
    if meta is None:
        return None
    persistent = frozenset(str(item) for item in meta.obligation_keys if str(item))
    runtime = frozenset(str(item) for item in getattr(row, "obligation_keys", ()) if str(item))
    if runtime and runtime != persistent:
        return persistent, runtime
    return None


def _normalize_intent(intent: object) -> tuple[tuple[str, object], ...]:
    """Return deterministic intent without treating it as fuzzy similarity."""

    if not isinstance(intent, Mapping):
        return ()
    normalized: list[tuple[str, object]] = []
    for raw_key, raw_value in intent.items():
        if isinstance(raw_key, bool):
            key = str(raw_key).lower()
        elif isinstance(raw_key, int):
            key = str(raw_key)
        else:
            key = str(raw_key)
        normalized.append((key, _freeze(raw_value)))
    return tuple(sorted(normalized, key=lambda item: (item[0], repr(item[1]))))


def _cell_parts(value: object) -> tuple[str | None, object, object]:
    if isinstance(value, CellValue):
        semantic = value.semantic_value if value.semantic_value is not None else _UNKNOWN
        return value.kind, value.raw_text, semantic
    return None, value, _UNKNOWN


def _integer_value(value: object) -> int | None:
    """Decode an integer without making non-numeric text comparable.

    The merge engine deliberately accepts only the small lexical grammar used
    by the XML/workbook adapters.  In particular, arbitrary strings are never
    normalized merely because they happen to contain digits.
    """

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _INTEGER_RE.fullmatch(value.strip()):
        text = value.strip()
        try:
            # Python's base=0 rejects valid decimal spellings such as ``08``.
            # The workbook/XML grammar is decimal by default and permits an
            # explicit 0x prefix for hexadecimal values.
            signless = text[1:] if text[:1] in {"+", "-"} else text
            radix = 16 if signless.lower().startswith("0x") else 10
            return int(text, radix)
        except ValueError:
            return None
    return None


def _enum_value(value: object, metadata: object | None) -> object:
    """Return an enum's proven canonical member, or ``_UNKNOWN``.

    Analysis/adapter metadata has appeared as a mapping, a sequence of
    ``(symbol, integer)`` pairs, and a sequence of symbols in older callers.
    Only explicit metadata is considered; this is semantic equality evidence,
    not a similarity heuristic.
    """

    domain = _first_attr(metadata, ("domain", "enum_values", "values"), None)
    if domain is None:
        return _UNKNOWN

    def canonical_member(left: object, right: object) -> object:
        # Both ``{symbol: number}`` and ``{number: symbol}`` occur in adapter
        # metadata.  Prefer the explicitly numeric side so symbol/numeric
        # aliases share one exact comparison/range-validation key.
        for candidate in (left, right):
            numeric = _integer_value(candidate)
            if numeric is not None:
                return numeric
        return ("member", _freeze(left), _freeze(right))

    if isinstance(domain, Mapping):
        for key, mapped in domain.items():
            try:
                is_match = value == key or value == mapped
            except Exception:
                is_match = False
            if is_match:
                return canonical_member(key, mapped)
        return _UNKNOWN
    try:
        items = tuple(domain)
    except TypeError:
        return _UNKNOWN
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            if value == item[0] or value == item[1]:
                return canonical_member(item[0], item[1])
        elif value == item:
            # A symbol-only domain proves membership, but not a numeric alias.
            return ("member", item)
    return _UNKNOWN


def _normalized_scalar(value: object, metadata: object | None) -> object:
    """Build an exact comparison key with narrowly proven normalization."""

    if _metadata_proves_numeric(metadata):
        # Prefer an explicit enum domain over the generic integer spelling so
        # ``READY`` and its proven numeric member ``1`` share one exact key.
        # This is intentionally limited to declared enum metadata.
        enum = _enum_value(value, metadata)
        if enum is not _UNKNOWN:
            return ("enum", _freeze(enum))
        integer = _integer_value(value)
        if integer is not None:
            return ("integer", integer)
    return ("scalar", type(value).__name__, _freeze(value))


def _normalized_value(value: object, metadata: object | None = None) -> object:
    """Normalize one value for exact matching/equality only.

    Raw text remains in the actual merged value.  This key is used to decide
    whether two edits are semantically identical, and therefore may collapse
    proven numeric/enum lexical aliases while retaining the USER object as the
    winner at the merge boundary.
    """

    if value is _DELETED:
        return ("deleted",)
    if isinstance(value, CellValue):
        if value.kind == "value":
            semantic = value.semantic_value if value.semantic_value is not None else value.raw_text
            return ("cell", "value", _normalized_scalar(semantic, metadata))
        return (
            "cell",
            value.kind,
            type(value.raw_text).__name__,
            _freeze(value.raw_text),
            _freeze(value.semantic_value),
        )
    if isinstance(value, Mapping):
        return tuple(sorted(
            (
                str(key),
                _normalized_value(item, _metadata_for_name(key, metadata) if isinstance(metadata, Mapping) else None),
            )
            for key, item in value.items()
        ))
    if isinstance(value, (tuple, list)):
        return tuple(_normalized_value(item, metadata) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_normalized_value(item, metadata) for item in value), key=repr))
    return _normalized_scalar(value, metadata)


def _freeze(value: object) -> object:
    """Canonical, lossless-ish value key used only for exact comparisons."""

    if isinstance(value, CellValue):
        return ("CellValue", value.kind, value.raw_text, _freeze(value.semantic_value))
    if isinstance(value, Mapping):
        return tuple(sorted(((str(key), _freeze(item)) for key, item in value.items()), key=lambda item: item[0]))
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    if isinstance(value, float):
        # repr is deterministic and does not silently normalize raw lexemes.
        return repr(value)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _mapping_equal(left: object, right: object) -> bool:
    return _freeze(left) == _freeze(right)


def _row_payload(row: TestRow | None) -> object:
    if row is None:
        return None
    meta = _row_meta(row)
    return {
        "row_id": None if meta is None else meta.row_id,
        "case_name": None if meta is None else meta.case_name,
        "kind": row.kind,
        "description": row.description,
        "values": _freeze(row.values),
        "expected": _freeze(row.expected),
        "intent": _freeze(row.intent),
        "obligations": tuple(sorted(_obligations(row))),
    }


def _value_equal(
    left: object,
    right: object,
    *,
    metadata: object | None = None,
) -> bool:
    """Compare cells conservatively, preserving raw lexical distinctions.

    A direct ``CellValue.semantic_value`` is typed evidence supplied by the
    adapter.  For ordinary raw strings, numeric normalization is enabled only
    when the argument metadata proves that the value is numeric.
    """

    if left is _DELETED or right is _DELETED:
        return left is right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        keys = set(left) | set(right)
        return all(
            _value_equal(
                left[key] if key in left else _DELETED,
                right[key] if key in right else _DELETED,
                metadata=(
                    _metadata_for_name(key, metadata)
                    if isinstance(metadata, Mapping)
                    else metadata
                ),
            )
            for key in keys
        )
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        return len(left) == len(right) and all(
            _value_equal(a, b, metadata=metadata) for a, b in zip(left, right)
        )
    if isinstance(left, CellValue) and isinstance(right, CellValue):
        if left.kind != right.kind:
            return False
        return _normalized_value(left, metadata) == _normalized_value(right, metadata)
    if isinstance(left, CellValue):
        if left.kind != "value":
            return False
        semantic = left.semantic_value if left.semantic_value is not None else left.raw_text
        return _normalized_scalar(semantic, metadata) == _normalized_scalar(right, metadata)
    if isinstance(right, CellValue):
        if right.kind != "value":
            return False
        semantic = right.semantic_value if right.semantic_value is not None else right.raw_text
        return _normalized_scalar(left, metadata) == _normalized_scalar(semantic, metadata)
    return _normalized_scalar(left, metadata) == _normalized_scalar(right, metadata)


def _numeric_equal(
    left_semantic: object,
    right_semantic: object,
    left_raw: object,
    right_raw: object,
) -> bool:
    def integer(value: object) -> int | None:
        return _integer_value(value)

    left = integer(left_semantic) if left_semantic is not _UNKNOWN else integer(left_raw)
    right = integer(right_semantic) if right_semantic is not _UNKNOWN else integer(right_raw)
    return left is not None and right is not None and left == right


def _metadata_proves_numeric(metadata: object | None) -> bool:
    if metadata is None:
        return False
    for name in ("bits", "signed", "min_value", "max_value", "minimum", "maximum"):
        if _first_attr(metadata, (name,), None) is not None:
            return True
    value = _first_attr(metadata, ("fmt", "format", "type_name", "ctype", "type"), None)
    if value is not None:
        text = str(value).strip().lower().replace(" ", "")
        if re.search(r"(?:u?int|integer|short|long|char|byte|word|bool|_bool)", text) or re.fullmatch(r"u?\d+", text):
            return True

    # An explicit enum pair is also a semantic proof even when the adapter
    # omits a numeric fmt.  A symbol-only collection is membership metadata,
    # not evidence that arbitrary symbol/numeric spellings are aliases.
    domain = _first_attr(metadata, ("domain", "enum_values", "values"), None)
    if isinstance(domain, Mapping):
        return any(
            _integer_value(key) is not None or _integer_value(mapped) is not None
            for key, mapped in domain.items()
        )
    try:
        items = tuple(domain)
    except TypeError:
        return False
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            if _integer_value(item[0]) is not None or _integer_value(item[1]) is not None:
                return True
    return bool(items) and all(_integer_value(item) is not None for item in items)


def _metadata_map(arguments: object | None) -> dict[str, object]:
    if arguments is None:
        return {}
    if isinstance(arguments, Mapping):
        values = list(arguments.items())
    else:
        try:
            values = [(None, item) for item in arguments]  # type: ignore[arg-type]
        except TypeError:
            values = []
    result: dict[str, object] = {}
    for key, item in values:
        names = []
        if key is not None:
            names.append(str(key))
        for attr in ("name", "canonical_name", "canonical_xml_name"):
            value = _first_attr(item, (attr,), None)
            if value:
                names.append(str(value))
        item_key = _first_attr(item, ("key",), None)
        if isinstance(item_key, LogicalArgumentKey):
            names.append(item_key.canonical_xml_name)
        elif item_key is not None:
            canonical = _first_attr(item_key, ("canonical_xml_name", "name"), None)
            if canonical:
                names.append(str(canonical))
        for name in names:
            result.setdefault(name, item)
    return result


def _metadata_record(argument: object) -> dict[str, object]:
    """Expose schema/type evidence as a mergeable, read-only-style record."""

    if isinstance(argument, Mapping):
        return {str(key): value for key, value in argument.items()}
    try:
        values = dict(vars(argument))
    except TypeError:
        values = {}
    if values:
        return values
    # Accommodate lightweight adapter objects which expose properties rather
    # than a __dict__, while keeping the list limited to schema evidence.
    fields = (
        "name", "canonical_name", "canonical_xml_name", "key", "direction",
        "fmt", "format", "xml_len", "len", "pointer", "dimensions",
        "testmethod", "range_encoding", "output_encoding", "source",
        "metadata_status", "original_order", "bits", "signed", "domain",
        "enum_values", "values", "min_value", "max_value", "minimum",
        "maximum", "type_name", "ctype", "type",
    )
    return {
        name: value
        for name in fields
        if (value := _first_attr(argument, (name,), _UNKNOWN)) is not _UNKNOWN
    }


def _merge_metadata_maps(*maps: Mapping[str, object]) -> dict[str, object]:
    """Supplement type evidence without allowing it to replace NEW schema."""

    result: dict[str, object] = {}
    for key in sorted({name for mapping in maps for name in mapping}):
        merged: dict[str, object] = {}
        for mapping in maps:
            item = mapping.get(key)
            if item is not None:
                merged.update(_metadata_record(item))
        result[key] = merged
    return result


def _metadata_for_name(name: object, metadata: Mapping[str, object]) -> object | None:
    text = str(name)
    if text in metadata:
        return metadata[text]
    # Array leaves may carry an index path.  The logical schema name is the
    # same name with all bracket components removed.
    base = re.sub(r"\[[0-9]+\]", "", text)
    if base in metadata:
        return metadata[base]
    return None


def _row_schema_signature(row: TestRow, metadata: Mapping[str, object]) -> tuple[object, ...]:
    keys = set()
    for mapping in (row.values, row.expected):
        if isinstance(mapping, Mapping):
            keys.update(str(key) for key in mapping)
    schema = []
    for key in sorted(keys):
        item = _metadata_for_name(key, metadata)
        schema.append((key, _schema_fields(item)))
    return tuple(schema)


def _schema_fields(argument: object | None) -> tuple[object, ...]:
    if argument is None:
        return ()
    fields = []
    for name in (
        "name", "canonical_name", "canonical_xml_name", "direction", "fmt",
        "format", "xml_len", "len", "pointer", "dimensions", "testmethod",
        "range_encoding", "output_encoding",
    ):
        value = _first_attr(argument, (name,), _UNKNOWN)
        if value is not _UNKNOWN:
            fields.append((name, _freeze(value)))
    return tuple(fields)


def _generated_signature(row: TestRow, metadata: Mapping[str, object]) -> tuple[object, ...]:
    """Exact signature for a legacy row without usable obligations."""

    return (
        str(row.kind),
        _normalize_intent(row.intent),
        _normalized_mapping_signature(row.values, metadata),
        _normalized_mapping_signature(row.expected, metadata),
        _row_schema_signature(row, metadata),
    )


def _normalized_mapping_signature(mapping: object, metadata: Mapping[str, object]) -> object:
    """Canonicalize generated cell maps for exact identity matching.

    Unlike ``_freeze``, this keeps typed semantic values comparable across raw
    lexeme changes when the argument metadata proves that they are aliases.
    Mapping keys and their insertion order never participate in the result.
    """

    if not isinstance(mapping, Mapping):
        return _normalized_value(mapping, metadata)
    return tuple(sorted(
        (
            str(key),
            _normalized_value(item, _metadata_for_name(key, metadata)),
        )
        for key, item in mapping.items()
    ))


def _structural_signature(row: TestRow, metadata: Mapping[str, object]) -> tuple[object, ...]:
    keys = set()
    for mapping in (row.values, row.expected):
        if isinstance(mapping, Mapping):
            keys.update(str(key) for key in mapping)
    return (
        str(row.kind),
        _normalize_intent(row.intent),
        tuple(sorted(keys)),
        _row_schema_signature(row, metadata),
    )


def _function_compatible(base: TestRow, new: TestRow, expected: str | None) -> bool:
    base_function = _function_key(base)
    new_function = _function_key(new)
    if base_function and new_function and base_function != new_function:
        return False
    if expected and ((base_function and base_function != expected) or (new_function and new_function != expected)):
        return False
    return True


def _semantic_row_equal(
    base: TestRow,
    other: TestRow,
    metadata: Mapping[str, object],
) -> bool:
    if str(base.kind) != str(other.kind):
        return False
    if _normalize_intent(base.intent) != _normalize_intent(other.intent):
        return False
    if str(base.description) != str(other.description):
        return False
    return _mapping_semantic_equal(base.values, other.values, metadata) and _mapping_semantic_equal(
        base.expected, other.expected, metadata
    )


def _raw_row_equal(base: TestRow, other: TestRow) -> bool:
    """Compare accepted row representation without semantic normalization."""

    return (
        str(base.kind) == str(other.kind)
        and _freeze(base.description) == _freeze(other.description)
        and _freeze(base.intent) == _freeze(other.intent)
        and _freeze(base.values) == _freeze(other.values)
        and _freeze(base.expected) == _freeze(other.expected)
    )


def _mapping_semantic_equal(left: object, right: object, metadata: Mapping[str, object]) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return _value_equal(left, right)
    keys = set(left) | set(right)
    for key in keys:
        left_value = left[key] if key in left else _DELETED
        right_value = right[key] if key in right else _DELETED
        if not _value_equal(left_value, right_value, metadata=_metadata_for_name(key, metadata)):
            return False
    return True


def _user_changed(base: TestRow | None, working: TestRow | None, metadata: Mapping[str, object]) -> bool:
    if base is None or working is None:
        return working is not None
    if _row_meta(working) is not None and _row_meta(working).origin == "manual":
        return True
    # A lexical-only edit is still user-owned state.  It may be semantically
    # equal for merge purposes (for example ``1`` versus ``0x1`` with proven
    # numeric metadata), but a source removal must preserve that accepted raw
    # representation as an orphan rather than silently discarding it.
    return not _semantic_row_equal(base, working, metadata) or not _raw_row_equal(base, working)


def _is_deleted(value: object) -> bool:
    return value is _DELETED


def _cell_for_mapping(mapping: object, key: object) -> object:
    if not isinstance(mapping, Mapping) or key not in mapping:
        return _DELETED
    return mapping[key]


def _merge_field(
    base: object,
    user: object,
    new: object,
    *,
    metadata: object | None,
) -> tuple[object, bool]:
    """Apply one BASE/WORKING/NEW field rule.

    The bool reports a USER-vs-NEW conflict.  Deletion is represented by the
    private sentinel, so a physical missing mapping key remains distinct from
    a present ``CellValue(kind='missing')``.
    """

    user_eq_base = _value_equal(user, base, metadata=metadata)
    new_eq_base = _value_equal(new, base, metadata=metadata)
    if user_eq_base and new_eq_base:
        # The accepted WORKING representation is authoritative for raw
        # presentation.  This matters even when USER is semantically
        # unchanged relative to BASE: an engineer may have entered ``0x1``
        # where the generator used ``1``.  Returning USER here preserves that
        # lexeme while retaining the same semantic value.  ``_value_equal``
        # only collapses lexical aliases when typed metadata proves them.
        return user, False
    if not user_eq_base and new_eq_base:
        return user, False
    if user_eq_base and not new_eq_base:
        return new, False
    if _value_equal(user, new, metadata=metadata):
        # USER wins lexical presentation ties (for example 0x01 versus 1).
        return user, False
    # A deletion is safe only if the other side is unchanged.  Reaching this
    # branch means one side changed/deleted while the other changed differently.
    return _DELETED, True


def _merge_mapping(
    base: object,
    user: object,
    new: object,
    *,
    metadata: Mapping[str, object],
) -> tuple[dict[Any, Any], tuple[Any, ...]]:
    base_map = base if isinstance(base, Mapping) else {}
    user_map = user if isinstance(user, Mapping) else {}
    new_map = new if isinstance(new, Mapping) else {}
    result: dict[Any, Any] = {}
    conflicts: list[Any] = []
    keys = sorted(set(base_map) | set(user_map) | set(new_map), key=lambda value: str(value))
    for key in keys:
        merged, conflict = _merge_field(
            _cell_for_mapping(base_map, key),
            _cell_for_mapping(user_map, key),
            _cell_for_mapping(new_map, key),
            metadata=_metadata_for_name(key, metadata),
        )
        if conflict:
            conflicts.append(key)
        elif merged is not _DELETED:
            result[key] = merged
    return result, tuple(conflicts)


def _obligation_intent_compatible(base: TestRow, new: TestRow) -> bool:
    overlap = _obligations(base) & _obligations(new)
    return bool(overlap) and _normalize_intent(base.intent) == _normalize_intent(new.intent)


def _edge_for_rows(
    base: TestRow,
    new: TestRow,
    base_index: int,
    new_index: int,
    metadata: Mapping[str, object],
    function_key: str | None,
) -> _Edge | None:
    if not _function_compatible(base, new, function_key):
        return None
    base_id, new_id = _row_id(base), _row_id(new)
    if base_id and new_id and base_id == new_id:
        return _Edge(base_index, new_index, 500, "stable_row_id")

    base_obligations = _obligations(base)
    new_obligations = _obligations(new)
    if base_obligations and base_obligations == new_obligations:
        return _Edge(base_index, new_index, 400, "exact_obligation_set")
    if _obligation_intent_compatible(base, new):
        # This is a distinct precedence tier, never a cardinality score.
        # Keeping it below exact-obligation identity is important: a large
        # overlap must not outrank a smaller exact set.
        return _Edge(base_index, new_index, 300, "compatible_obligation_intent")

    # Once both rows carry non-identical obligation sets, do not use values to
    # pair them.  This is what makes a threshold/expression change a removed
    # obligation plus an added obligation under the exact-only policy.
    if base_obligations and new_obligations and base_obligations != new_obligations:
        return None
    if _generated_signature(base, metadata) == _generated_signature(new, metadata):
        return _Edge(base_index, new_index, 200, "exact_generated_signature")
    if _structural_signature(base, metadata) == _structural_signature(new, metadata):
        return _Edge(base_index, new_index, 100, "exact_structural_alignment")
    return None


def _source_prefix(source_file: str | None, function_key: str | None, rows: Iterable[TestRow]) -> str:
    if source_file:
        leaf = source_file.replace("\\", "/").rsplit("/", 1)[-1]
        stem = leaf.rsplit(".", 1)[0]
        if stem:
            return stem
    for row in rows:
        name = _case_name(row)
        if name:
            match = _CASE_NUMBER_RE.search(name)
            if match:
                return name[: match.start()]
    return function_key or "Source"


def _case_number(name: str | None) -> int | None:
    if not name:
        return None
    match = _CASE_NUMBER_RE.search(name)
    return None if match is None else int(match.group(1))


def _row_id_from_factory(factory: Callable[[], uuid.UUID], used: set[str]) -> str:
    for _ in range(100):
        value = factory()
        if isinstance(value, uuid.UUID):
            candidate = f"r_{value.hex}"
        else:
            candidate = str(value)
            if not candidate.startswith("r_"):
                # Do not hash values into identity.  A supplied deterministic
                # factory must return either UUID or a persistent r_ ID.
                raise MergeBError("uuid_factory must return uuid.UUID or a persistent r_ row ID")
        if candidate in used:
            continue
        if not _ROW_ID_RE.fullmatch(candidate):
            raise MergeBError("allocated row ID must match r_ followed by 32 hexadecimal digits")
        used.add(candidate)
        return candidate
    raise MergeBError("uuid_factory repeatedly returned colliding row IDs")


def _new_meta(
    row: TestRow,
    *,
    row_id: str,
    case_name: str,
    function_key: str,
    generation: int,
    origin: str = "generated",
    status: str = "active",
    created: int | None = None,
) -> RowMeta:
    source_meta = _row_meta(row)
    return RowMeta(
        row_id=row_id,
        case_name=case_name,
        external_test_case_id=None if source_meta is None else source_meta.external_test_case_id,
        origin=origin,
        status=status,
        generation_created=generation if created is None else created,
        generation_modified=generation,
        function_key=function_key,
        obligation_keys=tuple(sorted(_obligations(row))),
    )


def _with_meta(row: TestRow, meta: RowMeta) -> TestRow:
    return replace(row, meta=meta, obligation_keys=tuple(meta.obligation_keys))


def _safe_value(value: object) -> object:
    if value is _DELETED:
        return "<deleted>"
    if isinstance(value, CellValue):
        return {"kind": value.kind, "raw_text": value.raw_text, "semantic_value": _safe_value(value.semantic_value)}
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_safe_value(item) for item in value]
    try:
        # MergeConflict serialization accepts JSON-compatible primitives only.
        import json

        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _conflict(
    code: str,
    function_key: str,
    *,
    entity_type: str,
    message: str,
    base: object = None,
    user: object = None,
    new: object = None,
    row_id: str | None = None,
    case_name: str | None = None,
    logical_argument_key: str | None = None,
) -> MergeConflict:
    allowed_resolutions = (
        ("keep_user", "keep_new", "cancel")
        if code in {"USER_NEW_VALUE_CONFLICT", "DESCRIPTION_CONFLICT"}
        and entity_type in {"value", "expected", "description", "intent", "row"}
        else ()
    )
    return MergeConflict(
        code,
        "blocking",
        entity_type,
        function_key or _DEFAULT_FUNCTION,
        row_id,
        case_name,
        logical_argument_key,
        None,
        _safe_value(base),
        _safe_value(user),
        _safe_value(new),
        message,
        allowed_resolutions,
    )


def _maximum_matching(
    edges: Iterable[_Edge],
    *,
    available_bases: set[int] | None = None,
    available_news: set[int] | None = None,
    forbidden: tuple[int, int] | None = None,
) -> tuple[_Edge, ...]:
    """Return one deterministic maximum-cardinality bipartite matching.

    The matcher is intentionally small and exact.  It is used one precedence
    tier at a time, so cardinality is only a way to resolve a globally
    constrained assignment within that exact tier; it is never a fuzzy score.
    A stable ordering of BASE indexes and NEW indexes makes the returned
    assignment reproducible across runs.
    """

    base_filter = available_bases
    new_filter = available_news
    by_base: dict[int, list[_Edge]] = {}
    for edge in edges:
        if base_filter is not None and edge.base_index not in base_filter:
            continue
        if new_filter is not None and edge.new_index not in new_filter:
            continue
        if forbidden == (edge.base_index, edge.new_index):
            continue
        by_base.setdefault(edge.base_index, []).append(edge)
    for candidates in by_base.values():
        candidates.sort(key=lambda edge: (edge.new_index, edge.base_index, edge.method, -edge.score))

    new_to_edge: dict[int, _Edge] = {}

    def augment(base_index: int, seen_news: set[int]) -> bool:
        for edge in by_base.get(base_index, ()):
            if edge.new_index in seen_news:
                continue
            seen_news.add(edge.new_index)
            previous = new_to_edge.get(edge.new_index)
            if previous is None or augment(previous.base_index, seen_news):
                new_to_edge[edge.new_index] = edge
                return True
        return False

    for base_index in sorted(by_base):
        augment(base_index, set())
    return tuple(sorted(new_to_edge.values(), key=lambda edge: (edge.base_index, edge.new_index)))


class MergeBEngine:
    """Pure exact BASE + WORKING + NEW merge engine.

    The engine is intentionally independent of PR13's function/stub lifecycle
    and repository transactions.  ``merge`` only returns a candidate model.
    """

    def __init__(
        self,
        *,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        function_key: str | None = None,
        source_file: str | None = None,
    ) -> None:
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        self.uuid_factory = uuid_factory
        self.function_key = function_key
        self.source_file = source_file

    def merge(
        self,
        base: BaseSnapshot | FunctionIterationState,
        working: WorkingSnapshot | None,
        new: BaseSnapshot,
        *,
        function_key: str | None = None,
        source_file: str | None = None,
        base_arguments: object | None = None,
        new_arguments: object | None = None,
        arguments: object | None = None,
        base_schema: object | None = None,
        new_schema: object | None = None,
        type_metadata: object | None = None,
        conflict_resolver: Callable[[MergeConflict], str | None] | None = None,
    ) -> MergeBResult:
        """Merge snapshots using deterministic exact-only semantics.

        ``base_arguments``/``new_arguments`` are optional because the current
        pure ``BaseSnapshot`` model stores rows only.  Callers with a richer
        generation snapshot may expose an ``arguments`` attribute, and the
        explicit arguments take precedence.  ``arguments`` is accepted as a
        convenient alias for a new schema.
        """

        accepted_state: FunctionIterationState | None = None
        if isinstance(base, FunctionIterationState):
            accepted_state = base
            if working is None:
                working = base.working
            base_snapshot = base.base
            if function_key is None:
                function_key = base.function_key
            if source_file is None:
                source_file = base.source_file
        else:
            base_snapshot = base
        if not isinstance(base_snapshot, BaseSnapshot):
            raise TypeError("base must be a BaseSnapshot or FunctionIterationState")
        if not isinstance(working, WorkingSnapshot):
            raise TypeError("working must be a WorkingSnapshot")
        if not isinstance(new, BaseSnapshot):
            raise TypeError("new must be a BaseSnapshot")

        actual_function = function_key or self.function_key or _infer_function_key(base_snapshot, working, new)
        actual_function = actual_function or _DEFAULT_FUNCTION
        source_file = source_file or self.source_file

        # Rich callers may attach a schema to a GenerationSnapshot-like object.
        if base_arguments is None:
            base_arguments = getattr(base_snapshot, "arguments", None) or getattr(working, "arguments", None)
        if new_arguments is None:
            new_arguments = getattr(new, "arguments", None)
        if new_arguments is None and arguments is not None:
            new_arguments = arguments
        if new_arguments is None and type_metadata is not None:
            # A caller may provide only a normalized type map.  Treat it as
            # the NEW schema for validation while still allowing the accepted
            # WORKING arguments to supply the BASE side below.
            new_arguments = type_metadata
        if base_schema is not None:
            base_arguments = base_schema
        if new_schema is not None:
            new_arguments = new_schema
        base_meta = _metadata_map(base_arguments)
        new_meta = _metadata_map(new_arguments)
        if not new_meta:
            new_meta = base_meta
        type_meta = _metadata_map(type_metadata)
        # Explicit type metadata supplements schema metadata.  The concrete
        # NEW schema remains authoritative when both describe one argument;
        # type evidence must not silently override its direction/shape.
        merged_meta = _merge_metadata_maps(type_meta, base_meta, new_meta)

        conflicts: list[MergeConflict] = []
        conflicts.extend(self._validate_snapshot_identity(base_snapshot, working, new, actual_function))
        conflicts.extend(self._validate_schema(working, base_arguments, new_arguments, merged_meta, actual_function))

        base_rows = tuple(base_snapshot.rows)
        new_rows = tuple(new.rows)
        working_rows = tuple(working.rows)
        conflicts.extend(self._duplicate_identity_conflicts(base_rows, new_rows, working_rows, actual_function))

        matches, matching_conflicts = self._match_rows(
            base_rows,
            new_rows,
            merged_meta,
            actual_function,
        )
        conflicts.extend(matching_conflicts)

        # Even in the presence of a matching conflict, produce deterministic
        # diagnostics for all unambiguous pairs.  The candidate remains blocked.
        base_to_new = {item.base_index: item.new_index for item in matches}
        new_to_base = {item.new_index: item.base_index for item in matches}
        working_by_id, working_by_case = _working_indexes(working_rows)
        used_row_ids = {row_id for row_id in (_row_id(row) for row in base_rows + working_rows + new_rows) if row_id}
        used_case_names = {name for name in (_case_name(row) for row in base_rows + working_rows) if name}
        used_case_names.update(
            tombstone.entity_key
            for tombstone in working.tombstones
            if tombstone.entity_type == "row" and _case_number(tombstone.entity_key) is not None
        )
        prefix = _source_prefix(source_file, actual_function, base_rows + working_rows + new_rows)
        persisted_counter = (
            accepted_state.next_case_number
            if accepted_state is not None
            else getattr(working, "next_case_number", 1)
        )
        next_case_number = max(
            [persisted_counter]
            + [number + 1 for number in (_case_number(name) for name in used_case_names) if number is not None]
        )
        generation = max(base_snapshot.generation, working.generation, new.generation)

        active_rows: list[TestRow] = []
        preserved_rows: list[TestRow] = []
        active_ids: list[str] = []
        added_ids: list[str] = []
        removed_ids: list[str] = []
        orphan_ids: list[str] = []
        superseded_ids: list[str] = []
        added_new_indices: list[int] = []
        tombstones = list(working.tombstones)
        tombstone_keys = {(item.entity_type, item.entity_key) for item in tombstones}

        def add_tombstone(row: TestRow) -> None:
            row_id = _row_id(row)
            key = row_id or _case_name(row)
            if not key or ("row", key) in tombstone_keys:
                return
            tombstones.append(Tombstone("row", key, generation))
            tombstone_keys.add(("row", key))

        def working_for_base(row: TestRow) -> TestRow | None:
            row_id = _row_id(row)
            if row_id and row_id in working_by_id:
                return working_by_id[row_id]
            name = _case_name(row)
            if name and name in working_by_case:
                return working_by_case[name]
            return None

        # Preserve existing WORKING order for all matched/surviving rows.  The
        # base iteration is used only for source classification; the user-facing
        # order remains a WORKING concern.
        matched_working_ids: set[str] = set()
        merged_by_base: dict[int, TestRow] = {}
        for match in matches:
            base_row = base_rows[match.base_index]
            new_row = new_rows[match.new_index]
            user_row = working_for_base(base_row)
            merged_row, row_conflicts = self._merge_matched_row(
                base_row,
                user_row,
                new_row,
                merged_meta,
                actual_function,
                resolution_resolver=conflict_resolver,
            )
            conflicts.extend(row_conflicts)
            if merged_row is not None:
                row_id = _row_id(merged_row)
                if row_id is None:
                    row_id = _row_id(user_row) or _row_id(base_row)
                if row_id is None:
                    row_id = _row_id_from_factory(self.uuid_factory, used_row_ids)
                case_name = _case_name(user_row) or _case_name(base_row) or _case_name(new_row)
                if not case_name:
                    allocation = allocate_case_name(
                        f"{prefix}.c",
                        next_case_number,
                        used_case_names,
                        on_collision="skip",
                    )
                    case_name = allocation.case_name
                    next_case_number = allocation.next_case_number
                    used_case_names.add(case_name)
                origin = "generated"
                if user_row is not None and _user_changed(base_row, user_row, merged_meta):
                    origin = "manual" if _row_meta(user_row) and _row_meta(user_row).origin == "manual" else "merged"
                meta = RowMeta(
                    row_id=row_id,
                    case_name=case_name,
                    external_test_case_id=(
                        _row_meta(user_row).external_test_case_id
                        if _row_meta(user_row) is not None
                        else (_row_meta(base_row).external_test_case_id if _row_meta(base_row) is not None else None)
                    ),
                    origin=origin,
                    status="active",
                    generation_created=(
                        _row_meta(base_row).generation_created
                        if _row_meta(base_row) is not None
                        else generation
                    ),
                    generation_modified=generation,
                    function_key=actual_function,
                    obligation_keys=tuple(sorted(_obligations(new_row))),
                )
                merged_row = _with_meta(merged_row, meta)
                merged_by_base[match.base_index] = merged_row
                if row_id:
                    matched_working_ids.add(row_id)
            elif not row_conflicts:
                # A matched row can still be explicitly deleted in WORKING.
                # It is important to record that tombstone here because the
                # BASE row *did* have a NEW successor and therefore is not
                # visited by the unmatched-BASE removal pass below.
                add_tombstone(base_row)
                removed_id = _row_id(base_row)
                if removed_id:
                    removed_ids.append(removed_id)

        # BASE rows which did not survive exact matching are removed when their
        # WORKING counterpart was untouched, and retained as orphans when it was
        # manually modified.  A user-deleted counterpart remains deleted unless
        # NEW modified the same row, which is a blocking conflict.
        for base_index, base_row in enumerate(base_rows):
            if base_index in base_to_new:
                continue
            user_row = working_for_base(base_row)
            if user_row is None:
                add_tombstone(base_row)
                removed_id = _row_id(base_row)
                if removed_id:
                    removed_ids.append(removed_id)
                continue
            changed_by_user = _user_changed(base_row, user_row, merged_meta)
            if changed_by_user:
                orphan = self._as_orphan(user_row, generation, actual_function)
                preserved_rows.append(orphan)
                orphan_id = _row_id(orphan)
                if orphan_id:
                    orphan_ids.append(orphan_id)
            elif self._has_consumed_new_successor(
                base_index,
                base_rows,
                new_rows,
                new_to_base,
                merged_meta,
                actual_function,
            ):
                # This is the deterministic many-BASE-to-one-NEW case.  The
                # losing untouched generated row remains in state as
                # ``superseded`` rather than being mistaken for a removed
                # obligation.  It is excluded from active XML via row_order.
                superseded = self._as_superseded(user_row, generation, actual_function)
                preserved_rows.append(superseded)
                superseded_id = _row_id(superseded)
                if superseded_id:
                    superseded_ids.append(superseded_id)
            else:
                add_tombstone(base_row)
                removed_id = _row_id(base_row)
                if removed_id:
                    removed_ids.append(removed_id)

        # Existing WORKING-only/manual rows are durable user content and are
        # retained independently of BASE-to-NEW matching.
        for row in working_rows:
            row_id = _row_id(row)
            if row_id and row_id in matched_working_ids:
                continue
            if row_id and row_id in {item for item in (_row_id(base_row) for base_row in base_rows) if item}:
                # The BASE row was unmatched and has already been handled above.
                continue
            if _row_meta(row) is not None and _row_meta(row).status in {"orphaned", "superseded", "conflict"}:
                preserved_rows.append(row)
            elif _row_meta(row) is not None and _row_meta(row).status == "active":
                active_rows.append(row)
                if row_id:
                    active_ids.append(row_id)
            elif row_id is None:
                row_id = _row_id_from_factory(self.uuid_factory, used_row_ids)
                case_name = _case_name(row)
                if not case_name:
                    allocation = allocate_case_name(
                        f"{prefix}.c",
                        next_case_number,
                        used_case_names,
                        on_collision="skip",
                    )
                    case_name = allocation.case_name
                    next_case_number = allocation.next_case_number
                    used_case_names.add(case_name)
                meta = _new_meta(
                    row,
                    row_id=row_id,
                    case_name=case_name,
                    function_key=actual_function,
                    generation=generation,
                    origin="manual",
                )
                row = _with_meta(row, meta)
                active_rows.append(row)
                active_ids.append(row_id)

        # Place matched rows according to existing WORKING order.  A working
        # row may not have appeared in row_order in old bootstrap state, so the
        # fallback tuple order is deterministic too.
        matched_rows_by_id = {
            _row_id(row): row
            for row in merged_by_base.values()
            if _row_id(row) is not None
        }
        active_rows_by_id = {
            _row_id(row): row
            for row in active_rows
            if _row_id(row) is not None
        }
        ordered_existing: list[TestRow] = []
        seen: set[str] = set()
        for row_id in tuple(working.row_order) + tuple(_row_id(row) for row in working_rows):
            if not row_id or row_id in seen:
                continue
            row = matched_rows_by_id.get(row_id) or active_rows_by_id.get(row_id)
            if row is not None:
                ordered_existing.append(row)
                seen.add(row_id)
        for base_index in sorted(merged_by_base):
            row = merged_by_base[base_index]
            row_id = _row_id(row)
            if row_id and row_id not in seen:
                ordered_existing.append(row)
                seen.add(row_id)
        # Include active WORKING-only rows not represented by row_order (for
        # example bootstrap rows that had no accepted order entry) in their
        # stable tuple order.
        for row in active_rows:
            row_id = _row_id(row)
            if row_id and row_id not in seen:
                ordered_existing.append(row)
                seen.add(row_id)

        # Fresh NEW rows are added in NEW tuple order.  Identity and case-number
        # allocation is monotonic and independent of dictionary/set iteration.
        for new_index, new_row in enumerate(new_rows):
            if new_index in new_to_base:
                continue
            row_id = _row_id_from_factory(self.uuid_factory, used_row_ids)
            allocation = allocate_case_name(
                f"{prefix}.c",
                next_case_number,
                used_case_names,
                on_collision="skip",
            )
            next_case_number = allocation.next_case_number
            used_case_names.add(allocation.case_name)
            meta = _new_meta(
                new_row,
                row_id=row_id,
                case_name=allocation.case_name,
                function_key=actual_function,
                generation=generation,
                origin="generated",
            )
            added = _with_meta(deepcopy(new_row), meta)
            ordered_existing.append(added)
            active_ids.append(row_id)
            added_ids.append(row_id)
            added_new_indices.append(new_index)

        active_rows = ordered_existing
        # Orphans are retained in state but excluded from the active row order.
        all_rows = tuple(active_rows + preserved_rows)
        row_order = tuple(_row_id(row) for row in active_rows if _row_id(row) is not None)
        # If a deleted WORKING row was matched to a modified NEW row, the
        # row-level merge reports a conflict.  Match diagnostics plus schema
        # blockers are deterministic and sorted at the final boundary.
        conflicts = _sort_conflicts(conflicts)
        blocked = any(item.severity == "blocking" for item in conflicts)
        if blocked:
            return MergeBResult(
                None,
                conflicts,
                False,
                tuple(sorted(matches, key=lambda item: (item.base_index, item.new_index))),
                tuple(added_ids),
                tuple(removed_ids),
                tuple(orphan_ids),
                tuple(superseded_ids),
            )

        try:
            candidate = WorkingSnapshot(
                snapshot_id=working.snapshot_id,
                generation=generation,
                analysis_id=new.analysis_id,
                row_order=row_order,
                rows=all_rows,
                arguments=tuple(new_arguments) if _is_argument_state_sequence(new_arguments) else working.arguments,
                tombstones=tuple(tombstones),
            )
        except (TypeError, ValueError) as exc:
            # A malformed candidate (for example a duplicate identity created
            # by an external snapshot) is a structured blocking conflict, not
            # an exception that can accidentally bypass the merge contract.
            conflict = _conflict(
                "AMBIGUOUS_ROW_MATCH",
                actual_function,
                entity_type="row",
                message=f"candidate identity/state is invalid: {exc}",
            )
            return MergeBResult(
                None,
                _sort_conflicts((*conflicts, conflict)),
                False,
                tuple(sorted(matches, key=lambda item: (item.base_index, item.new_index))),
                tuple(added_ids),
                tuple(removed_ids),
                tuple(orphan_ids),
                tuple(superseded_ids),
            )

        changed = _candidate_changed(working, candidate) or _generator_changed(base_snapshot, new)
        if accepted_state is not None:
            # PR13 owns accepted-analysis advancement.  Returning a state here
            # would violate FunctionIterationState's invariant when NEW uses a
            # different analysis ID, so the pure engine consistently returns a
            # WorkingSnapshot candidate for both call forms.
            del accepted_state
        return MergeBResult(
            candidate,
            conflicts,
            changed,
            tuple(sorted(matches, key=lambda item: (item.base_index, item.new_index))),
            tuple(added_ids),
            tuple(removed_ids),
            tuple(orphan_ids),
            tuple(superseded_ids),
        )

    def _validate_snapshot_identity(
        self,
        base: BaseSnapshot,
        working: WorkingSnapshot,
        new: BaseSnapshot,
        function_key: str,
    ) -> tuple[MergeConflict, ...]:
        conflicts: list[MergeConflict] = []
        if base.origin != "pure_new" or new.origin != "pure_new":
            conflicts.append(_conflict(
                "INVALID_GENERATION_SNAPSHOT",
                function_key,
                entity_type="snapshot",
                message="Merge B requires pure generator BASE and NEW snapshots",
                base=base.origin,
                user=working.snapshot_id,
                new=new.origin,
            ))
        if base.analysis_id != working.analysis_id:
            conflicts.append(_conflict(
                "ANALYSIS_DB_MISMATCH",
                function_key,
                entity_type="snapshot",
                message="WORKING is bound to a different accepted analysis than BASE",
                base=base.analysis_id,
                user=working.analysis_id,
                new=new.analysis_id,
            ))
        for label, rows in (("BASE", base.rows), ("WORKING", working.rows), ("NEW", new.rows)):
            for row in rows:
                row_function = _function_key(row)
                if row_function and row_function != function_key:
                    conflicts.append(_conflict(
                        "IDENTITY_FUNCTION_MISMATCH",
                        function_key,
                        entity_type="row",
                        row_id=_row_id(row),
                        case_name=_case_name(row),
                        message=f"{label} row belongs to function {row_function!r}, expected {function_key!r}",
                        user=row_function,
                    ))
        return conflicts

    def _duplicate_identity_conflicts(
        self,
        base_rows: Sequence[TestRow],
        new_rows: Sequence[TestRow],
        working_rows: Sequence[TestRow],
        function_key: str,
    ) -> tuple[MergeConflict, ...]:
        conflicts: list[MergeConflict] = []
        for label, rows in (("BASE", base_rows), ("NEW", new_rows), ("WORKING", working_rows)):
            ids: dict[str, TestRow] = {}
            cases: dict[str, TestRow] = {}
            for row in rows:
                obligation_mismatch = _obligation_consistency_error(row)
                if obligation_mismatch is not None:
                    persistent, runtime = obligation_mismatch
                    conflicts.append(_conflict(
                        "IDENTITY_OBLIGATION_CONFLICT",
                        function_key,
                        entity_type="row",
                        row_id=_row_id(row),
                        case_name=_case_name(row),
                        message=(
                            f"{label} row has runtime obligation_keys inconsistent with persisted RowMeta; "
                            "the persisted set is authoritative"
                        ),
                        base=tuple(sorted(persistent)),
                        user=tuple(sorted(runtime)),
                        new=_row_payload(row),
                    ))
                row_id = _row_id(row)
                if row_id and row_id in ids:
                    conflicts.append(_conflict(
                        "AMBIGUOUS_ROW_MATCH",
                        function_key,
                        entity_type="row",
                        row_id=row_id,
                        case_name=_case_name(row),
                        message=f"{label} contains duplicate row_id {row_id!r}",
                        base=_row_payload(ids[row_id]),
                        new=_row_payload(row),
                    ))
                elif row_id:
                    ids[row_id] = row
                case_name = _case_name(row)
                if case_name and case_name in cases:
                    conflicts.append(_conflict(
                        "AMBIGUOUS_ROW_MATCH",
                        function_key,
                        entity_type="row",
                        row_id=row_id,
                        case_name=case_name,
                        message=f"{label} contains duplicate case_name {case_name!r}",
                        base=_row_payload(cases[case_name]),
                        new=_row_payload(row),
                    ))
                elif case_name:
                    cases[case_name] = row
        return tuple(conflicts)

    def _match_rows(
        self,
        base_rows: Sequence[TestRow],
        new_rows: Sequence[TestRow],
        metadata: Mapping[str, object],
        function_key: str,
    ) -> tuple[tuple[RowMatch, ...], tuple[MergeConflict, ...]]:
        """Match BASE and NEW with exact, tiered global assignments.

        Matching is global within each precedence tier: a locally
        ambiguous row may still have one unique assignment once another row's
        only candidate is taken into account.
        """

        edges: list[_Edge] = []
        for base_index, base_row in enumerate(base_rows):
            for new_index, new_row in enumerate(new_rows):
                edge = _edge_for_rows(base_row, new_row, base_index, new_index, metadata, function_key)
                if edge is not None:
                    edges.append(edge)

        matched_edges: list[_Edge] = []
        conflicts: list[MergeConflict] = []
        used_base: set[int] = set()
        used_new: set[int] = set()
        reserved_base: set[int] = set()
        reserved_new: set[int] = set()

        def record(edge: _Edge) -> None:
            matched_edges.append(edge)
            used_base.add(edge.base_index)
            used_new.add(edge.new_index)

        # Stable lineage is the highest precedence.  Duplicate identity input
        # is diagnosed separately; the deterministic matching still keeps this
        # helper safe when it is called with such data directly.
        for edge in _maximum_matching((item for item in edges if item.score == 500)):
            record(edge)

        # Within each exact tier, maximize cardinality and then verify that
        # the maximum assignment is unique.  A local greedy choice would fail
        # A->{N1,N2}, B->{N1}: the only complete assignment is B->N1, A->N2.
        for score in (400, 300, 200, 100):
            tier_edges = [
                edge for edge in edges
                if edge.score == score
                and edge.base_index not in used_base
                and edge.new_index not in used_new
                and edge.base_index not in reserved_base
                and edge.new_index not in reserved_new
            ]
            if not tier_edges:
                continue
            selected = _maximum_matching(tier_edges)
            if not selected:
                continue

            ambiguous_bases: set[int] = set()
            ambiguous_news: set[int] = set()
            selected_pairs = {(edge.base_index, edge.new_index) for edge in selected}
            for edge in selected:
                alternative = _maximum_matching(
                    tier_edges,
                    forbidden=(edge.base_index, edge.new_index),
                )
                if len(alternative) != len(selected):
                    continue
                # Only the symmetric difference identifies the connected
                # choice component whose exact assignment is not deterministic.
                # Common edges can belong to an independent, uniquely assigned
                # component and must not be reported as ambiguous.
                alternative_pairs = {(item.base_index, item.new_index) for item in alternative}
                for candidate in selected:
                    if (candidate.base_index, candidate.new_index) not in alternative_pairs:
                        ambiguous_bases.add(candidate.base_index)
                        ambiguous_news.add(candidate.new_index)
                for candidate in alternative:
                    if (candidate.base_index, candidate.new_index) not in selected_pairs:
                        ambiguous_bases.add(candidate.base_index)
                        ambiguous_news.add(candidate.new_index)

            # Reserve ambiguous vertices for all lower tiers.  A lower-ranked
            # structural/value edge must never silently decide an unresolved
            # higher-ranked identity conflict.
            reserved_base.update(ambiguous_bases)
            reserved_new.update(ambiguous_news)

            tier_name = {
                400: "exact obligation-set",
                300: "compatible obligation/intent",
                200: "exact generated signature",
                100: "exact structural",
            }[score]
            for base_index in sorted(ambiguous_bases):
                candidates = sorted(
                    (edge for edge in tier_edges if edge.base_index == base_index),
                    key=lambda edge: (edge.new_index, edge.method),
                )
                conflicts.append(_conflict(
                    "AMBIGUOUS_ROW_MATCH",
                    function_key,
                    entity_type="row",
                    row_id=_row_id(base_rows[base_index]),
                    case_name=_case_name(base_rows[base_index]),
                    message=f"{tier_name} candidates admit multiple globally valid assignments",
                    base=_row_payload(base_rows[base_index]),
                    new=[_row_payload(new_rows[edge.new_index]) for edge in candidates],
                ))
            for new_index in sorted(ambiguous_news):
                candidates = sorted(
                    (edge for edge in tier_edges if edge.new_index == new_index),
                    key=lambda edge: (edge.base_index, edge.method),
                )
                conflicts.append(_conflict(
                    "AMBIGUOUS_ROW_MATCH",
                    function_key,
                    entity_type="row",
                    row_id=_row_id(new_rows[new_index]),
                    case_name=_case_name(new_rows[new_index]),
                    message=f"{tier_name} candidates admit multiple globally valid assignments",
                    base=[_row_payload(base_rows[edge.base_index]) for edge in candidates],
                    new=_row_payload(new_rows[new_index]),
                ))

            # Keep matches from independent, uniquely assigned components for
            # deterministic diagnostics.  Ambiguous components remain unused
            # and therefore cannot accidentally justify a lower tier.
            for edge in selected:
                if edge.base_index in ambiguous_bases or edge.new_index in ambiguous_news:
                    continue
                record(edge)

        return (
            tuple(
                RowMatch(
                    edge.base_index,
                    edge.new_index,
                    _row_id(base_rows[edge.base_index]),
                    _row_id(new_rows[edge.new_index]),
                    edge.method,
                    edge.score,
                )
                for edge in sorted(matched_edges, key=lambda item: (item.base_index, item.new_index))
            ),
            tuple(conflicts),
        )

    @staticmethod
    def _has_consumed_new_successor(
        base_index: int,
        base_rows: Sequence[TestRow],
        new_rows: Sequence[TestRow],
        new_to_base: Mapping[int, int],
        metadata: Mapping[str, object],
        function_key: str,
    ) -> bool:
        """Whether an unmatched BASE row had an exact edge to a used NEW row."""

        base_row = base_rows[base_index]
        for new_index, new_row in enumerate(new_rows):
            if new_index not in new_to_base or new_to_base[new_index] == base_index:
                continue
            if _edge_for_rows(base_row, new_row, base_index, new_index, metadata, function_key) is not None:
                return True
        return False

    def _merge_matched_row(
        self,
        base: TestRow,
        user: TestRow | None,
        new: TestRow,
        metadata: Mapping[str, object],
        function_key: str,
        *,
        resolution_resolver: Callable[[MergeConflict], str | None] | None = None,
    ) -> tuple[TestRow | None, tuple[MergeConflict, ...]]:
        conflicts: list[MergeConflict] = []

        def warning(conflict: MergeConflict, action: str) -> MergeConflict:
            return replace(
                conflict,
                severity="warning",
                message=f"{conflict.message} (explicit resolution: {action})",
            )

        def select(conflict: MergeConflict) -> str | None:
            """Return only an explicit action, or keep the conflict blocking.

            ``MergeConflict`` stores a safe, JSON-serializable diagnostic
            payload.  It is deliberately not an execution payload: converting
            ``CellValue`` instances or the private deletion sentinel for audit
            output would lose the typed/lexical value needed by the merge.
            Callers therefore select a side here and the code below reads that
            side directly from the original typed ``user``/``new`` rows.
            """

            if resolution_resolver is None:
                conflicts.append(conflict)
                return None
            try:
                action = resolution_resolver(conflict)
            except Exception:
                # Resolution callbacks are advisory inputs to the pure engine;
                # an exception must leave the conflict unresolved.
                action = None
            if not isinstance(action, str):
                conflicts.append(conflict)
                return None
            action = action.casefold()
            if action == "keep_user" and action in conflict.allowed_resolutions:
                conflicts.append(warning(conflict, action))
                return action
            if action == "keep_new" and action in conflict.allowed_resolutions:
                conflicts.append(warning(conflict, action))
                return action
            # ``cancel`` and every unknown/action-mismatch remain blocking.
            conflicts.append(conflict)
            return None

        if user is None:
            # Missing WORKING row is an explicit user deletion.  NEW must be
            # byte/semantically unchanged for that deletion to survive.
            if _semantic_row_equal(base, new, metadata):
                return None, ()
            conflict = _conflict(
                "USER_NEW_VALUE_CONFLICT",
                function_key,
                entity_type="row",
                row_id=_row_id(base),
                case_name=_case_name(base),
                message="USER deletion conflicts with a modified NEW row",
                base=_row_payload(base),
                user="<deleted>",
                new=_row_payload(new),
            )
            action = select(conflict)
            # Deletion is a genuine row-level value choice.  The callback can
            # select the explicit USER deletion or the complete NEW row; it
            # never allocates or reuses an identity here.
            if action == "keep_user":
                return None, tuple(conflicts)
            if action == "keep_new":
                return deepcopy(new), tuple(conflicts)
            return None, (conflict,)

        merged_values, value_conflicts = _merge_mapping(base.values, user.values, new.values, metadata=metadata)
        merged_expected, expected_conflicts = _merge_mapping(base.expected, user.expected, new.expected, metadata=metadata)
        for key in value_conflicts:
            conflict = _conflict(
                "USER_NEW_VALUE_CONFLICT",
                function_key,
                entity_type="value",
                row_id=_row_id(base),
                case_name=_case_name(user) or _case_name(base),
                logical_argument_key=str(key),
                message=f"input value changed differently by USER and NEW for {key!r}",
                base=_cell_for_mapping(base.values, key),
                user=_cell_for_mapping(user.values, key),
                new=_cell_for_mapping(new.values, key),
            )
            action = select(conflict)
            if action is not None:
                # Never select from ``conflict.user``/``conflict.new`` here:
                # those fields are the safe serialized diagnostics returned by
                # ``_safe_value``.  The original rows retain CellValue
                # instances, raw lexemes, and the private deletion sentinel.
                selected = (
                    _cell_for_mapping(user.values, key)
                    if action == "keep_user"
                    else _cell_for_mapping(new.values, key)
                )
                if selected is _DELETED:
                    merged_values.pop(key, None)
                else:
                    merged_values[key] = deepcopy(selected)
        for key in expected_conflicts:
            conflict = _conflict(
                "USER_NEW_VALUE_CONFLICT",
                function_key,
                entity_type="expected",
                row_id=_row_id(base),
                case_name=_case_name(user) or _case_name(base),
                logical_argument_key=str(key),
                message=f"expected output changed differently by USER and NEW for {key!r}",
                base=_cell_for_mapping(base.expected, key),
                user=_cell_for_mapping(user.expected, key),
                new=_cell_for_mapping(new.expected, key),
            )
            action = select(conflict)
            if action is not None:
                selected = (
                    _cell_for_mapping(user.expected, key)
                    if action == "keep_user"
                    else _cell_for_mapping(new.expected, key)
                )
                if selected is _DELETED:
                    merged_expected.pop(key, None)
                else:
                    merged_expected[key] = deepcopy(selected)

        merged_description, description_conflict = _merge_field(
            base.description,
            user.description,
            new.description,
            metadata=None,
        )
        if description_conflict:
            conflict = _conflict(
                "DESCRIPTION_CONFLICT",
                function_key,
                entity_type="description",
                row_id=_row_id(base),
                case_name=_case_name(user) or _case_name(base),
                message="Description changed differently by USER and NEW",
                base=base.description,
                user=user.description,
                new=new.description,
            )
            action = select(conflict)
            selected = user.description if action == "keep_user" else new.description
            if action is not None and isinstance(selected, str):
                merged_description = selected
            elif action is not None:
                conflicts.append(conflict)

        merged_kind, kind_conflict = _merge_field(base.kind, user.kind, new.kind, metadata=None)
        if kind_conflict:
            conflict = _conflict(
                "USER_NEW_VALUE_CONFLICT",
                function_key,
                entity_type="row",
                row_id=_row_id(base),
                case_name=_case_name(user) or _case_name(base),
                message="row kind changed differently by USER and NEW",
                base=base.kind,
                user=user.kind,
                new=new.kind,
            )
            action = select(conflict)
            selected = user.kind if action == "keep_user" else new.kind
            if action is not None and isinstance(selected, str):
                merged_kind = selected
            elif action is not None:
                conflicts.append(conflict)

        merged_intent, intent_conflict = _merge_field(
            base.intent,
            user.intent,
            new.intent,
            metadata=None,
        )
        if intent_conflict:
            conflict = _conflict(
                "USER_NEW_VALUE_CONFLICT",
                function_key,
                entity_type="intent",
                row_id=_row_id(base),
                case_name=_case_name(user) or _case_name(base),
                message="generator intent changed differently by USER and NEW",
                base=base.intent,
                user=user.intent,
                new=new.intent,
            )
            action = select(conflict)
            selected = user.intent if action == "keep_user" else new.intent
            if action is not None and isinstance(selected, Mapping):
                merged_intent = deepcopy(selected)
            elif action is not None:
                conflicts.append(conflict)

        merged = TestRow(
            values=merged_values,
            kind=merged_kind if isinstance(merged_kind, str) else user.kind,
            description=merged_description if isinstance(merged_description, str) else user.description,
            intent=merged_intent if isinstance(merged_intent, dict) else deepcopy(user.intent),
            expected=merged_expected,
            meta=user.meta or base.meta,
            obligation_keys=tuple(sorted(_obligations(new))),
        )
        return merged, tuple(conflicts)

    @staticmethod
    def _as_orphan(row: TestRow, generation: int, function_key: str) -> TestRow:
        meta = _row_meta(row)
        if meta is None:
            return row
        return _with_meta(row, replace(meta, status="orphaned", generation_modified=generation, function_key=function_key))

    @staticmethod
    def _as_superseded(row: TestRow, generation: int, function_key: str) -> TestRow:
        """Retain an untouched many-to-one loser as historical state.

        A row which had an exact candidate but lost because another BASE row
        deterministically consumed that NEW row is not the same thing as an
        obligation removed by the generator.  Keeping it as ``superseded``
        preserves the lineage/audit record while excluding it from active
        ``row_order``.
        """

        meta = _row_meta(row)
        if meta is None:
            return row
        return _with_meta(row, replace(meta, status="superseded", generation_modified=generation, function_key=function_key))

    def _validate_schema(
        self,
        working: WorkingSnapshot,
        base_arguments: object | None,
        new_arguments: object | None,
        metadata: Mapping[str, object],
        function_key: str,
    ) -> tuple[MergeConflict, ...]:
        if new_arguments is None:
            return ()
        base_map = _metadata_map(base_arguments)
        new_map = _metadata_map(new_arguments)
        conflicts: list[MergeConflict] = []
        reported_cells: set[tuple[str, str, str]] = set()

        def values_for_argument(rows: Iterable[TestRow], argument_name: str) -> Iterable[tuple[TestRow, str, object, str]]:
            # Return all indexed leaves too: ``x[0]`` is governed by the
            # logical ``x`` schema, and must not escape narrowing validation.
            for row in rows:
                for field_name, mapping in (("values", row.values), ("expected", row.expected)):
                    if not isinstance(mapping, Mapping):
                        continue
                    for cell_name, value in mapping.items():
                        text = str(cell_name)
                        if text == argument_name or re.sub(r"\[[0-9]+\]", "", text) == argument_name:
                            yield row, field_name, value, text

        # Compare logical schema entries first.  Mapping aliases (for example
        # ``x`` and ``x[0]``) are already collapsed by ``_metadata_map``.
        for key in sorted(set(base_map) | set(new_map)):
            before = base_map.get(key)
            after = new_map.get(key)
            if before is not None and after is None:
                for row, field_name, value, cell_name in values_for_argument(working.rows, key):
                    if isinstance(value, CellValue) and value.kind == "missing":
                        continue
                    marker = (row_id := (_row_id(row) or ""), field_name, cell_name)
                    if marker in reported_cells:
                        continue
                    reported_cells.add(marker)
                    conflicts.append(_conflict(
                        "ARGUMENT_SCHEMA_CONFLICT",
                        function_key,
                        entity_type="argument_schema",
                        row_id=row_id or None,
                        case_name=_case_name(row),
                        logical_argument_key=key,
                        message=f"NEW removed argument {key!r} still used by WORKING",
                        base=_schema_fields(before),
                        user=value,
                        new=None,
                    ))
                continue
            if after is None:
                continue
            validation_after = metadata.get(key, after)
            # Validate every preserved cell, including indexed leaves and
            # cells that were not manually edited.  Narrowing is a schema
            # concern, not a row-match concern.
            for row, field_name, value, cell_name in values_for_argument(working.rows, key):
                if _valid_for_metadata(value, validation_after):
                    continue
                marker = (_row_id(row) or "", field_name, cell_name)
                if marker in reported_cells:
                    continue
                reported_cells.add(marker)
                conflicts.append(_conflict(
                    "ARGUMENT_SCHEMA_CONFLICT",
                    function_key,
                    entity_type="argument_schema",
                    row_id=_row_id(row),
                    case_name=_case_name(row),
                    logical_argument_key=key,
                    message=f"preserved WORKING {field_name} value is incompatible with NEW schema",
                    base=_schema_fields(before),
                    user=value,
                    new=_schema_fields(after),
                ))
            if before is None:
                continue
            if _schema_incompatible(before, validation_after, working, key):
                conflicts.append(_conflict(
                    "ARGUMENT_SCHEMA_CONFLICT",
                    function_key,
                    entity_type="argument_schema",
                    logical_argument_key=key,
                    message=f"argument schema changed incompatibly for {key!r}",
                    base=_schema_fields(before),
                    user="WORKING",
                    new=_schema_fields(after),
                ))
        return tuple(conflicts)


def _infer_function_key(*snapshots: object) -> str | None:
    for snapshot in snapshots:
        rows = getattr(snapshot, "rows", ())
        for row in rows:
            value = _function_key(row)
            if value:
                return value
    return None


def _working_indexes(rows: Sequence[TestRow]) -> tuple[dict[str, TestRow], dict[str, TestRow]]:
    by_id: dict[str, TestRow] = {}
    by_case: dict[str, TestRow] = {}
    for row in rows:
        row_id = _row_id(row)
        if row_id:
            by_id.setdefault(row_id, row)
        case_name = _case_name(row)
        if case_name:
            by_case.setdefault(case_name, row)
    return by_id, by_case


def _mapping_has_value(mapping: object, key: str) -> bool:
    if not isinstance(mapping, Mapping) or key not in mapping:
        return False
    value = mapping[key]
    return not (isinstance(value, CellValue) and value.kind == "missing")


def _mapping_has_argument_value(mapping: object, key: str) -> bool:
    """Return whether a logical argument has any retained scalar/leaf value."""

    if not isinstance(mapping, Mapping):
        return False
    for cell_name, value in mapping.items():
        text = str(cell_name)
        logical_name = re.sub(r"\[[0-9]+\]", "", text)
        if text == key or logical_name == key:
            if not (isinstance(value, CellValue) and value.kind == "missing"):
                return True
    return False


def _is_argument_state_sequence(value: object) -> bool:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return False
    try:
        return all(isinstance(item, ArgumentState) for item in value)  # type: ignore[arg-type]
    except TypeError:
        return False


def _schema_incompatible(
    before: object,
    after: object,
    working: WorkingSnapshot,
    key: str,
) -> bool:
    def value(name: str, item: object) -> object:
        return _first_attr(item, (name,), None)

    # Pointer/direction/shape are structural schema.  A change is not a
    # lexical conversion and therefore cannot be silently carried across a
    # retained row, even when its current cells happen to be empty.
    for field_name in ("pointer", "direction", "dimensions"):
        before_value = value(field_name, before)
        after_value = value(field_name, after)
        if before_value is not None and after_value is not None and _freeze(before_value) != _freeze(after_value):
            return True

    before_fmt = value("fmt", before) or value("format", before)
    after_fmt = value("fmt", after) or value("format", after)
    before_len = value("xml_len", before) or value("len", before)
    after_len = value("xml_len", after) or value("len", after)
    if before_fmt == after_fmt and before_len == after_len:
        return False

    # A known numeric conversion (including a narrowing conversion) is
    # permitted only after every retained cell has passed `_valid_for_metadata`
    # against NEW.  Unknown/non-numeric changes require a populated value to
    # be blocked because no safe migration proof exists.
    before_type = _numeric_type(before)
    after_type = _numeric_type(after)
    if before_type is not None and after_type is not None:
        return False
    return any(
        _mapping_has_argument_value(row.values, key) or _mapping_has_argument_value(row.expected, key)
        for row in working.rows
    )


def _numeric_type(argument: object) -> tuple[bool, int] | None:
    if argument is None:
        return None
    bits = _first_attr(argument, ("bits",), None)
    signed = _first_attr(argument, ("signed",), None)
    fmt = _first_attr(argument, ("fmt", "format", "type_name", "ctype", "type"), None)
    if bits is not None:
        try:
            bits_value = int(bits)
        except (TypeError, ValueError):
            bits_value = 0
        if bits_value > 0:
            if signed is None:
                text = str(fmt or "").strip().lower().replace(" ", "")
                signed = not (text.startswith("u") or text.startswith("unsigned"))
            return bool(signed), bits_value
    if fmt is None:
        return None
    text = str(fmt).strip().lower().replace(" ", "")
    if text in {"bool", "_bool", "boolean"}:
        return False, 1
    unsigned = text.startswith("u") or text.startswith("unsigned")
    # Cover fixed-width C spellings (uint8_t/int16_t), compact adapter hints
    # (u8/i16), and explicit ``unsigned int`` forms.  Do not guess widths for
    # bare ``int``/``long`` because ABI width is configuration-owned.
    match = re.search(r"(?:u?int|integer|short|long|char|byte|word)(8|16|24|32|64|128)(?:_t)?$", text)
    if match is None:
        match = re.fullmatch(r"(?:u|i)?(8|16|24|32|64|128)", text)
    if match is None:
        return None
    return (not unsigned), int(match.group(1))


def _valid_for_metadata(value: object, argument: object) -> bool:
    if isinstance(value, CellValue):
        if value.kind == "missing":
            return True
        if value.kind == "explicit_empty":
            return True
        value = value.semantic_value if value.semantic_value is not None else value.raw_text
    if value is _DELETED:
        return True
    if isinstance(value, (tuple, list)):
        return all(_valid_for_metadata(item, argument) for item in value)
    if isinstance(value, Mapping):
        return all(_valid_for_metadata(item, argument) for item in value.values())
    domain = _first_attr(argument, ("domain", "values", "enum_values"), None)
    enum_proven = False
    if domain is not None:
        # An explicit domain is stronger than the underlying integer width:
        # reject values which are not a declared enum/member alias.
        enum_value = _enum_value(value, argument)
        if enum_value is _UNKNOWN:
            if isinstance(domain, Mapping):
                return False
            try:
                members = tuple(domain)
            except TypeError:
                return False
            if not any(
                (isinstance(item, (tuple, list)) and len(item) >= 2 and value in (item[0], item[1]))
                or (not isinstance(item, (tuple, list)) and value == item)
                for item in members
            ):
                return False
        elif isinstance(enum_value, tuple) and enum_value and enum_value[0] == "member":
            # Symbol-only domains prove membership but have no numeric width;
            # they are valid here and need no further range check.
            return True
        elif enum_value is not _UNKNOWN:
            value = enum_value
            enum_proven = True
    numeric = _numeric_type(argument)
    if numeric is None:
        if domain is not None:
            if enum_proven:
                return True
            try:
                return value in domain
            except TypeError:
                return False
        return True
    signed, bits = numeric
    number = _integer_value(value)
    if number is None:
        # An enum symbol may be valid even though its underlying numeric value
        # is not visible in the cell; explicit domain metadata is required.
        domain = _first_attr(argument, ("domain", "values", "enum_values"), None)
        if domain is not None:
            try:
                return value in domain
            except TypeError:
                return False
        return False
    if signed:
        return -(1 << (bits - 1)) <= number <= (1 << (bits - 1)) - 1
    return 0 <= number <= (1 << bits) - 1


def _candidate_changed(before: WorkingSnapshot, after: WorkingSnapshot) -> bool:
    def normalized(snapshot: WorkingSnapshot) -> object:
        rows = []
        for row in snapshot.rows:
            meta = _row_meta(row)
            rows.append((
                _row_id(row),
                _case_name(row),
                None if meta is None else meta.status,
                row.kind,
                row.description,
                _freeze(row.values),
                _freeze(row.expected),
                _freeze(row.intent),
                tuple(sorted(_obligations(row))),
            ))
        return (
            tuple(rows),
            tuple(snapshot.row_order),
            tuple(snapshot.arguments),
            tuple(snapshot.tombstones),
        )

    return normalized(before) != normalized(after)


def _generator_changed(base: BaseSnapshot, new: BaseSnapshot) -> bool:
    if base.analysis_id != new.analysis_id:
        # Distinct analysis IDs are source-analysis identity changes.  The pure
        # row comparison below still controls no-op when callers reused an ID
        # for byte-equivalent snapshots.
        return True
    if len(base.rows) != len(new.rows):
        return True
    for left, right in zip(base.rows, new.rows):
        if _obligations(left) != _obligations(right):
            return True
        if _generated_signature(left, {}) != _generated_signature(right, {}):
            return True
        if not _mapping_equal(left.expected, right.expected):
            return True
    return False


def _sort_conflicts(conflicts: Iterable[MergeConflict]) -> tuple[MergeConflict, ...]:
    # Keep duplicate diagnostics (they can represent distinct fields), but
    # canonicalize their order for stable reports and serialized state.
    return tuple(sorted(
        conflicts,
        key=lambda item: (
            item.code,
            item.entity_type,
            item.function_key,
            item.row_id or "",
            item.case_name or "",
            item.logical_argument_key or "",
            item.message,
        ),
    ))


def merge_b(
    base: BaseSnapshot | FunctionIterationState,
    working: WorkingSnapshot | None,
    new: BaseSnapshot,
    **kwargs: Any,
) -> MergeBResult:
    """Convenience wrapper for :class:`MergeBEngine`."""

    return MergeBEngine().merge(base, working, new, **kwargs)


__all__ = [
    "GenerationSnapshot",
    "MergeBEngine",
    "MergeBError",
    "MergeBResult",
    "RowMatch",
    "StubLifecycleResult",
    "merge_stub_states",
    "merge_b",
]
