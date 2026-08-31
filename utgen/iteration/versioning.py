"""Pure user-visible display-version transitions.

Versioning owns only the per-Test-Object presentation metadata.  Repository
revisions, semantic fingerprints, XML rendering, and acceptance transactions
remain outside this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Callable, Literal

from .model import DisplayVersion, ModelError, _canonicalize_timestamp


_DISPLAY_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
TransitionOutcome = Literal["accepted", "noop", "failed", "cancelled", "conflict"]
Clock = Callable[[], str]
_OUTCOMES = frozenset({"accepted", "noop", "failed", "cancelled", "conflict"})


class VersioningError(ValueError):
    """Raised when a display version or transition request is invalid."""


def _utc_clock() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonicalize_timestamp(value: object) -> str:
    """Return the strict canonical second-precision timestamp representation."""

    try:
        return _canonicalize_timestamp(value)
    except ModelError as exc:
        raise VersioningError(str(exc)) from exc


def _capture_timestamp(clock: Clock | None) -> str:
    if clock is None:
        clock = _utc_clock
    if not callable(clock):
        raise TypeError("clock must be callable")
    # The injected clock is intentionally called exactly once for each
    # successful transition.  DisplayVersion validates the returned value but
    # does not call the clock again.
    return canonicalize_timestamp(clock())


def _require_version(value: object) -> DisplayVersion:
    if not isinstance(value, DisplayVersion):
        raise TypeError("version must be a DisplayVersion")
    return value


def _require_outcome(value: object) -> TransitionOutcome:
    if type(value) is not str or value not in _OUTCOMES:
        choices = ", ".join(sorted(_OUTCOMES))
        raise VersioningError(f"outcome must be one of {choices}")
    return value  # type: ignore[return-value]


def parse_display_version(value: object, updated_at: object) -> DisplayVersion:
    """Parse a strict ``major.minor`` value with its accepted timestamp."""

    if type(value) is not str:
        raise VersioningError("display version must be a string")
    match = _DISPLAY_VERSION.fullmatch(value)
    if match is None:
        raise VersioningError(
            "display version must match ^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$"
        )
    return DisplayVersion(int(match.group(1)), int(match.group(2)), canonicalize_timestamp(updated_at))


def format_display_version(value: object) -> str:
    """Format one validated display version for XML/manifest boundaries."""

    version = _require_version(value)
    return f"{version.major}.{version.minor}"


def initial_display_version(*, clock: Clock | None = None) -> DisplayVersion:
    """Create the first accepted version, ``1.0``, using one clock read."""

    return DisplayVersion(1, 0, _capture_timestamp(clock))


def advance_merge_a(
    current: DisplayVersion,
    *,
    semantic_change: bool,
    outcome: TransitionOutcome = "accepted",
    clock: Clock | None = None,
) -> DisplayVersion:
    """Apply an accepted semantic Merge A minor transition.

    No-op, failed, cancelled, and blocking-conflict outcomes return the same
    immutable object and do not read the clock.  An accepted semantic change
    reads the clock once and returns the timestamp to share with the bundle.
    """

    current = _require_version(current)
    if type(semantic_change) is not bool:
        raise TypeError("semantic_change must be a boolean")
    outcome = _require_outcome(outcome)
    if outcome != "accepted" or not semantic_change:
        return current
    return DisplayVersion(current.major, current.minor + 1, _capture_timestamp(clock))


def advance_merge_b(
    current: DisplayVersion,
    *,
    outcome: TransitionOutcome = "accepted",
    clock: Clock | None = None,
) -> DisplayVersion:
    """Apply an accepted Merge B major transition for one affected object."""

    current = _require_version(current)
    outcome = _require_outcome(outcome)
    if outcome != "accepted":
        return current
    return DisplayVersion(current.major + 1, 0, _capture_timestamp(clock))


__all__ = [
    "Clock",
    "TransitionOutcome",
    "VersioningError",
    "advance_merge_a",
    "advance_merge_b",
    "canonicalize_timestamp",
    "format_display_version",
    "initial_display_version",
    "parse_display_version",
]
