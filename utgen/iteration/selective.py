"""Selective regeneration, deterministic caches, and phase instrumentation.

The selective layer sits around the existing generation pipeline.  It makes the
regeneration decision from exact semantic fingerprints and dependency edges,
then invokes expensive callbacks only for selected functions.  It is deliberately
independent from the repository and Merge B state machines: a timeout or callback
failure yields a blocked result and can never turn an older function into a new
BASE implicitly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields as dataclass_fields, is_dataclass, replace
from hashlib import sha256
import inspect
import json
import re
import time
from typing import Any, Iterator

from .fingerprint import (
    FingerprintConfidence,
    FingerprintDetails,
    canonical_fingerprint_payload,
    config_fingerprint as _config_fingerprint,
    dependency_fingerprint_details as _dependency_fingerprint_details,
    function_fingerprint_details as _function_fingerprint_details,
)
from .model import BaseSnapshot


HIGH_CONFIDENCE = FingerprintConfidence.HIGH.value
LOW_CONFIDENCE = FingerprintConfidence.LOW.value
REQUIRED_PHASES = ("generation", "parsing", "matching", "verification", "commit")


class SelectiveRegenerationError(ValueError):
    """Base error for invalid selective-regeneration inputs."""


class RegenerationTimeout(TimeoutError, SelectiveRegenerationError):
    """Raised by a pipeline or the deadline guard when generation times out."""

    code = "REGENERATION_TIMEOUT"


@dataclass(frozen=True)
class RegenerationConflict:
    """Blocking diagnostic returned for a failed selective operation."""

    code: str
    function_key: str
    message: str
    severity: str = "blocking"
    previous: Any = None
    candidate: Any = None


@dataclass
class PhaseMetric:
    """Counters and elapsed time for one named pipeline phase."""

    calls: int = 0
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "duration_seconds": self.duration_seconds}


class PhaseMetrics:
    """Deterministic phase/callback metrics with an injectable clock.

    Phase names and counter keys are serialized in sorted order.  The clock is
    injectable so tests can assert exact durations without sleeping.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.perf_counter
        # Keep the operational report shape stable even when a fast path does
        # not execute a phase.  A zero-call phase is evidence that the phase was
        # considered and skipped, rather than an absent/ambiguous metric.
        self._phases: dict[str, PhaseMetric] = {
            name: PhaseMetric() for name in REQUIRED_PHASES
        }
        self._counters: dict[str, int] = {}

    @property
    def phases(self) -> Mapping[str, PhaseMetric]:
        return self._phases

    @property
    def counters(self) -> Mapping[str, int]:
        return self._counters

    def increment(self, name: str, amount: int = 1) -> int:
        key = str(name)
        self._counters[key] = self._counters.get(key, 0) + int(amount)
        return self._counters[key]

    def count(self, name: str, amount: int = 1) -> int:
        return self.increment(name, amount)

    @property
    def total_duration_seconds(self) -> float:
        return sum(item.duration_seconds for item in self._phases.values())

    @property
    def phase_durations(self) -> Mapping[str, float]:
        return {key: value.duration_seconds for key, value in sorted(self._phases.items())}

    def record(self, name: str, duration_seconds: float = 0.0, *, calls: int = 1) -> PhaseMetric:
        key = str(name)
        metric = self._phases.setdefault(key, PhaseMetric())
        metric.calls += int(calls)
        metric.duration_seconds += max(0.0, float(duration_seconds))
        return metric

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        start = self._clock()
        try:
            yield
        finally:
            self.record(name, self._clock() - start)

    measure = phase

    def snapshot(self) -> dict[str, Any]:
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "phases": {
                key: {
                    "calls": value.calls,
                    "duration_seconds": value.duration_seconds,
                }
                for key, value in sorted(self._phases.items())
            },
            "counters": dict(sorted(self._counters.items())),
        }

    as_dict = to_dict

    def merge(self, other: "PhaseMetrics") -> "PhaseMetrics":
        if not isinstance(other, PhaseMetrics):
            raise TypeError("other must be PhaseMetrics")
        for key, metric in other._phases.items():
            self.record(key, metric.duration_seconds, calls=metric.calls)
        for key, value in other._counters.items():
            self.increment(key, value)
        return self


PerformanceMetrics = PhaseMetrics
RegenerationMetrics = PhaseMetrics


def _stable_payload(value: Any) -> bytes:
    """Encode arbitrary cache input without process-randomized ``hash()``."""

    try:
        encoded = canonical_fingerprint_payload(value)
        # ``fingerprint`` intentionally marks unrecognized objects using their
        # diagnostic repr.  A repr containing an object address is unsuitable
        # as a cache key, so use the structural fallback below in that case.
        if not re.search(rb"0x[0-9a-fA-F]{8,}", encoded):
            return encoded
    except Exception:
        encoded = None

    def normalize(item: Any, seen: set[int] | None = None) -> Any:
        if seen is None:
            seen = set()
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        if isinstance(item, (bytes, bytearray)):
            return bytes(item).hex()
        identity = id(item)
        if identity in seen:
            return {"$cycle": type(item).__name__}
        seen.add(identity)
        if isinstance(item, Mapping):
            result = {str(key): normalize(item[key], seen) for key in sorted(item, key=str)}
            seen.remove(identity)
            return result
        if isinstance(item, (list, tuple)):
            result = [normalize(child, seen) for child in item]
            seen.remove(identity)
            return result
        if isinstance(item, (set, frozenset)):
            result = sorted((normalize(child, seen) for child in item), key=lambda child: repr(child))
            seen.remove(identity)
            return result
        fields = getattr(item, "__dataclass_fields__", None)
        if fields:
            result = normalize({name: getattr(item, name) for name in fields}, seen)
            seen.remove(identity)
            return result
        attrs = getattr(item, "__dict__", None)
        if isinstance(attrs, Mapping):
            result = {"type": f"{type(item).__module__}.{type(item).__qualname__}", "attributes": normalize(attrs, seen)}
            seen.remove(identity)
            return result
        # Type identity is deterministic; unlike repr(item), it has no address.
        seen.remove(identity)
        return {"type": f"{type(item).__module__}.{type(item).__qualname__}"}

    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


class FingerprintCache(MutableMapping[str, Any]):
    """Content-keyed in-memory cache for normalized fingerprint results.

    Keys are SHA-256 digests of a namespace plus canonical context.  This keeps
    cache behavior independent from mapping order and Python's randomized hash
    seed.  ``get_or_compute`` is the primitive used by the planner; convenience
    methods cache the repository's function/dependency/config fingerprint helpers.
    """

    def __init__(
        self,
        initial: Mapping[str, Any] | None = None,
        *,
        metrics: PhaseMetrics | None = None,
    ) -> None:
        self._values: dict[str, Any] = {}
        self._hits = 0
        self._misses = 0
        self.metrics = metrics
        if initial:
            for key, value in initial.items():
                self._values[str(key)] = deepcopy(value)

    @staticmethod
    def make_key(*parts: Any, namespace: str = "fingerprint", **context: Any) -> str:
        payload = {"namespace": str(namespace), "parts": parts, "context": context}
        return sha256(_stable_payload(payload)).hexdigest()

    key = make_key
    cache_key = make_key

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def hit_count(self) -> int:
        return self._hits

    @property
    def miss_count(self) -> int:
        return self._misses

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._values[key])

    def __setitem__(self, key: str, value: Any) -> None:
        self._values[str(key)] = deepcopy(value)

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._values))

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: Any = None, **context: Any) -> Any:  # type: ignore[override]
        actual = self.make_key(key, namespace=context.pop("namespace", "fingerprint"), **context) if context else str(key)
        if actual not in self._values:
            self._misses += 1
            if self.metrics is not None:
                self.metrics.increment("fingerprint_cache_misses")
            return default
        self._hits += 1
        if self.metrics is not None:
            self.metrics.increment("fingerprint_cache_hits")
        return deepcopy(self._values[actual])

    def put(self, key: Any, value: Any, *, namespace: str | None = None, **context: Any) -> Any:
        actual = self.make_key(key, namespace=namespace, **context) if namespace is not None or context else str(key)
        self._values[actual] = deepcopy(value)
        return value

    set = put

    def get_or_compute(
        self,
        key: Any,
        factory: Callable[[], Any],
        *,
        namespace: str | None = None,
        **context: Any,
    ) -> Any:
        actual = self.make_key(key, namespace=namespace, **context) if namespace is not None or context else str(key)
        if actual in self._values:
            self._hits += 1
            if self.metrics is not None:
                self.metrics.increment("fingerprint_cache_hits")
            return deepcopy(self._values[actual])
        self._misses += 1
        if self.metrics is not None:
            self.metrics.increment("fingerprint_cache_misses")
        if self.metrics is None:
            value = factory()
        else:
            with self.metrics.phase("fingerprint"):
                value = factory()
        self._values[actual] = deepcopy(value)
        return value

    compute = get_or_compute
    get_or_set = get_or_compute
    lookup = get
    remember = put

    @property
    def cache_hits(self) -> int:
        return self._hits

    @property
    def cache_misses(self) -> int:
        return self._misses

    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "entries": len(self._values)}

    def to_dict(self) -> dict[str, Any]:
        return {key: deepcopy(self._values[key]) for key in sorted(self._values)}

    as_dict = to_dict

    def clear(self) -> None:
        self._values.clear()
        self._hits = self._misses = 0

    def function_fingerprint(self, analysis_or_function: Any, function: Any = None, **kwargs: Any) -> FingerprintDetails:
        context = {
            name: kwargs.pop(name)
            for name in ("analysis_revision", "accepted_revision", "revision", "config_revision")
            if name in kwargs
        }
        key = ("function", context, kwargs, _fingerprint_name(function) or _fingerprint_name(analysis_or_function), _fingerprint_seed(analysis_or_function, function))
        return self.get_or_compute(key, lambda: _function_fingerprint_details(analysis_or_function, function, **kwargs))

    def dependency_fingerprint(self, analysis_or_function: Any, function: Any = None, config: Any = None, **kwargs: Any) -> FingerprintDetails:
        context = {
            name: kwargs.pop(name)
            for name in ("analysis_revision", "accepted_revision", "revision", "config_revision")
            if name in kwargs
        }
        key = ("dependency", context, kwargs, _fingerprint_name(function) or _fingerprint_name(analysis_or_function), _fingerprint_seed(analysis_or_function, function), config)
        return self.get_or_compute(key, lambda: _dependency_fingerprint_details(analysis_or_function, function, config, **kwargs))

    def config_fingerprint(self, config: Any = None, **kwargs: Any) -> str:
        context = {
            name: kwargs.pop(name)
            for name in ("analysis_revision", "accepted_revision", "revision", "config_revision")
            if name in kwargs
        }
        key = ("config", context, config, kwargs)
        return self.get_or_compute(key, lambda: _config_fingerprint(config, **kwargs))

    get_function_fingerprint = function_fingerprint
    get_dependency_fingerprint = dependency_fingerprint
    get_config_fingerprint = config_fingerprint


DeterministicFingerprintCache = FingerprintCache


def cached_function_fingerprint(cache: FingerprintCache, analysis_or_function: Any, function: Any = None, **kwargs: Any) -> FingerprintDetails:
    """Compute a function fingerprint through a caller-owned cache."""

    return cache.function_fingerprint(analysis_or_function, function, **kwargs)


def cached_dependency_fingerprint(cache: FingerprintCache, analysis_or_function: Any, function: Any = None, config: Any = None, **kwargs: Any) -> FingerprintDetails:
    """Compute a dependency fingerprint through a caller-owned cache."""

    return cache.dependency_fingerprint(analysis_or_function, function, config, **kwargs)


def cached_config_fingerprint(cache: FingerprintCache, config: Any = None, **kwargs: Any) -> str:
    """Compute a generation-config fingerprint through a caller-owned cache."""

    return cache.config_fingerprint(config, **kwargs)


def _fingerprint_seed(value: Any, other: Any = None) -> Any:
    if isinstance(value, FingerprintDetails):
        return value.fingerprint
    if isinstance(other, FingerprintDetails):
        return other.fingerprint
    if isinstance(value, Mapping):
        for key in ("fingerprint", "function_fingerprint", "analysis_id"):
            if key in value:
                return value[key]
    return value


def _fingerprint_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("function_key", "function", "name", "key"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    for key in ("function_key", "function", "name", "key"):
        candidate = getattr(value, key, None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


@dataclass(frozen=True)
class FunctionFingerprint:
    function_key: str
    fingerprint: str | None
    dependency_fingerprint: str | None = None
    confidence: str = HIGH_CONFIDENCE
    dependencies: tuple[str, ...] = ()
    dependency_complete: bool | None = None

    @property
    def low_confidence(self) -> bool:
        return self.confidence != HIGH_CONFIDENCE

    @property
    def name(self) -> str:
        return self.function_key

    @property
    def function(self) -> str:
        return self.function_key

    @property
    def function_fingerprint(self) -> str | None:
        return self.fingerprint

    @property
    def dependencies_complete(self) -> bool | None:
        return self.dependency_complete


FingerprintRecord = FunctionFingerprint


def _field(value: Any, names: Sequence[str], default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return default


def _as_dependencies(value: Any) -> tuple[str, ...]:
    raw = _field(value, ("dependencies", "depends_on", "callees", "calls", "helpers"), ())
    if isinstance(raw, Mapping):
        raw = raw.keys()
    if isinstance(raw, (str, bytes)):
        raw = (raw,)
    try:
        return tuple(sorted({str(item) for item in raw if str(item)}))
    except TypeError:
        return ()


def _as_record(name: str, value: Any) -> FunctionFingerprint:
    if isinstance(value, FunctionFingerprint):
        if value.function_key == name:
            return value
        return FunctionFingerprint(
            name,
            value.fingerprint,
            value.dependency_fingerprint,
            value.confidence,
            value.dependencies,
            value.dependency_complete,
        )
    if isinstance(value, FingerprintDetails):
        # FingerprintDetails carries an explicit confidence result.  A high
        # confidence dependency result is sufficient evidence; a bare string
        # below intentionally is not.
        return FunctionFingerprint(
            name,
            value.fingerprint,
            None,
            value.confidence,
            (),
            value.confidence == HIGH_CONFIDENCE and not value.conservative,
        )
    fp = _field(value, ("fingerprint", "function_fingerprint", "value"), value if isinstance(value, str) else None)
    dep = _field(value, ("dependency_fingerprint", "dependencies_fingerprint", "dependency"), None)
    confidence = _field(value, ("confidence",), HIGH_CONFIDENCE)
    if _field(value, ("conservative", "requires_conservative_handling", "low_confidence"), False):
        confidence = LOW_CONFIDENCE
    confidence = getattr(confidence, "value", confidence)
    if isinstance(value, Mapping):
        explicit_complete = _field(
            value,
            ("dependency_complete", "dependencies_complete", "dependency_graph_complete"),
            None,
        )
        if explicit_complete is None and "confidence" in value:
            explicit_complete = str(confidence) == HIGH_CONFIDENCE
    else:
        explicit_complete = _field(
            value,
            ("dependency_complete", "dependencies_complete", "dependency_graph_complete"),
            None,
        )
        if explicit_complete is None and hasattr(value, "confidence"):
            explicit_complete = str(confidence) == HIGH_CONFIDENCE
    if explicit_complete is not None:
        explicit_complete = bool(explicit_complete)
    return FunctionFingerprint(
        name,
        None if fp is None else str(fp),
        None if dep is None else str(dep),
        str(confidence),
        _as_dependencies(value),
        explicit_complete,
    )


def _records(value: Any, *, names: Iterable[str] = ()) -> dict[str, FunctionFingerprint]:
    if value is None:
        return {}
    if isinstance(value, Mapping) and "functions" in value and isinstance(value["functions"], (Mapping, Sequence)):
        value = value["functions"]
    result: dict[str, FunctionFingerprint] = {}
    if isinstance(value, Mapping):
        for raw_name, item in value.items():
            name = str(raw_name)
            result[name] = _as_record(name, item)
    else:
        try:
            items = tuple(value)
        except TypeError:
            items = ()
        for item in items:
            name = _fingerprint_name(item)
            if name:
                result[name] = _as_record(name, item)
    for name in names:
        result.setdefault(str(name), FunctionFingerprint(str(name), None, None, LOW_CONFIDENCE, ()))
    return result


@dataclass(frozen=True)
class RegenerationPlan:
    """The deterministic function universe and selective target for one run.

    ``all_functions`` is intentionally retained as the decision universe.  It
    can contain historical functions which disappeared from the current
    source, because those identities still participate in change detection and
    dependency propagation.  ``target_functions`` is the current/NEW active
    set that may receive a generated snapshot.  The latter distinction is
    important for a complete Merge B map: deleted historical functions must
    not be sent to a generator or emitted as NEW entries.

    The field is optional for compatibility with plans constructed by older
    callers.  Such plans predate the decision-universe/target split and are
    treated as targeting their complete ``all_functions`` set.
    """

    all_functions: tuple[str, ...]
    selected_functions: tuple[str, ...]
    skipped_functions: tuple[str, ...]
    changed_functions: tuple[str, ...]
    dependency_affected_functions: tuple[str, ...]
    reasons: Mapping[str, str] = field(default_factory=dict)
    confidence: str = HIGH_CONFIDENCE
    conservative: bool = False
    conservative_reason: str | None = None
    target_functions: tuple[str, ...] | None = None

    @property
    def decision_universe(self) -> tuple[str, ...]:
        """Names considered while classifying source/dependency changes."""

        return self.all_functions

    @property
    def current_active_functions(self) -> tuple[str, ...]:
        """Current/NEW function keys eligible for snapshot generation."""

        return self.all_functions if self.target_functions is None else self.target_functions

    @property
    def active_functions(self) -> tuple[str, ...]:
        """Alias for :attr:`current_active_functions`."""

        return self.current_active_functions

    @property
    def selected(self) -> tuple[str, ...]:
        return self.selected_functions

    @property
    def skipped(self) -> tuple[str, ...]:
        return self.skipped_functions

    @property
    def regenerate(self) -> tuple[str, ...]:
        return self.selected_functions

    @property
    def unchanged_functions(self) -> tuple[str, ...]:
        return self.skipped_functions

    @property
    def functions(self) -> tuple[str, ...]:
        return self.all_functions

    @property
    def affected_functions(self) -> tuple[str, ...]:
        return self.selected_functions

    @property
    def unchanged(self) -> tuple[str, ...]:
        return self.skipped_functions

    def is_selected(self, function_key: str) -> bool:
        return function_key in self.selected_functions

    def is_unchanged(self, function_key: str) -> bool:
        return function_key in self.skipped_functions

    def reason_for(self, function_key: str) -> str | None:
        return self.reasons.get(function_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_functions": list(self.all_functions),
            "decision_universe": list(self.all_functions),
            "target_functions": list(self.current_active_functions),
            "selected_functions": list(self.selected_functions),
            "skipped_functions": list(self.skipped_functions),
            "changed_functions": list(self.changed_functions),
            "dependency_affected_functions": list(self.dependency_affected_functions),
            "reasons": dict(sorted(self.reasons.items())),
            "confidence": self.confidence,
            "conservative": self.conservative,
            "conservative_reason": self.conservative_reason,
        }

    as_dict = to_dict


RegenerationDecision = RegenerationPlan
SelectivePlan = RegenerationPlan


def compute_regeneration_plan(
    previous: Any = None,
    current: Any = None,
    *,
    functions: Iterable[str] | None = None,
    target_functions: Iterable[str] | None = None,
    dependency_graph: Mapping[str, Iterable[str]] | None = None,
    previous_dependency_graph: Mapping[str, Iterable[str]] | None = None,
    old_dependency_graph: Mapping[str, Iterable[str]] | None = None,
    current_dependency_graph: Mapping[str, Iterable[str]] | None = None,
    new_dependency_graph: Mapping[str, Iterable[str]] | None = None,
    dependencies: Mapping[str, Iterable[str]] | None = None,
    config_changed: bool = False,
    generator_changed: bool = False,
    old_config_fingerprint: str | None = None,
    new_config_fingerprint: str | None = None,
    config_fingerprint_old: str | None = None,
    config_fingerprint_new: str | None = None,
    old_generator_version: str | None = None,
    new_generator_version: str | None = None,
    generator_version_old: str | None = None,
    generator_version_new: str | None = None,
    conservative: bool = False,
    dependency_graph_complete: bool | None = None,
    dependencies_complete: bool | None = None,
    dependency_complete: bool | None = None,
    metrics: PhaseMetrics | None = None,
    **aliases: Any,
) -> RegenerationPlan:
    """Classify exact function changes and dependency-affected callers.

    ``previous`` and ``current`` accept mappings from function name to a string,
    :class:`FunctionFingerprint`, :class:`FingerprintDetails`, or a record-like
    mapping.  Missing fingerprints and low-confidence details force a conservative
    all-function regeneration so no caller can be silently skipped.
    """

    old_config_fingerprint = old_config_fingerprint or config_fingerprint_old
    new_config_fingerprint = new_config_fingerprint or config_fingerprint_new
    old_generator_version = old_generator_version or generator_version_old
    new_generator_version = new_generator_version or generator_version_new
    config_changed = bool(config_changed or (
        old_config_fingerprint is not None
        and new_config_fingerprint is not None
        and old_config_fingerprint != new_config_fingerprint
    ))
    generator_changed = bool(generator_changed or (
        old_generator_version is not None
        and new_generator_version is not None
        and old_generator_version != new_generator_version
    ))
    if previous is None:
        previous = aliases.pop("old", aliases.pop("old_fingerprints", None))
    if current is None:
        current = aliases.pop("new", aliases.pop("new_fingerprints", None))
    if target_functions is None:
        target_functions = aliases.pop(
            "target_functions",
            aliases.pop(
                "active_functions",
                aliases.pop("current_functions", aliases.pop("new_functions", None)),
            ),
        )
    if previous_dependency_graph is None:
        previous_dependency_graph = old_dependency_graph
    if previous_dependency_graph is None:
        previous_dependency_graph = aliases.pop(
            "previous_dependency_graph",
            aliases.pop("old_dependency_graph", aliases.pop("previous_graph", aliases.pop("old_graph", None))),
        )
    if current_dependency_graph is None:
        current_dependency_graph = new_dependency_graph
    if current_dependency_graph is None:
        current_dependency_graph = aliases.pop(
            "current_dependency_graph",
            aliases.pop("new_dependency_graph", aliases.pop("current_graph", aliases.pop("new_graph", None))),
        )
    if dependency_graph is None:
        dependency_graph = dependencies or aliases.pop("call_graph", aliases.pop("graph", None))
    # ``dependency_graph`` is the historical single-graph spelling.  Explicit
    # current/new graph parameters identify a current-only view, while the two
    # versioned graphs below let callers prove edge removals against the prior
    # source.  Keep the legacy spelling exact for existing callers/tests.
    current_graph_input = current_dependency_graph if current_dependency_graph is not None else dependency_graph
    old_graph_input = previous_dependency_graph

    def normalize_graph(value: Mapping[str, Iterable[str]] | None) -> dict[str, tuple[str, ...] | None] | None:
        if value is None:
            return None
        normalized: dict[str, tuple[str, ...] | None] = {}
        for raw_caller, raw_callees in value.items():
            caller = str(raw_caller)
            if raw_callees is None:
                normalized[caller] = None
                continue
            if isinstance(raw_callees, (str, bytes)):
                raw_callees = (raw_callees,)
            try:
                normalized[caller] = tuple(sorted({str(item) for item in raw_callees if str(item)}))
            except TypeError:
                normalized[caller] = None
        return normalized

    old_graph = normalize_graph(old_graph_input)
    current_graph = normalize_graph(current_graph_input)
    if dependency_graph_complete is None:
        dependency_graph_complete = dependencies_complete
    if dependency_graph_complete is None:
        dependency_graph_complete = dependency_complete
    requested = tuple(str(item) for item in (functions or ()))
    old = _records(previous, names=requested)
    new = _records(current, names=requested)
    names = set(old) | set(new) | set(requested)
    for graph in (old_graph, current_graph):
        if graph is None:
            continue
        names.update(graph)
        for values in graph.values():
            if values is not None:
                names.update(values)
    names_tuple = tuple(sorted(names))
    if metrics is not None:
        metrics.increment("functions_total", len(names_tuple))

    fingerprint_low = False
    dependency_low = False
    incomplete_reason = "dependency_graph_incomplete"
    for record in (*old.values(), *new.values()):
        if record.confidence != HIGH_CONFIDENCE or record.fingerprint is None:
            fingerprint_low = True
            incomplete_reason = "fingerprint_confidence_low"
        elif record.dependency_complete is not True:
            # A function hash alone cannot prove that no caller is affected.
            # Plain string fingerprints deliberately take this branch; callers
            # must provide a complete graph or an explicit complete marker.
            dependency_low = True
            incomplete_reason = "dependency_evidence_incomplete"
    graph_proves_complete = False

    def graph_covers(graph: dict[str, tuple[str, ...] | None] | None, expected: set[str]) -> bool:
        if graph is None:
            return False
        return all(name in graph and graph[name] is not None for name in expected)

    old_names = set(old) | set(requested)
    new_names = set(new) | set(requested)
    if old_graph is not None:
        old_names.update(old_graph)
        old_names.update(item for values in old_graph.values() if values is not None for item in values)
    if current_graph is not None:
        new_names.update(current_graph)
        new_names.update(item for values in current_graph.values() if values is not None for item in values)

    if old_graph is not None and current_graph is not None:
        graph_proves_complete = graph_covers(old_graph, old_names) and graph_covers(current_graph, new_names)
    elif current_graph is not None:
        graph_proves_complete = graph_covers(current_graph, new_names)
    elif old_graph is not None:
        graph_proves_complete = graph_covers(old_graph, old_names)

    if dependency_graph_complete is False:
        graph_proves_complete = False
    if current_graph is not None and not graph_proves_complete:
        dependency_low = True
        incomplete_reason = "dependency_graph_incomplete"
    elif old_graph is not None and not graph_proves_complete:
        dependency_low = True
        incomplete_reason = "dependency_graph_incomplete"
    elif current_graph is None and old_graph is None:
        # No graph is safe only when every record explicitly says its
        # dependency set is complete (including an explicit empty set for leaf
        # functions).
        if names_tuple and any(record.dependency_complete is not True for record in (*old.values(), *new.values())):
            dependency_low = True
            incomplete_reason = "dependency_graph_missing"

    # A current-only graph cannot establish that an old caller edge was absent.
    # Removed functions are the clearest case, but a plain fingerprint record
    # also lacks an authoritative old dependency set.  The legacy
    # ``dependency_graph`` spelling remains compatible with the established
    # exact-graph contract; callers using ``current_dependency_graph``/
    # ``new_dependency_graph`` must supply the old graph (or explicit complete
    # record edges) before a selective decision is trusted.
    removed_names = set(old) - set(new)
    explicit_current_only = (
        current_dependency_graph is not None
        and old_graph is None
    )
    old_record_edges_complete = bool(old) and all(
        record.dependency_complete is True for record in old.values()
    )
    potential_source_change = bool(
        config_changed
        or generator_changed
        or set(old) != set(new)
        or any(
            old.get(name) is not None
            and new.get(name) is not None
            and (
                old[name].fingerprint != new[name].fingerprint
                or old[name].dependency_fingerprint != new[name].dependency_fingerprint
                or old[name].dependencies != new[name].dependencies
            )
            for name in set(old) & set(new)
        )
    )
    history_uncertain = bool(
        current_graph is not None
        and old_graph is None
        and (
            removed_names
            or (explicit_current_only and not old_record_edges_complete and potential_source_change)
        )
    )
    if history_uncertain:
        dependency_low = True
        incomplete_reason = "dependency_graph_history_incomplete"

    graph_edge_changes: set[str] = set()
    if old_graph is not None and current_graph is not None:
        for caller in sorted(set(old_graph) | set(current_graph)):
            old_edges = set(old_graph.get(caller) or ())
            new_edges = set(current_graph.get(caller) or ())
            if old_edges != new_edges:
                graph_edge_changes.add(caller)

    # A complete explicit graph is authoritative for dependency coverage, even
    # when compact function fingerprints are supplied as strings.  A low
    # confidence *fingerprint* itself remains conservative regardless of graph
    # completeness.
    low = bool(
        conservative
        or fingerprint_low
        or history_uncertain
        or (dependency_low and not graph_proves_complete)
    )
    changed: set[str] = set()
    reasons: dict[str, str] = {}
    for name in names_tuple:
        left, right = old.get(name), new.get(name)
        if left is None or right is None:
            changed.add(name)
            reasons[name] = "added_function" if left is None else "removed_function"
            continue
        if config_changed or generator_changed:
            changed.add(name)
            reasons[name] = "configuration_changed" if config_changed else "generator_changed"
        elif left.fingerprint != right.fingerprint:
            changed.add(name)
            reasons[name] = "function_fingerprint_changed"
        elif left.dependency_fingerprint != right.dependency_fingerprint:
            changed.add(name)
            reasons[name] = "dependency_fingerprint_changed"
        elif left.dependencies != right.dependencies:
            changed.add(name)
            reasons[name] = "dependency_edges_changed"

    for caller in graph_edge_changes:
        if caller in names:
            changed.add(caller)
            reasons.setdefault(caller, "dependency_edges_changed")

    if low:
        selected = set(names_tuple)
        for name in names_tuple:
            reasons.setdefault(name, incomplete_reason)
        if metrics is not None:
            metrics.increment("low_confidence_runs")
        dependency_affected: set[str] = set()
    else:
        reverse: dict[str, set[str]] = {name: set() for name in names_tuple}
        # A removed helper can still affect callers through an edge which is
        # present only in the old source.  Propagate over the old+new union.
        for graph in (old_graph, current_graph):
            if graph is None:
                continue
            for caller, callees in graph.items():
                if callees is None:
                    continue
                for callee in callees:
                    reverse.setdefault(str(callee), set()).add(str(caller))
        # Records carry precise helper edges when a separate graph was not
        # passed.  Keep both revisions so deletion propagation is conservative.
        for record_map in (old, new):
            for name, record in record_map.items():
                for dependency in record.dependencies:
                    reverse.setdefault(dependency, set()).add(name)
        dependency_affected = set()
        queue = list(sorted(changed))
        seen = set(queue)
        while queue:
            dependency = queue.pop(0)
            for caller in sorted(reverse.get(dependency, ())):
                if caller not in seen:
                    seen.add(caller)
                    dependency_affected.add(caller)
                    reasons.setdefault(caller, "dependency_affected")
                    queue.append(caller)
        selected = changed | dependency_affected

    selected &= set(names_tuple)
    skipped = set(names_tuple) - selected
    changed_tuple = tuple(sorted(changed))
    dependency_tuple = tuple(sorted(dependency_affected))
    selected_tuple = tuple(sorted(selected))
    skipped_tuple = tuple(sorted(skipped))
    if metrics is not None:
        metrics.increment("functions_changed", len(changed_tuple))
        metrics.increment("functions_dependency_affected", len(dependency_tuple))
        metrics.increment("functions_selected", len(selected_tuple))
        metrics.increment("functions_skipped", len(skipped_tuple))

    # ``names_tuple`` is the decision universe.  A complete NEW map is a
    # different projection: it must contain only functions that are active in
    # the current source.  An explicit ``target_functions`` value is the
    # authoritative active set.  Otherwise, when a current fingerprint map is
    # present, its records (plus current graph caller keys) define that set;
    # ``functions`` remains a decision-universe hint and must not resurrect a
    # deleted function merely because the accepted map still contains it.  If
    # no current input is supplied at all, retain the historical all-functions
    # behavior for older single-snapshot callers; an explicit empty mapping is
    # therefore the way to represent a source with no active functions.
    if target_functions is not None:
        target_names = {str(item) for item in target_functions}
    elif current is None:
        target_names = set(requested)
        if current_graph is not None:
            target_names.update(current_graph)
        if not target_names and current_graph is None:
            target_names.update(names_tuple)
    else:
        target_names = set(new)
        if current_graph is not None:
            # Graph keys are callers/definitions.  Graph values can refer to
            # external or removed helpers and must not by themselves become
            # active NEW functions.
            target_names.update(current_graph)
    target_names &= set(names_tuple)
    target_tuple = tuple(sorted(target_names))
    return RegenerationPlan(
        names_tuple,
        selected_tuple,
        skipped_tuple,
        changed_tuple,
        dependency_tuple,
        reasons,
        LOW_CONFIDENCE if low else HIGH_CONFIDENCE,
        bool(low),
        incomplete_reason if low else None,
        target_tuple,
    )


regeneration_plan = compute_regeneration_plan
plan_regeneration = compute_regeneration_plan


@dataclass(frozen=True)
class AlignmentResult:
    matches: tuple[tuple[int, int], ...]
    exact_matches: tuple[tuple[int, int], ...]
    aligned_matches: tuple[tuple[int, int], ...]
    unmatched_base: tuple[int, ...]
    unmatched_new: tuple[int, ...]
    duplicate_keys: tuple[Any, ...] = ()
    expensive_called: bool = False

    @property
    def unmatched_rows(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        return self.unmatched_base, self.unmatched_new

    @property
    def unmatched(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        return self.unmatched_rows


def _default_exact_key(item: Any) -> Any:
    meta = getattr(item, "meta", None)
    if meta is not None:
        for name in ("row_id", "case_name"):
            value = getattr(meta, name, None)
            if value:
                return (name, value)
        obligations = getattr(meta, "obligation_keys", ())
        if obligations:
            return ("obligations", tuple(sorted(obligations)))
    if isinstance(item, Mapping):
        for name in ("row_id", "case_name", "obligation_key", "obligation_keys"):
            if name in item and item[name] is not None:
                value = item[name]
                return (name, tuple(value) if name.endswith("keys") and not isinstance(value, str) else value)
    return ("value", _stable_payload(item))


def align_unmatched_rows(
    base_rows: Sequence[Any],
    new_rows: Sequence[Any],
    *,
    exact_key: Callable[[Any], Any] | None = None,
    key: Callable[[Any], Any] | None = None,
    aligner: Callable[[Sequence[Any], Sequence[Any]], Iterable[tuple[Any, Any]]] | None = None,
    alignment: Callable[[Sequence[Any], Sequence[Any]], Iterable[tuple[Any, Any]]] | None = None,
    metrics: PhaseMetrics | None = None,
    max_unmatched: int | None = None,
) -> AlignmentResult:
    """Exact-index rows first, then optionally align only unmatched subsets."""

    selector = exact_key or key or _default_exact_key
    base_index: dict[Any, list[int]] = {}
    new_index: dict[Any, list[int]] = {}
    for index, item in enumerate(base_rows):
        base_index.setdefault(selector(item), []).append(index)
    for index, item in enumerate(new_rows):
        new_index.setdefault(selector(item), []).append(index)
    exact: list[tuple[int, int]] = []
    duplicate: list[Any] = []
    used_base: set[int] = set()
    used_new: set[int] = set()
    for item_key in sorted(set(base_index) & set(new_index), key=lambda value: repr(value)):
        left, right = base_index[item_key], new_index[item_key]
        if len(left) != 1 or len(right) != 1:
            duplicate.append(item_key)
            continue
        exact.append((left[0], right[0]))
        used_base.add(left[0])
        used_new.add(right[0])
    unmatched_base = tuple(index for index in range(len(base_rows)) if index not in used_base)
    unmatched_new = tuple(index for index in range(len(new_rows)) if index not in used_new)
    if metrics is not None:
        metrics.increment("alignment_exact_matches", len(exact))
        metrics.increment("alignment_unmatched_base", len(unmatched_base))
        metrics.increment("alignment_unmatched_new", len(unmatched_new))
    expensive = aligner or alignment
    aligned: list[tuple[int, int]] = []
    called = False
    if expensive is not None and unmatched_base and unmatched_new and (
        max_unmatched is None or max(len(unmatched_base), len(unmatched_new)) <= max_unmatched
    ):
        called = True
        if metrics is not None:
            metrics.increment("alignment_calls")
        subset_base = tuple(base_rows[index] for index in unmatched_base)
        subset_new = tuple(new_rows[index] for index in unmatched_new)
        if metrics is None:
            result = expensive(subset_base, subset_new)
        else:
            with metrics.phase("alignment"):
                result = expensive(subset_base, subset_new)
        for pair in result or ():
            try:
                left, right = pair
            except (TypeError, ValueError):
                continue
            if isinstance(left, int) and isinstance(right, int):
                if 0 <= left < len(unmatched_base) and 0 <= right < len(unmatched_new):
                    candidate = (unmatched_base[left], unmatched_new[right])
                else:
                    continue
            else:
                try:
                    candidate = (unmatched_base[subset_base.index(left)], unmatched_new[subset_new.index(right)])
                except ValueError:
                    continue
            if candidate[0] not in used_base and candidate[1] not in used_new:
                aligned.append(candidate)
                used_base.add(candidate[0])
                used_new.add(candidate[1])
        if metrics is not None:
            metrics.increment("alignment_pairs", len(aligned))
    remaining_base = tuple(index for index in range(len(base_rows)) if index not in used_base)
    remaining_new = tuple(index for index in range(len(new_rows)) if index not in used_new)
    exact.sort()
    aligned.sort()
    return AlignmentResult(tuple(exact + aligned), tuple(exact), tuple(aligned), remaining_base, remaining_new, tuple(duplicate), called)


align_rows = align_unmatched_rows
exact_then_align = align_unmatched_rows
align_unmatched = align_unmatched_rows


@dataclass(frozen=True)
class WorkbookScan:
    used_cells: int
    sheets: int
    cells_by_sheet: Mapping[str, int]

    @property
    def cell_count(self) -> int:
        return self.used_cells

    def to_dict(self) -> dict[str, Any]:
        return {"used_cells": self.used_cells, "sheets": self.sheets, "cells_by_sheet": dict(sorted(self.cells_by_sheet.items()))}


def _workbook_sheets(workbook: Any) -> tuple[tuple[str, Any], ...]:
    if isinstance(workbook, Mapping):
        return tuple((str(key), value) for key, value in workbook.items())
    worksheets = getattr(workbook, "worksheets", None)
    if worksheets is not None:
        result = []
        for index, sheet in enumerate(worksheets):
            result.append((str(getattr(sheet, "title", index)), sheet))
        return tuple(result)
    return ((str(getattr(workbook, "title", "sheet")), workbook),)


def iter_used_cells(workbook: Any) -> Iterator[tuple[str, Any]]:
    """Yield physical cells once, without allocating a max-row/max-column grid."""

    for sheet_name, sheet in _workbook_sheets(workbook):
        # The adapter's public PhysicalWorksheet contract is preferred.  A
        # compatible worksheet may expose either a physical mapping or an
        # iterator; neither path asks the object to materialize a rectangle.
        cells = getattr(sheet, "physical_cells", None)
        if isinstance(cells, Mapping):
            for cell in cells.values():
                yield sheet_name, cell
            continue
        iterator = getattr(sheet, "iter_used_cells", None)
        if callable(iterator):
            for cell in iterator():
                yield sheet_name, cell
            continue
        # Direct openpyxl callers are converted once at this boundary by the
        # adapter compatibility helper.  The rest of this scan consumes only
        # the project-owned public protocol.  A rectangular ``iter_rows()``
        # fallback is deliberately never attempted: a sparse sheet can
        # advertise a huge dimension while containing only a handful of cells.
        try:
            from .adapters.tessy_workbook import physicalize_worksheet

            physical_sheet = physicalize_worksheet(sheet)
        except Exception as exc:
            raise SelectiveRegenerationError(
                f"sheet {sheet_name!r} does not expose the project physical-cell protocol"
            ) from exc
        for cell in physical_sheet.iter_used_cells():
            yield sheet_name, cell


scan_used_cells = iter_used_cells


def scan_workbook(workbook: Any, *, metrics: PhaseMetrics | None = None) -> WorkbookScan:
    counts: dict[str, int] = {}
    total = 0
    if metrics is None:
        cells = iter_used_cells(workbook)
        for sheet_name, _cell in cells:
            counts[sheet_name] = counts.get(sheet_name, 0) + 1
            total += 1
    else:
        with metrics.phase("parsing"):
            for sheet_name, _cell in iter_used_cells(workbook):
                counts[sheet_name] = counts.get(sheet_name, 0) + 1
                total += 1
    if metrics is not None:
        metrics.increment("workbook_used_cells", total)
        metrics.increment("workbook_sheets", len(counts))
    return WorkbookScan(total, len(counts), counts)


def count_used_cells(workbook: Any) -> int:
    return scan_workbook(workbook).used_cells


workbook_used_cells = count_used_cells
linear_workbook_scan = scan_workbook
used_cell_scan = scan_workbook


@dataclass(frozen=True)
class FunctionRegeneration:
    function_key: str
    status: str
    candidate: Any = None
    reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RegenerationResult:
    plan: RegenerationPlan
    candidate: Mapping[str, Any] | None
    records: tuple[FunctionRegeneration, ...]
    conflicts: tuple[RegenerationConflict, ...]
    accepted: bool
    metrics: PhaseMetrics

    @property
    def blocked(self) -> bool:
        return not self.accepted

    @property
    def committed(self) -> bool:
        return self.accepted

    @property
    def can_emit(self) -> bool:
        return self.accepted and self.candidate is not None

    @property
    def outputs(self) -> Mapping[str, Any] | None:
        return self.candidate

    @property
    def conflict_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.conflicts)

    @property
    def timed_out_functions(self) -> tuple[str, ...]:
        return tuple(item.function_key for item in self.records if item.status == "timeout")

    @property
    def skipped_functions(self) -> tuple[str, ...]:
        return tuple(item.function_key for item in self.records if item.status == "skipped")

    @property
    def regenerated_functions(self) -> tuple[str, ...]:
        return tuple(item.function_key for item in self.records if item.status == "generated")

    @property
    def skipped(self) -> tuple[str, ...]:
        return self.skipped_functions

    @property
    def regenerated(self) -> tuple[str, ...]:
        return self.regenerated_functions


@dataclass(frozen=True)
class NewMapResult:
    """Complete NEW function map prepared for a Merge B service call.

    ``new_map`` is present only when every function has either a freshly built
    NEW snapshot or an explicitly supplied accepted snapshot to retain.  A
    missing accepted entry is a blocking error; there is intentionally no
    current-payload fallback for unchanged functions.
    """

    plan: RegenerationPlan
    new_map: Mapping[str, Any] | None
    records: tuple[FunctionRegeneration, ...]
    conflicts: tuple[RegenerationConflict, ...]
    alignments: Mapping[str, AlignmentResult]
    accepted: bool
    metrics: PhaseMetrics

    @property
    def candidate(self) -> Mapping[str, Any] | None:
        return self.new_map

    @property
    def outputs(self) -> Mapping[str, Any] | None:
        return self.new_map

    @property
    def blocked(self) -> bool:
        return not self.accepted

    @property
    def can_emit(self) -> bool:
        return self.accepted and self.new_map is not None

    @property
    def conflict_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.conflicts)

    def __getitem__(self, function_key: str) -> Any:
        if self.new_map is None:
            raise KeyError(function_key)
        return self.new_map[function_key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.new_map or {})

    def __len__(self) -> int:
        return len(self.new_map or {})

    def as_dict(self) -> dict[str, Any]:
        return dict(self.new_map or {})

    @property
    def skipped_functions(self) -> tuple[str, ...]:
        return tuple(item.function_key for item in self.records if item.status == "skipped")

    @property
    def regenerated_functions(self) -> tuple[str, ...]:
        return tuple(item.function_key for item in self.records if item.status == "generated")


@dataclass(frozen=True)
class SelectiveMergeOutcome:
    """Result of building a complete map and invoking MergeBService."""

    build: NewMapResult
    service_outcome: Any | None
    conflicts: tuple[RegenerationConflict, ...]
    accepted: bool

    @property
    def committed(self) -> bool:
        return self.accepted

    @property
    def blocked(self) -> bool:
        return not self.accepted

    @property
    def new_map(self) -> Mapping[str, Any] | None:
        return self.build.new_map

    @property
    def metrics(self) -> PhaseMetrics:
        return self.build.metrics

    @property
    def conflict_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.conflicts)


def _invoke(callback: Callable[..., Any], function_key: str, payload: Any, *, stage: str) -> Any:
    """Call common one- or two-argument pipeline callback forms."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(function_key, payload)
    positional = [
        item for item in signature.parameters.values()
        if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)
    ]
    if any(item.kind == item.VAR_POSITIONAL for item in signature.parameters.values()):
        return callback(function_key, payload)
    required = sum(item.default is inspect.Parameter.empty for item in positional)
    if len(positional) >= 2 and required <= 2:
        return callback(function_key, payload)
    if len(positional) == 1:
        parameter = positional[0].name.casefold()
        if stage != "generate" and parameter in {"item", "payload", "candidate", "result", "output", "row", "analysis", "function_obj"}:
            return callback(payload)
        return callback(function_key)
    if not positional:
        return callback()
    return callback(function_key, payload)


def _factory_requires_payload(callback: Callable[..., Any]) -> bool:
    """Whether a factory has a required second positional argument.

    Added functions have no accepted BASE/WORKING state to use as the second
    callback argument.  Introspecting the callable lets the selective layer
    invoke a one-argument factory normally, while refusing to fabricate a
    ``None`` payload for a required two-argument factory.
    """

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        # Opaque callables are conservatively treated as requiring the normal
        # two-argument contract.  Callers can provide ``new_inputs`` to make
        # that contract explicit.
        return True
    positional = [
        item for item in signature.parameters.values()
        if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)
    ]
    required = sum(item.default is inspect.Parameter.empty for item in positional)
    return len(positional) >= 2 and required >= 2


def _invoke_new_factory(
    callback: Callable[..., Any],
    function_key: str,
    payload: Any,
    *,
    payload_present: bool,
    added: bool,
) -> Any:
    """Invoke a NEW factory without passing a fake accepted payload.

    For an added function with no accepted state, a required two-argument
    factory must receive an explicit ``new_inputs`` value.  A one-argument
    factory remains valid and receives only the function key.  Existing
    selected functions keep the historical callback behavior, using their
    accepted state when no explicit input was supplied.
    """

    if added and not payload_present and _factory_requires_payload(callback):
        raise SelectiveRegenerationError(
            "added function requires an explicit NEW input for a two-argument snapshot factory"
        )
    if added and not payload_present:
        # Calling through _invoke with ``None`` would still pass a fabricated
        # second argument to a callback whose second parameter is optional.
        # A one-argument factory has a clear, stable contract: the function
        # key identifies what it should generate.
        try:
            signature = inspect.signature(callback)
            positional = [
                item for item in signature.parameters.values()
                if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)
            ]
            if len(positional) <= 1 and not any(
                item.kind == item.VAR_POSITIONAL for item in signature.parameters.values()
            ):
                return callback(function_key)
        except (TypeError, ValueError):
            # The arity guard above already rejects opaque callables when no
            # payload is present; keep this branch defensive for unusual
            # callable signatures.
            pass
        return callback(function_key)
    return _invoke(callback, function_key, payload, stage="generate")


def _mapping_value(mapping: Mapping[str, Any] | None, function_key: str) -> tuple[bool, Any]:
    if mapping is None or function_key not in mapping:
        return False, None
    value = mapping[function_key]
    return value is not None, value


def _accepted_state_value(states: Mapping[str, Any] | None, function_key: str) -> tuple[bool, Any]:
    """Project an accepted FunctionIterationState-like record for reuse.

    The selective layer does not serialize or mutate state.  It only carries
    the accepted working object through for an unchanged function, allowing a
    real MergeBService orchestration to retain its state/identity lineage.
    """

    if states is None or function_key not in states:
        return False, None
    state = states[function_key]
    working = getattr(state, "working", None)
    if working is not None:
        return True, working
    return state is not None, state


def _snapshot_rows(value: Any) -> tuple[Any, ...] | None:
    """Extract logical rows from BaseSnapshot/WorkingSnapshot-like values."""

    # A FunctionIterationState contains both BASE (pure generated NEW lineage)
    # and WORKING (manual/user state).  Selective NEW construction must inspect
    # BASE for identity/alignment; WORKING is never silently promoted to NEW.
    base = getattr(value, "base", None)
    rows = getattr(base, "rows", None)
    if rows is not None and not isinstance(rows, (str, bytes, Mapping)):
        try:
            return tuple(rows)
        except TypeError:
            return None
    rows = getattr(value, "rows", None)
    if rows is not None and not isinstance(rows, (str, bytes, Mapping)):
        try:
            return tuple(rows)
        except TypeError:
            return None
    working = getattr(value, "working", None)
    rows = getattr(working, "rows", None)
    if rows is not None and not isinstance(rows, (str, bytes, Mapping)):
        try:
            return tuple(rows)
        except TypeError:
            return None
    return None


def _pure_new_value(value: Any) -> Any:
    """Project an accepted state to its immutable pure-NEW/BASE value."""

    base = getattr(value, "base", None)
    return base if base is not None else value


def _plan_target_functions(plan: RegenerationPlan) -> tuple[str, ...]:
    """Return the active NEW target for a plan, with old-plan compatibility."""

    target = getattr(plan, "target_functions", None)
    if target is None:
        # Plans created by pre-PR14 callers have no target field.  Their
        # complete function universe was also their generation target.
        return tuple(plan.all_functions)
    return tuple(str(item) for item in target)


def _analysis_id(value: Any) -> str | None:
    candidate = getattr(value, "analysis_id", None)
    return candidate if isinstance(candidate, str) and candidate else None


def _rebind_analysis(value: Any, analysis_id: str | None) -> Any:
    """Rebind a pure snapshot to the NEW analysis ID without mutating it."""

    if not analysis_id or _analysis_id(value) in {None, analysis_id}:
        return value
    if is_dataclass(value) and any(item.name == "analysis_id" for item in dataclass_fields(value)):
        try:
            return replace(value, analysis_id=analysis_id)
        except (TypeError, ValueError):
            return value
    return value


def _merge_trusted_service_metrics(metrics: PhaseMetrics, service_result: Any) -> bool:
    """Merge only an explicitly typed service phase report.

    ``MergeBService.merge`` is an opaque orchestration call from this layer.
    Its wall time belongs to ``service_call``; the required ``commit`` phase
    is populated only when the service returns a real :class:`PhaseMetrics`
    object describing its internal phases.
    """

    if service_result is None:
        return False
    for name in ("phase_metrics", "metrics"):
        reported = getattr(service_result, name, None)
        if isinstance(reported, PhaseMetrics) and reported is not metrics:
            metrics.merge(reported)
            return True
    return False


class SelectiveRegenerator:
    """Plan and execute exact selective function regeneration."""

    def __init__(
        self,
        *,
        fingerprint_cache: FingerprintCache | None = None,
        cache: FingerprintCache | None = None,
        metrics: PhaseMetrics | None = None,
        clock: Callable[[], float] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.cache = fingerprint_cache or cache or FingerprintCache()
        self.metrics = metrics or PhaseMetrics(clock=clock)
        self.clock = clock or time.perf_counter
        self.timeout_seconds = timeout_seconds

    def plan(self, previous: Any = None, current: Any = None, **kwargs: Any) -> RegenerationPlan:
        kwargs.setdefault("metrics", self.metrics)
        with self.metrics.phase("selection"):
            return compute_regeneration_plan(previous, current, **kwargs)

    build_plan = plan
    select = plan

    def build_new_map(
        self,
        accepted: Mapping[str, Any] | None,
        plan: RegenerationPlan | None = None,
        new_snapshot_factory: Callable[..., Any] | None = None,
        *,
        previous: Any = None,
        current: Any = None,
        functions: Mapping[str, Any] | None = None,
        new_inputs: Mapping[str, Any] | None = None,
        factory: Callable[..., Any] | None = None,
        snapshot_factory: Callable[..., Any] | None = None,
        new_analysis_id: str | None = None,
        analysis_id: str | None = None,
        alignment_callback: Callable[[Sequence[Any], Sequence[Any]], Iterable[tuple[Any, Any]]] | None = None,
        aligner: Callable[[Sequence[Any], Sequence[Any]], Iterable[tuple[Any, Any]]] | None = None,
        exact_key: Callable[[Any], Any] | None = None,
        dependency_graph: Mapping[str, Iterable[str]] | None = None,
        timeout_seconds: float | None = None,
        deadline: float | None = None,
        metrics: PhaseMetrics | None = None,
        **kwargs: Any,
    ) -> NewMapResult:
        """Build a complete NEW map for MergeBService.

        ``accepted`` is mandatory for every active function in the plan.
        Historical functions that are absent from the current/NEW target may
        remain in that map for complete Merge B removal classification, but
        they are never passed to ``new_snapshot_factory`` or emitted.  Selected
        active functions are passed to the factory; skipped active functions
        are copied from ``accepted``.  Any missing active output, timeout, or
        factory failure blocks the map and prevents a service call.
        """

        metrics = metrics or self.metrics
        if not isinstance(accepted, Mapping):
            # A repository's AcceptedBundle is a valid source for the required
            # map, but arbitrary/implicit values are not.  This projection is
            # read-only and still enforces an entry for every planned function.
            states = getattr(accepted, "function_states", None)
            if states is not None and not isinstance(states, (str, bytes, Mapping)):
                try:
                    accepted = {
                        str(getattr(state, "function_key", getattr(state, "function", ""))): state
                        for state in states
                        if getattr(state, "function_key", getattr(state, "function", ""))
                    }
                except TypeError:
                    accepted = None
        if not isinstance(accepted, Mapping):
            empty_plan = plan or self.plan(previous, current, functions=())
            conflict = RegenerationConflict(
                "MISSING_ACCEPTED_FUNCTION_MAP",
                "module",
                "selective Merge B requires the accepted function-state/output map",
            )
            return NewMapResult(empty_plan, None, (), (conflict,), {}, False, metrics)
        accepted = {str(key): value for key, value in accepted.items()}
        if plan is None:
            plan = self.plan(
                previous,
                current,
                functions=tuple(str(key) for key in accepted),
                dependency_graph=dependency_graph,
                **kwargs,
            )
        plan_keys = set(plan.all_functions)
        target_keys = set(_plan_target_functions(plan))
        # The accepted map may legitimately still contain historical function
        # states which are absent from NEW.  They are part of the decision
        # universe and are consumed by MergeBService's complete-map removal
        # policy, but they must not be emitted as NEW entries.  Only keys
        # outside that universe are invalid extras.
        extra_accepted = tuple(sorted(set(accepted) - plan_keys))
        if extra_accepted:
            conflict = RegenerationConflict(
                "COMPLETE_MAP_MISMATCH",
                "module",
                "accepted function-state/output map contains keys outside plan.all_functions: "
                f"{extra_accepted!r}",
            )
            return NewMapResult(plan, None, (), (conflict,), {}, False, metrics)
        callback = new_snapshot_factory or factory or snapshot_factory
        raw_inputs = new_inputs if new_inputs is not None else functions
        input_map = {str(key): value for key, value in (raw_inputs or {}).items()}
        target_functions = tuple(sorted(target_keys))
        added_functions = {
            function_key
            for function_key in target_functions
            if plan.is_selected(function_key)
            and plan.reason_for(function_key) == "added_function"
        }
        # A complete accepted map is checked before invoking any expensive
        # factory, so a partially built map can never leak into Merge B.  An
        # explicitly classified added function is the sole exception to the
        # accepted-entry requirement: it has no historical state by design and
        # may be built from a NEW input payload (or a one-argument factory).
        conflicts: list[RegenerationConflict] = []
        for function_key in target_functions:
            if function_key in added_functions:
                continue
            if function_key not in accepted or accepted[function_key] is None:
                conflicts.append(
                    RegenerationConflict(
                        "MISSING_ACCEPTED_OUTPUT",
                        function_key,
                        "accepted function-state/output map has no entry",
                    )
                )
        if conflicts:
            return NewMapResult(plan, None, (), tuple(conflicts), {}, False, metrics)

        end = deadline
        limit = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        if end is None and limit is not None:
            end = self.clock() + max(0.0, float(limit))
        records: list[FunctionRegeneration] = []
        # The outgoing map is pure NEW input.  For accepted state records this
        # means carrying BASE forward for skipped functions, never WORKING.
        result_map: dict[str, Any] = {
            function_key: _pure_new_value(accepted[function_key])
            for function_key in target_functions
            if function_key in accepted and accepted[function_key] is not None
        }
        target_analysis_id = new_analysis_id if new_analysis_id is not None else analysis_id
        selected_fresh_keys: set[str] = set()
        alignments: dict[str, AlignmentResult] = {}
        for function_key in target_functions:
            accepted_value = accepted.get(function_key)
            if not plan.is_selected(function_key):
                retained_value = _pure_new_value(accepted_value)
                records.append(FunctionRegeneration(function_key, "skipped", retained_value, "unchanged"))
                continue
            if end is not None and self.clock() >= end:
                message = "regeneration deadline expired before NEW snapshot factory"
                conflicts.append(RegenerationConflict("REGENERATION_TIMEOUT", function_key, message, previous=accepted_value))
                records.append(FunctionRegeneration(function_key, "timeout", None, plan.reason_for(function_key), message))
                continue
            try:
                if callback is None:
                    if function_key not in input_map or input_map[function_key] is None:
                        raise SelectiveRegenerationError("selected function has no NEW snapshot factory/input")
                    fresh = input_map[function_key]
                else:
                    metrics.increment("generator_calls")
                    with metrics.phase("generation"):
                        # Two-argument factories receive a source/new-input
                        # payload when supplied, while one-argument factories
                        # continue to receive the function key.  Accepted state
                        # remains available as the fallback payload so callers
                        # can derive stable identities for a regenerated row.
                        has_explicit_input = function_key in input_map and input_map[function_key] is not None
                        factory_payload = input_map[function_key] if has_explicit_input else accepted_value
                        fresh = _invoke_new_factory(
                            callback,
                            function_key,
                            factory_payload,
                            payload_present=has_explicit_input,
                            added=function_key in added_functions,
                        )
                if fresh is None:
                    raise SelectiveRegenerationError("NEW snapshot factory returned no snapshot")
                fresh_analysis_id = _analysis_id(fresh)
                if isinstance(fresh, BaseSnapshot) and fresh_analysis_id is None:
                    message = "selected BaseSnapshot has no analysis_id"
                    conflicts.append(
                        RegenerationConflict(
                            "ANALYSIS_ID_MISMATCH",
                            function_key,
                            message,
                            previous=accepted_value,
                            candidate=fresh,
                        )
                    )
                    records.append(
                        FunctionRegeneration(function_key, "failed", None, plan.reason_for(function_key), message)
                    )
                    continue
                if fresh_analysis_id is not None:
                    if target_analysis_id is None:
                        target_analysis_id = fresh_analysis_id
                    elif fresh_analysis_id != target_analysis_id:
                        message = (
                            f"selected NEW snapshot analysis_id {fresh_analysis_id!r} "
                            f"does not match target {target_analysis_id!r}"
                        )
                        conflicts.append(
                            RegenerationConflict(
                                "ANALYSIS_ID_MISMATCH",
                                function_key,
                                message,
                                previous=accepted_value,
                                candidate=fresh,
                            )
                        )
                        records.append(
                            FunctionRegeneration(function_key, "failed", None, plan.reason_for(function_key), message)
                        )
                        continue
                selected_fresh_keys.add(function_key)
                # The alignment helper is part of the production selective path:
                # exact indexes are constructed for each selected snapshot, and
                # an expensive callback (if supplied) sees only unmatched rows.
                accepted_rows = _snapshot_rows(accepted_value)
                fresh_rows = _snapshot_rows(fresh)
                if accepted_rows is not None and fresh_rows is not None:
                    with metrics.phase("matching"):
                        alignment_result = align_unmatched_rows(
                            accepted_rows,
                            fresh_rows,
                            exact_key=exact_key,
                            aligner=alignment_callback or aligner,
                            metrics=metrics,
                        )
                    alignments[function_key] = alignment_result
                if end is not None and self.clock() >= end:
                    raise RegenerationTimeout("regeneration deadline expired after NEW snapshot factory")
                result_map[function_key] = fresh
                records.append(FunctionRegeneration(function_key, "generated", fresh, plan.reason_for(function_key)))
            except (RegenerationTimeout, TimeoutError) as exc:
                message = str(exc) or "regeneration timed out"
                conflicts.append(RegenerationConflict("REGENERATION_TIMEOUT", function_key, message, previous=accepted_value))
                records.append(FunctionRegeneration(function_key, "timeout", None, plan.reason_for(function_key), message))
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                conflicts.append(RegenerationConflict("REGENERATION_FAILED", function_key, message, previous=accepted_value))
                records.append(FunctionRegeneration(function_key, "failed", None, plan.reason_for(function_key), message))
        if target_analysis_id and plan.confidence == HIGH_CONFIDENCE and not plan.conservative:
            # Only unchanged accepted BASE projections may be rebound to a
            # verified target analysis.  Selected fresh snapshots were checked
            # above and must retain their exact factory-supplied analysis_id.
            result_map = {
                key: (
                    _rebind_analysis(value, target_analysis_id)
                    if key in plan.skipped_functions and key not in selected_fresh_keys
                    else value
                )
                for key, value in result_map.items()
            }
        accepted_map = not conflicts and set(result_map) == target_keys
        if not conflicts and set(result_map) != target_keys:
            accepted_map = False
            conflicts.append(
                RegenerationConflict(
                    "COMPLETE_MAP_MISMATCH",
                    "module",
                    "NEW map keys must exactly equal the current/NEW active function set",
                )
            )
        # A map-completeness check is the actual validation performed by this
        # layer.  Count it only when the check runs; blocked factory results do
        # not receive a fictitious verification call.
        if accepted_map:
            with metrics.phase("verification"):
                metrics.increment("validation_calls")
                if any(result_map.get(key) is None for key in target_keys):
                    accepted_map = False
                    conflicts.append(RegenerationConflict("MISSING_NEW_SNAPSHOT", "module", "NEW map contains a missing snapshot"))
                    result_map = {}
        if not accepted_map and not conflicts:
            conflicts.append(
                RegenerationConflict(
                    "COMPLETE_MAP_MISMATCH",
                    "module",
                    "NEW map was not accepted as a complete plan map",
                )
            )
        if not accepted_map:
            result_map = {}
        return NewMapResult(
            plan,
            result_map if accepted_map else None,
            tuple(records),
            tuple(conflicts),
            alignments,
            accepted_map,
            metrics,
        )

    def merge_with_service(
        self,
        service: Any,
        module_key: str,
        accepted: Mapping[str, Any] | None,
        plan: RegenerationPlan | None = None,
        new_snapshot_factory: Callable[..., Any] | None = None,
        *,
        complete: bool | None = None,
        service_kwargs: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> SelectiveMergeOutcome:
        """Prepare a complete map and invoke a real MergeBService exactly once.

        This entry point owns module-map orchestration, so ``complete`` is
        always true at the service boundary.  An explicit false value is a
        caller contract violation and fails closed before a factory or service
        callback can run.
        """

        call_kwargs = dict(service_kwargs or {})
        requested_complete = complete
        supplied_complete = call_kwargs.pop("complete", None)
        if supplied_complete is not None:
            if requested_complete is not None and bool(requested_complete) != bool(supplied_complete):
                requested_complete = False
            else:
                requested_complete = bool(supplied_complete)
        if requested_complete is False:
            metrics = kwargs.get("metrics") or self.metrics
            if plan is None:
                raw_functions = kwargs.get("functions")
                if isinstance(raw_functions, Mapping):
                    function_names = tuple(str(key) for key in raw_functions)
                elif isinstance(accepted, Mapping):
                    function_names = tuple(str(key) for key in accepted)
                else:
                    function_names = ()
                plan = self.plan(
                    kwargs.get("previous"),
                    kwargs.get("current"),
                    functions=function_names,
                    dependency_graph=kwargs.get("dependency_graph"),
                    metrics=metrics,
                )
            conflict = RegenerationConflict(
                "COMPLETE_MAP_REQUIRED",
                module_key,
                "selective Merge B orchestration requires complete=True",
            )
            build = NewMapResult(plan, None, (), (conflict,), {}, False, metrics)
            return SelectiveMergeOutcome(build, None, (conflict,), False)

        build = self.build_new_map(
            accepted,
            plan,
            new_snapshot_factory,
            **kwargs,
        )
        if not build.accepted or build.new_map is None:
            return SelectiveMergeOutcome(build, None, build.conflicts, False)
        # ``build_new_map`` has already checked every accepted function and
        # returns a complete module map.  Do not let MergeBService infer map
        # completeness from the concrete input type.
        call_kwargs["complete"] = True
        try:
            # The service owns transaction/commit internals.  Measure this
            # boundary as an opaque service call rather than mislabelling all
            # of its work as this layer's pure commit phase.
            with build.metrics.phase("service_call"):
                service_result = service.merge(module_key, build.new_map, **call_kwargs)
        except Exception as exc:
            conflict = RegenerationConflict(
                "MERGE_B_SERVICE_FAILED",
                module_key,
                str(exc) or exc.__class__.__name__,
            )
            return SelectiveMergeOutcome(build, None, tuple((*build.conflicts, conflict)), False)
        _merge_trusted_service_metrics(build.metrics, service_result)
        service_conflicts = tuple(getattr(service_result, "conflicts", ()) or ())
        committed_present = service_result is not None and hasattr(service_result, "committed")
        committed = getattr(service_result, "committed", None) if committed_present else None
        accepted_result = committed is True
        conflicts_list = [*build.conflicts, *service_conflicts]
        if committed is not True:
            conflicts_list.append(
                RegenerationConflict(
                    "MERGE_B_SERVICE_UNCONFIRMED",
                    module_key,
                    "MergeBService did not return committed=True",
                )
            )
        conflicts = tuple(conflicts_list)
        # A committed service may carry non-blocking diagnostics.  They must
        # remain visible without turning an already published candidate into
        # a falsely reported failure; only blocking diagnostics invalidate the
        # selective acceptance result.
        if any(getattr(item, "severity", "blocking") == "blocking" for item in conflicts):
            accepted_result = False
        return SelectiveMergeOutcome(build, service_result, conflicts, accepted_result)

    def regenerate(
        self,
        previous: Any = None,
        current: Any = None,
        *,
        plan: RegenerationPlan | None = None,
        functions: Mapping[str, Any] | Iterable[Any] | None = None,
        generate: Callable[..., Any] | None = None,
        generator: Callable[..., Any] | None = None,
        pipeline: Callable[..., Any] | None = None,
        assembler: Callable[..., Any] | None = None,
        coverage: Callable[..., Any] | None = None,
        verifier: Callable[..., Any] | None = None,
        previous_outputs: Mapping[str, Any] | None = None,
        accepted_outputs: Mapping[str, Any] | None = None,
        accepted_states: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
        deadline: float | None = None,
        dependency_graph: Mapping[str, Iterable[str]] | None = None,
        metrics: PhaseMetrics | None = None,
        **kwargs: Any,
    ) -> RegenerationResult:
        metrics = metrics or self.metrics
        if plan is None:
            plan = self.plan(previous, current, dependency_graph=dependency_graph, **kwargs)
        if functions is None:
            functions_map: dict[str, Any] = {name: name for name in plan.all_functions}
        elif isinstance(functions, Mapping):
            functions_map = {str(key): value for key, value in functions.items()}
        else:
            functions_map = {}
            for item in functions:
                name = _fingerprint_name(item)
                if name:
                    functions_map[name] = item
        # ``previous_outputs`` is retained as a compatibility spelling.  An
        # unchanged function is reusable only when this accepted projection (or
        # an accepted state record) is actually supplied.
        if accepted_outputs is not None:
            previous_outputs = accepted_outputs
        previous_outputs = dict(previous_outputs or {})
        callback = generate or generator or pipeline
        if callback is None and assembler is None and coverage is None and verifier is None:
            callback = lambda function_key: functions_map.get(function_key)
        limit = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        end = deadline
        if end is None and limit is not None:
            end = self.clock() + max(0.0, float(limit))
        records: list[FunctionRegeneration] = []
        conflicts: list[RegenerationConflict] = []
        outputs: dict[str, Any] = {}
        target_functions = _plan_target_functions(plan)
        target_keys = set(target_functions)
        for function_key in target_functions:
            payload = functions_map.get(function_key)
            if not plan.is_selected(function_key):
                present, accepted_value = _mapping_value(previous_outputs, function_key)
                if not present:
                    present, accepted_value = _accepted_state_value(accepted_states, function_key)
                if not present:
                    message = "unchanged function has no accepted output/state to reuse"
                    conflicts.append(RegenerationConflict("MISSING_ACCEPTED_OUTPUT", function_key, message))
                    records.append(FunctionRegeneration(function_key, "blocked", None, "unchanged", message))
                    continue
                outputs[function_key] = accepted_value
                records.append(FunctionRegeneration(function_key, "skipped", accepted_value, "unchanged"))
                continue
            if end is not None and self.clock() >= end:
                error = "regeneration deadline expired before pipeline invocation"
                conflicts.append(RegenerationConflict("REGENERATION_TIMEOUT", function_key, error, previous=previous_outputs.get(function_key)))
                records.append(FunctionRegeneration(function_key, "timeout", None, plan.reason_for(function_key), error))
                continue
            try:
                result = payload
                for stage_name, stage_callback in (("row_assembler", assembler), ("coverage", coverage), ("verifier", verifier)):
                    if stage_callback is None:
                        continue
                    metrics.increment(f"{stage_name}_calls")
                    stage_input = result
                    phase_name = "verification" if stage_name == "verifier" else stage_name
                    with metrics.phase(phase_name):
                        stage_result = _invoke(stage_callback, function_key, stage_input, stage=stage_name)
                    if stage_name == "verifier":
                        if stage_result is False:
                            raise SelectiveRegenerationError("verifier rejected regenerated function")
                        if stage_result is None or stage_result is True:
                            # Verifiers commonly return a boolean or no value;
                            # they validate the preceding candidate rather than
                            # replacing it.
                            result = stage_input
                        else:
                            result = stage_result
                    else:
                        result = stage_result
                    if end is not None and self.clock() >= end:
                        raise RegenerationTimeout("regeneration deadline expired during pipeline")
                if callback is not None:
                    metrics.increment("generator_calls")
                    with metrics.phase("generation"):
                        result = _invoke(callback, function_key, result, stage="generate")
                if end is not None and self.clock() >= end:
                    raise RegenerationTimeout("regeneration deadline expired after pipeline")
                if result is None:
                    raise SelectiveRegenerationError(
                        "selected regeneration pipeline returned no output"
                    )
                outputs[function_key] = result
                records.append(FunctionRegeneration(function_key, "generated", result, plan.reason_for(function_key)))
            except (RegenerationTimeout, TimeoutError) as exc:
                message = str(exc) or "regeneration timed out"
                conflicts.append(RegenerationConflict("REGENERATION_TIMEOUT", function_key, message, previous=previous_outputs.get(function_key)))
                records.append(FunctionRegeneration(function_key, "timeout", None, plan.reason_for(function_key), message))
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                conflicts.append(RegenerationConflict("REGENERATION_FAILED", function_key, message, previous=previous_outputs.get(function_key)))
                records.append(FunctionRegeneration(function_key, "failed", None, plan.reason_for(function_key), message))
        # A selective result is a NEW target map.  Historical/deleted names
        # may still be present in the decision universe for diagnostics, but
        # they are not candidates for generation or emission.
        if not conflicts and set(outputs) != target_keys:
            conflicts.append(
                RegenerationConflict(
                    "COMPLETE_MAP_MISMATCH",
                    "module",
                    "regeneration output keys must exactly equal the current/NEW active function set",
                )
            )
        accepted = not conflicts
        # A timeout/failure is explicitly fail-closed: neither the previous output
        # nor a partial map is exposed as an accepted candidate.
        candidate = dict(outputs) if accepted else None
        return RegenerationResult(plan, candidate, tuple(records), tuple(conflicts), accepted, metrics)

    run = regenerate
    execute = regenerate


SelectiveRegeneration = SelectiveRegenerator
SelectiveRegenerationPlanner = SelectiveRegenerator


def selective_regenerate(*args: Any, **kwargs: Any) -> RegenerationResult:
    return SelectiveRegenerator(
        fingerprint_cache=kwargs.pop("fingerprint_cache", None),
        metrics=kwargs.pop("metrics", None),
        clock=kwargs.pop("clock", None),
        timeout_seconds=kwargs.pop("timeout_seconds", None),
    ).regenerate(*args, **kwargs)


def build_new_map(
    accepted: Mapping[str, Any] | None,
    *args: Any,
    regenerator: SelectiveRegenerator | None = None,
    **kwargs: Any,
) -> NewMapResult:
    """Build a complete NEW map through the selective orchestration boundary."""

    owner = regenerator or SelectiveRegenerator(
        fingerprint_cache=kwargs.pop("fingerprint_cache", None),
        metrics=kwargs.pop("metrics", None),
        clock=kwargs.pop("clock", None),
        timeout_seconds=kwargs.pop("timeout_seconds", None),
    )
    return owner.build_new_map(accepted, *args, **kwargs)


def merge_with_service(
    service: Any,
    module_key: str,
    accepted: Mapping[str, Any] | None,
    *args: Any,
    regenerator: SelectiveRegenerator | None = None,
    **kwargs: Any,
) -> SelectiveMergeOutcome:
    """Build the complete map, then invoke a MergeBService once."""

    owner = regenerator or SelectiveRegenerator(
        fingerprint_cache=kwargs.pop("fingerprint_cache", None),
        metrics=kwargs.pop("metrics", None),
        clock=kwargs.pop("clock", None),
        timeout_seconds=kwargs.pop("timeout_seconds", None),
    )
    return owner.merge_with_service(service, module_key, accepted, *args, **kwargs)


selective_merge = merge_with_service


regenerate_selective = selective_regenerate
regenerate_functions = selective_regenerate
TimeoutFallbackError = RegenerationTimeout


__all__ = [
    "AlignmentResult",
    "DeterministicFingerprintCache",
    "FingerprintCache",
    "FingerprintConfidence",
    "FingerprintDetails",
    "FingerprintRecord",
    "FunctionFingerprint",
    "FunctionRegeneration",
    "NewMapResult",
    "HIGH_CONFIDENCE",
    "LOW_CONFIDENCE",
    "PerformanceMetrics",
    "PhaseMetric",
    "PhaseMetrics",
    "RegenerationConflict",
    "RegenerationDecision",
    "RegenerationMetrics",
    "REQUIRED_PHASES",
    "RegenerationPlan",
    "RegenerationResult",
    "RegenerationTimeout",
    "SelectivePlan",
    "SelectiveRegeneration",
    "SelectiveRegenerationError",
    "SelectiveRegenerator",
    "SelectiveRegenerationPlanner",
    "SelectiveMergeOutcome",
    "WorkbookScan",
    "align_rows",
    "align_unmatched",
    "align_unmatched_rows",
    "build_new_map",
    "compute_regeneration_plan",
    "count_used_cells",
    "exact_then_align",
    "iter_used_cells",
    "linear_workbook_scan",
    "plan_regeneration",
    "regenerate_selective",
    "regeneration_plan",
    "scan_used_cells",
    "scan_workbook",
    "merge_with_service",
    "selective_merge",
    "used_cell_scan",
    "selective_regenerate",
    "regenerate_functions",
    "TimeoutFallbackError",
    "workbook_used_cells",
]
