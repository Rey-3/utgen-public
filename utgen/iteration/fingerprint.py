"""Deterministic semantic identities and fingerprints for iterative generation.

The analysis database is deliberately treated as an input here.  This module does
not persist iteration state and it does not use the byte hash of the DB as a
semantic fingerprint.  The latter is only useful for integrity checks performed by
``analysis_store``.

There are two slightly different consumers of this module:

* the merge layer needs small, stable string keys for decisions and generator
  obligations; and
* diagnostics/selective-regeneration code needs the normalized component payload and
  a confidence signal explaining why a result must be handled conservatively.

The public ``*_fingerprint`` helpers return strings so they are convenient in state
models.  The corresponding ``*_fingerprint_details`` helpers expose the normalized
components and confidence without making callers reconstruct the explanation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path, PurePath
import re
from typing import Any, Iterable
import xml.etree.ElementTree as ET


FINGERPRINT_SCHEMA_VERSION = 1
DEFAULT_GENERATOR_SCHEMA = "utgen-fingerprint-v1"
DEFAULT_GENERATOR_VERSION = "utgen"

_HEX = re.compile(r"^[0-9a-f]{64}$")
_ARRAY_INDEX = re.compile(r"\[\s*\d+\s*\]")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_PREFIX = re.compile(r"^(?:\\\\|//)")
_PATH_KEYS = frozenset({
    "file", "path", "location", "source", "source_file", "sourcefile",
    "filename", "header_path", "include_path", "included_from", "directory",
    "dir", "absolute_path", "absolutepath", "pdb", "pdb_path",
})
_LINE_KEYS = frozenset({
    "line", "lines", "column", "columns", "offset", "end_line", "endline",
    "end_column", "endcolumn", "line_number", "linenumber", "source_line",
})
_DECISION_ID_KEYS = frozenset({
    "id", "decision", "decision_id", "decisionid", "decision_ref", "decisionref",
})
_ROOT_METADATA_KEYS = frozenset({
    "analysis_id", "analysid", "generated_at", "generatedat", "db_schema_version",
    "dbschemaversion", "utgen_version", "utgen_updated_at", "updated_at",
    "version", "timestamp", "created_at", "createdat",
})
_ROOT_INTEGRITY_KEYS = frozenset({
    # SourceAnalysis/ModuleAnalysis integrity and producer metadata are useful to
    # the DB adapter, but are deliberately not semantic analysis inputs.  Header
    # hashes remain meaningful in the dependency component because they are nested
    # declarations rather than root integrity metadata.
    "sha256", "source_sha256", "sourcehash", "raw_sha256", "rawhash",
    "content_sha256", "contenthash", "tool", "tool_version", "toolversion",
    "producer", "producer_version", "producerversion",
})
_NON_SEMANTIC_FIELDS = frozenset({
    "line", "location", "local_functions", "localfunctions", "_cache", "cache",
    "object_id", "objectid", "identity", "source_location", "sourcelocation",
})


class FingerprintError(ValueError):
    """Raised when an analysis/fingerprint input cannot be interpreted."""


class FingerprintConfidence(str, Enum):
    HIGH = "high"
    LOW = "low"


class ComponentChangeReport(dict):
    """Mapping-style explanation with convenient diagnostic properties.

    The mapping contains one entry per component and a ``changed_components`` entry.
    Keeping it a normal dict preserves compatibility with callers that serialize
    reports directly, while the properties make the common diagnostic path concise.
    """

    @property
    def changed_components(self) -> tuple[str, ...]:
        value = self.get("changed_components", ())
        return tuple(value) if isinstance(value, (tuple, list)) else ()

    @property
    def summary(self) -> str:
        names = self.changed_components
        return "no semantic component changed" if not names else "changed: " + ", ".join(names)

    def __str__(self) -> str:  # pragma: no cover - exercised through diagnostics
        return self.summary


class FingerprintDetails:
    """Immutable-ish details object returned by component fingerprint helpers.

    ``normalized`` is intentionally available for diagnostics but should not be put
    into iteration state.  It contains source-analysis data only; no mutable rows,
    descriptions, or user Stub Bodies are copied into it.
    """

    __slots__ = ("fingerprint", "components", "normalized", "confidence", "conservative", "reasons")

    def __init__(
        self,
        fingerprint: str,
        components: Mapping[str, Any],
        normalized: Any,
        *,
        confidence: str = FingerprintConfidence.HIGH.value,
        conservative: bool = False,
        reasons: Iterable[str] = (),
    ) -> None:
        self.fingerprint = fingerprint
        self.components = dict(components)
        self.normalized = normalized
        self.confidence = str(confidence)
        self.conservative = bool(conservative)
        self.reasons = tuple(str(reason) for reason in reasons)

    @property
    def value(self) -> str:
        return self.fingerprint

    @property
    def low_confidence(self) -> bool:
        return self.confidence != FingerprintConfidence.HIGH.value

    @property
    def requires_conservative_handling(self) -> bool:
        return self.conservative or self.low_confidence

    @property
    def explanation(self) -> str:
        if not self.reasons:
            return "semantic fingerprint has high confidence"
        return "; ".join(self.reasons)

    def __str__(self) -> str:
        return self.fingerprint

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return (
            f"FingerprintDetails(fingerprint={self.fingerprint!r}, "
            f"confidence={self.confidence!r}, conservative={self.conservative!r})"
        )


# Friendly aliases used by a few integration callers.
FingerprintResult = FingerprintDetails
FingerprintExplanation = ComponentChangeReport


def _key_name(value: object) -> str:
    """Normalize a field/attribute name for filtering and canonical output."""

    text = str(value)
    if "}" in text:
        text = text.rsplit("}", 1)[-1]
    return "".join(character.lower() for character in text if character.isalnum() or character == "_")


def _local_tag(value: object) -> str:
    text = str(value)
    return text.rsplit("}", 1)[-1]


def normalize_path(value: object, *, root: object | None = None) -> str:
    """Return a machine-independent diagnostic path token.

    Absolute source/header paths are never semantic identities.  We retain a stable
    semantic suffix while dropping drive letters, UNC hosts, and machine-specific
    prefixes.  If an explicit project ``root`` is supplied, the path is made relative
    to that root without guessing which leading components are machine-specific.
    Relative paths are normalized to slash separators and retained.
    """

    if isinstance(value, Path):
        value = str(value)
    text = str(value).replace("\\", "/")
    if not text:
        return ""
    absolute = bool(_WINDOWS_DRIVE.match(text) or _UNC_PREFIX.match(text) or text.startswith("/"))
    if root is not None:
        root_text = str(root).replace("\\", "/").rstrip("/")
        root_absolute = bool(_WINDOWS_DRIVE.match(root_text) or _UNC_PREFIX.match(root_text) or root_text.startswith("/"))
        if absolute and root_absolute:
            left = text.casefold()
            right = root_text.casefold()
            if left == right:
                return ""
            prefix = right + "/"
            if left.startswith(prefix):
                return text[len(root_text) + 1 :].replace("\\", "/")
    text = re.sub(r"^[A-Za-z]:/+", "", text)
    text = text.lstrip("/")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if absolute:
        # A one-directory absolute path is commonly an opaque machine include
        # root (e.g. C:/machine/inc); retain its semantic basename for backwards
        # compatibility.  Once there is more structure, retain the final two
        # components so sibling projects such as .../first/include and
        # .../second/include cannot collide.  A caller with an explicit project
        # root can still pass a relative path and retain the complete structure.
        if len(parts) <= 2:
            return parts[-1] if parts else ""
        return "/".join(parts[-2:])
    return "/".join(parts)


_C_LITERAL_PREFIXES = ("u8", "u", "U", "L")
_C_PUNCTUATORS = tuple(sorted({
    # C punctuators plus the common C++ spellings accepted by the legacy IR.
    "%:%:", "<<=", ">>=", "...", "->*", "##",
    "<<", ">>", "<=", ">=", "==", "!=", "&&", "||",
    "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "^=", "|=",
    "->", ".*", "::", "<:", ":>", "<%", "%>", "%:",
    "[", "]", "(", ")", "{", "}", ".", "&", "*", "+", "-", "~", "!",
    "/", "%", "<", ">", "^", "|", "?", ":", ";", "=", ",", "#",
}, key=lambda item: (-len(item), item)))


def _is_c_identifier_start(character: str) -> bool:
    """Return whether *character* can start a portable C identifier token."""

    return character == "_" or character.isalpha() or (
        ord(character) >= 0x80 and character.isidentifier()
    )


def _is_c_identifier_continue(character: str) -> bool:
    return _is_c_identifier_start(character) or character.isdigit()


def _scan_c_literal(text: str, start: int) -> tuple[str, int] | None:
    """Scan one C string/character literal, including its optional prefix."""

    quote_at = start
    for prefix in _C_LITERAL_PREFIXES:
        candidate = start + len(prefix)
        if text.startswith(prefix, start) and candidate < len(text) and text[candidate] in {"'", '"'}:
            quote_at = candidate
            break
    if quote_at >= len(text) or text[quote_at] not in {"'", '"'}:
        return None

    quote = text[quote_at]
    index = quote_at + 1
    escaped = False
    while index < len(text):
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            index += 1
            break
        index += 1
    return text[start:index], index


def _scan_c_pp_number(text: str, start: int) -> tuple[str, int] | None:
    """Scan a deterministic C preprocessing number token."""

    if not text[start].isdigit() and not (
        text[start] == "." and start + 1 < len(text) and text[start + 1].isdigit()
    ):
        return None
    index = start + 1
    while index < len(text):
        character = text[index]
        if character.isalnum() or character in {"_", "."}:
            index += 1
            continue
        # C pp-numbers retain an exponent sign as part of the same token.
        if character in {"+", "-"} and index > start and text[index - 1] in "eEpP":
            index += 1
            continue
        break
    return text[start:index], index


def _c_preprocessing_tokens(value: object) -> tuple[str, ...]:
    """Tokenize C presentation while preserving literal spelling.

    Comments and whitespace are discarded, but the lexer never concatenates
    neighboring preprocessing tokens: ``a b`` therefore remains ``("a", "b")``
    while ``ab`` is ``("ab",)``.  Encoding the resulting token sequence as JSON
    gives callers an unambiguous, deterministic token-boundary representation.
    """

    text = "" if value is None else str(value)
    tokens: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace():
            index += 1
            continue

        # Comments are recognized only outside literals.  Skipping them here
        # naturally keeps identifier boundaries (``a/*c*/b`` is two tokens),
        # while adjacent literals remain equivalent to their comment-free form.
        if character == "/" and index + 1 < length:
            next_character = text[index + 1]
            if next_character == "/":
                index += 2
                while index < length and text[index] not in "\r\n":
                    index += 1
                continue
            if next_character == "*":
                index += 2
                while index < length:
                    if text[index] == "*" and index + 1 < length and text[index + 1] == "/":
                        index += 2
                        break
                    index += 1
                continue

        literal = _scan_c_literal(text, index)
        if literal is not None:
            token, index = literal
            tokens.append(token)
            continue

        if _is_c_identifier_start(character):
            end = index + 1
            while end < length and _is_c_identifier_continue(text[end]):
                end += 1
            tokens.append(text[index:end])
            index = end
            continue

        number = _scan_c_pp_number(text, index)
        if number is not None:
            token, index = number
            tokens.append(token)
            continue

        for punctuator in _C_PUNCTUATORS:
            if text.startswith(punctuator, index):
                tokens.append(punctuator)
                index += len(punctuator)
                break
        else:
            # Preserve implementation-defined/single-character punctuators as
            # one token rather than silently dropping source information.
            tokens.append(character)
            index += 1
    return tuple(tokens)


def _normalize_text(value: object, *, source_text: bool = False) -> str:
    text = "" if value is None else str(value)
    if not source_text:
        # Semantic strings are data, not source presentation.  In particular a
        # string literal ``"a  b"`` must not collide with ``"a b"`` and URLs
        # containing ``//`` must never be treated as comments.
        return text
    tokens = _c_preprocessing_tokens(text)
    if not tokens:
        return ""
    return json.dumps(tokens, ensure_ascii=False, separators=(",", ":"))


_SOURCE_PRESENTATION_FIELDS = frozenset({
    "text", "expr", "expression", "condition", "guard_expression",
    "source_text", "raw_expression", "predicate",
})


def _field_source_text(field_name: object, *, inherited: bool = False, owner: str = "") -> bool:
    """Select comment/whitespace normalization only for source expressions.

    IR literals and semantic strings (including ``Const.value`` and macro
    replacement text) must remain byte/value-distinct.  Presentation cleanup is
    therefore opt-in by field/tag rather than the old all-string default.
    """

    name = _key_name(field_name)
    owner_name = _key_name(owner)
    if name in _SOURCE_PRESENTATION_FIELDS:
        # ``text`` on an Atom is the original source predicate.  A generic
        # ``text`` value on an opaque record is semantic data and stays literal.
        return name != "text" or owner_name in {
            "atom", "decision", "guard", "condition", "predicate", "unknown",
            "body", "dataflow", "if", "then", "else", "for", "while", "dowhile",
            "switch", "case", "assign", "decl", "exprstmt", "return", "binop",
            "call", "arg", "unary", "ternary", "cast", "index", "memberexpr",
        }
    if name in {
        "value", "raw", "literal", "string", "string_value", "semantic_value",
        "semantic", "url", "uri", "name", "path", "scope", "type_name",
        "typename", "kind", "op", "params", "description", "description_raw",
    }:
        return False
    return inherited


def _is_root_integrity_key(key: object) -> bool:
    return _key_name(key) in _ROOT_INTEGRITY_KEYS


def _normalize_ref_path(value: object) -> str:
    """Normalize an XML Ref path without treating variable names as filenames.

    Legacy DB fixtures occasionally rewrite every ``path`` attribute with an
    absolute machine prefix while preserving the final IR member name.  Strip
    that prefix only for absolute-looking values; ordinary semantic paths such as
    ``Error.ShortCirCounter`` remain exact strings so a real member change still
    changes the function fingerprint.
    """

    text = "" if value is None else str(value)
    normalized = text.replace("\\", "/")
    if _WINDOWS_DRIVE.match(normalized) or _UNC_PREFIX.match(normalized) or normalized.startswith("/"):
        return normalized.rstrip("/").rsplit("/", 1)[-1]
    return text


def _looks_like_path_key(key: object) -> bool:
    name = _key_name(key)
    return name in _PATH_KEYS or name in {
        "include", "includes", "include_dir", "includedir", "include_dirs", "includedirs",
        "search_path", "searchpaths", "paths",
    } or name.endswith("path") or name.endswith("filepath")


def _is_line_key(key: object) -> bool:
    name = _key_name(key)
    return name in _LINE_KEYS or name.endswith("linenumber")


def _is_decision_id_key(key: object) -> bool:
    name = _key_name(key)
    return name in _DECISION_ID_KEYS


def _is_root_metadata_key(key: object) -> bool:
    name = _key_name(key)
    return name in _ROOT_METADATA_KEYS


def _canonical_sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _normalize_scalar(value: object, *, field_name: str = "", source_text: bool = False) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, Enum):
        return _normalize_scalar(value.value, field_name=field_name, source_text=source_text)
    if isinstance(value, float):
        # JSON's spelling of finite floats is deterministic enough; special values are
        # represented as strings instead of relying on allow_nan implementation details.
        if value != value:
            return "nan"
        if value == float("inf"):
            return "inf"
        if value == float("-inf"):
            return "-inf"
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (Path, PurePath)):
        return normalize_path(value)
    if isinstance(value, str):
        if _looks_like_path_key(field_name):
            return normalize_path(value)
        return _normalize_text(value, source_text=source_text)
    return None


def _normalize_value(
    value: object,
    *,
    field_name: str = "",
    root: bool = False,
    seen: set[int] | None = None,
    unsupported: list[str] | None = None,
    source_text: bool = False,
) -> Any:
    """Canonicalize dataclasses, mappings, sequences, and XML nodes.

    This routine is intentionally private because callers should use the semantic
    wrappers below.  It nevertheless accepts broad inputs so a regenerated XML view
    and the legacy ``ModuleAnalysis`` object produce the same style of normalized
    payload.
    """

    if seen is None:
        seen = set()
    if unsupported is None:
        unsupported = []

    if isinstance(value, ET.Element):
        return _normalize_element(value, seen=seen, unsupported=unsupported, root=root)
    if isinstance(value, ET.ElementTree):
        return _normalize_element(value.getroot(), seen=seen, unsupported=unsupported, root=root)

    scalar = _normalize_scalar(value, field_name=field_name, source_text=source_text)
    if scalar is not None or value is None:
        # ``_normalize_scalar`` returns None for objects not handled there.  A real
        # None must still pass through, while unsupported objects continue below.
        if value is None or isinstance(value, (bool, int, float, str, bytes, bytearray, Path, PurePath, Enum)):
            return scalar

    object_id = id(value)
    if object_id in seen:
        unsupported.append(f"cycle:{type(value).__name__}")
        return {"$cycle": type(value).__name__}

    if isinstance(value, Mapping):
        seen.add(object_id)
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            name = _key_name(raw_key)
            if _is_root_metadata_key(name) or _is_line_key(name) or _is_decision_id_key(name):
                continue
            if root and _is_root_integrity_key(name):
                continue
            # ``local_functions`` points back at the complete module and would copy
            # mutable analysis state recursively.  It is not a function semantic input.
            if name in _NON_SEMANTIC_FIELDS:
                continue
            result[str(raw_key)] = _normalize_value(
                item,
                field_name=name,
                seen=seen,
                unsupported=unsupported,
                source_text=_field_source_text(name, inherited=source_text),
            )
        seen.remove(object_id)
        return {key: result[key] for key in sorted(result)}

    if is_dataclass(value):
        seen.add(object_id)
        result = {}
        class_name = type(value).__name__
        # Unknown IR nodes are valid data but force conservative handling.  Known
        # ``Unknown`` nodes are called out explicitly because they represent a parser
        # limitation rather than an ordinary source statement.
        if class_name.lower() in {"unknown", "unsupported", "opaque"}:
            unsupported.append(f"unsupported IR node: {class_name}")
        for item in fields(value):
            name = item.name
            normalized_name = _key_name(name)
            if _is_root_metadata_key(normalized_name) or _is_line_key(normalized_name):
                continue
            if root and _is_root_integrity_key(normalized_name):
                continue
            if _is_decision_id_key(normalized_name) or normalized_name in _NON_SEMANTIC_FIELDS:
                continue
            raw_item = getattr(value, name)
            if _key_name(class_name) == "ref" and normalized_name == "path":
                # Keep dataclass and XML Ref normalization aligned: an absolute
                # machine prefix is diagnostic, while the variable/member suffix
                # is semantic.
                result[name] = _normalize_ref_path(raw_item)
                continue
            result[name] = _normalize_value(
                raw_item,
                field_name=normalized_name,
                seen=seen,
                unsupported=unsupported,
                source_text=_field_source_text(
                    normalized_name,
                    inherited=source_text,
                    owner=class_name,
                ),
            )
        seen.remove(object_id)
        return {key: result[key] for key in sorted(result)}

    if isinstance(value, Mapping):  # pragma: no cover - kept for unusual Mapping ABCs
        return _normalize_value(dict(value), field_name=field_name, seen=seen, unsupported=unsupported)
    if isinstance(value, (set, frozenset)):
        seen.add(object_id)
        items = [
            _normalize_value(item, field_name=field_name, seen=seen, unsupported=unsupported, source_text=source_text)
            for item in value
        ]
        seen.remove(object_id)
        return sorted(items, key=_canonical_sort_key)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        seen.add(object_id)
        item_field = field_name if _key_name(field_name) in {
            "include", "includes", "include_dir", "includedir", "include_dirs", "includedirs",
            "path", "paths", "search_path", "searchpaths",
        } else ""
        items = [
            _normalize_value(
                item,
                field_name=item_field,
                seen=seen,
                unsupported=unsupported,
                source_text=_field_source_text(item_field, inherited=source_text),
            )
            for item in value
        ]
        seen.remove(object_id)
        return items

    # Named tuples and simple external records are common in adapter tests.  Use
    # ``_asdict`` without importing or copying any mutable iteration state.
    as_dict = getattr(value, "_asdict", None)
    if callable(as_dict):
        return _normalize_value(as_dict(), field_name=field_name, seen=seen, unsupported=unsupported)

    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, Mapping):
        unsupported.append(f"unrecognized record: {type(value).__name__}")
        return _normalize_value(attrs, field_name=field_name, seen=seen, unsupported=unsupported)

    unsupported.append(f"unrecognized value: {type(value).__name__}")
    return _normalize_text(value, source_text=source_text)


def _normalize_element(
    element: ET.Element,
    *,
    seen: set[int],
    unsupported: list[str],
    root: bool = False,
) -> dict[str, Any]:
    object_id = id(element)
    if object_id in seen:
        unsupported.append("cycle:Element")
        return {"$cycle": "Element"}
    seen.add(object_id)
    tag = _local_tag(element.tag)
    normalized_tag = _key_name(tag)
    attrs: dict[str, Any] = {}
    for raw_key, raw_value in element.attrib.items():
        name = _key_name(raw_key)
        if _is_root_metadata_key(name) or _is_line_key(name):
            continue
        if root and _is_root_integrity_key(name):
            continue
        # IDs on Decision/IR statement nodes are analysis-run references.  Body
        # semantics are retained through their expression and structural nesting.
        if _is_decision_id_key(name):
            continue
        if _looks_like_path_key(name) or name in {"location", "source_location"}:
            # ``Ref.path`` is an IR variable/member path and therefore semantic;
            # ``file/path/location`` on source-analysis declarations are diagnostic
            # machine paths and must be ignored.
            if normalized_tag == "ref" and name == "path":
                attrs[name] = _normalize_ref_path(raw_value)
                continue
            else:
                continue
        attrs[name] = _normalize_scalar(
            raw_value,
            field_name=name,
            source_text=_field_source_text(name, inherited=False, owner=normalized_tag),
        )
    result: dict[str, Any] = {"tag": tag}
    if attrs:
        result["attributes"] = {key: attrs[key] for key in sorted(attrs)}
    raw_text = element.text
    # Pretty-print indentation is never a semantic body/expression node.  Keep
    # whitespace-only text only for literal-bearing XML tags, where the whitespace
    # itself can be the value (the usual DB writer uses attributes, but the generic
    # adapter must not silently erase a literal).
    if (
        raw_text
        and not raw_text.strip()
        and normalized_tag not in {"const", "macro", "value", "string", "literal"}
    ):
        text = ""
    else:
        text = _normalize_text(
            raw_text,
            source_text=_field_source_text("text", inherited=True, owner=normalized_tag),
        )
    if text:
        # XML comments/formatting are not semantic source.  Non-empty text is kept
        # because unknown IR nodes may be represented as text and must lower confidence.
        result["text"] = text
    children = [
        _normalize_element(child, seen=seen, unsupported=unsupported)
        for child in list(element)
        if isinstance(child.tag, str)
    ]
    if children:
        # Top-level declaration collections are unordered in SourceAnalysis.  Body
        # and expression children retain their order because it affects semantics.
        unordered_parent = normalized_tag in {
            "sourceanalysis", "includes", "macros", "enums", "structs", "variables",
        }
        if unordered_parent:
            children.sort(key=_canonical_sort_key)
        result["children"] = children
    seen.remove(object_id)
    known = {
        "sourceanalysis", "includes", "dir", "header", "macro", "enum", "value",
        "struct", "member", "variable", "function", "param", "stub", "leaf",
        "decision", "guards", "guard", "body", "dataflow", "datapoint", "state",
        "path", "condition", "runtimecondition", "then", "else", "for", "while",
        "dowhile", "switch", "case", "assign", "decl", "exprstmt", "return", "if",
        "break", "atom", "and", "or", "not", "ref", "const", "unary", "binop",
        "call", "arg", "ternary", "cast", "index", "memberexpr", "lhs", "none",
        "write", "writes", "returnvalue", "returns", "returnstate", "aliases", "alias",
        "unknown", "base", "constraint", "init", "increment", "point", "values",
    }
    if normalized_tag not in known and normalized_tag not in {"guards", "condition"}:
        unsupported.append(f"unsupported analysis XML element: {tag}")
    return result


def _read_input(value: object) -> object:
    if isinstance(value, (Path, PurePath)):
        try:
            return ET.fromstring(Path(value).read_bytes())
        except (OSError, ET.ParseError, ValueError) as exc:
            raise FingerprintError(f"cannot read Analysis DB XML: {value}") from exc
    if isinstance(value, (bytes, bytearray)):
        try:
            return ET.fromstring(bytes(value))
        except (ET.ParseError, ValueError) as exc:
            raise FingerprintError("Analysis DB XML is not well formed") from exc
    if isinstance(value, ET.ElementTree):
        return value.getroot()
    return value


def _analysis_model(value: object) -> object:
    """Resolve an AnalysisSnapshot/adapter wrapper to its source-analysis model."""

    if hasattr(value, "analysis") and not isinstance(value, (ET.Element, ET.ElementTree)):
        candidate = getattr(value, "analysis")
        if candidate is not None:
            return candidate
    if hasattr(value, "module_analysis") and not isinstance(value, (ET.Element, ET.ElementTree)):
        candidate = getattr(value, "module_analysis")
        if candidate is not None:
            return candidate
    return _read_input(value)


def _module_functions(analysis: object) -> tuple[object, ...]:
    analysis = _analysis_model(analysis)
    if isinstance(analysis, ET.Element):
        return tuple(child for child in analysis if _local_tag(child.tag).lower() == "function")
    functions = getattr(analysis, "functions", None)
    if functions is not None and not isinstance(functions, (str, bytes, bytearray)):
        return tuple(functions)
    if isinstance(analysis, Mapping):
        values = analysis.get("functions") or analysis.get("Functions") or ()
        return tuple(values)
    return ()


def _function_name(function: object) -> str:
    if isinstance(function, ET.Element):
        return function.get("name", "")
    if isinstance(function, Mapping):
        return str(function.get("name", function.get("function", "")))
    return str(getattr(function, "name", ""))


def _select_function(analysis_or_function: object, function: object | None = None) -> tuple[object, object | None]:
    """Accept both ``(function)`` and ``(analysis, function_name)`` conventions."""

    if function is None:
        candidate = _analysis_model(analysis_or_function)
        if _function_name(candidate):
            return candidate, None
        return candidate, None
    analysis = _analysis_model(analysis_or_function)
    if isinstance(function, str):
        for candidate in _module_functions(analysis):
            if _function_name(candidate) == function:
                return candidate, analysis
        raise FingerprintError(f"function {function!r} is not present in the analysis")
    return function, analysis


def _function_payload(function: object, unsupported: list[str]) -> dict[str, Any]:
    if isinstance(function, ET.Element):
        normalized = _normalize_element(function, seen=set(), unsupported=unsupported)
        return normalized
    if isinstance(function, Mapping):
        value = _normalize_value(function, unsupported=unsupported)
        return value if isinstance(value, dict) else {"value": value}
    value = _normalize_value(function, unsupported=unsupported)
    return value if isinstance(value, dict) else {"value": value}


def _hash_payload(payload: object, *, prefix: str = "") -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()


def canonical_json(value: object) -> str:
    """Expose canonical JSON used by fingerprints for diagnostics/tests."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def canonical_fingerprint_payload(value: object) -> bytes:
    """Return canonical bytes for a semantic value, excluding presentation churn."""

    unsupported: list[str] = []
    normalized = _normalize_value(_analysis_model(value), root=True, unsupported=unsupported)
    return canonical_json(normalized).encode("utf-8")


def normalize_analysis(value: object) -> Any:
    """Return a metadata-free normalized SourceAnalysis payload.

    ``file``, ``path``, ``location``, line/column fields, transient decision IDs,
    generated metadata, and XML formatting are excluded.  Mapping keys are sorted;
    ordered IR/body children remain ordered.
    """

    unsupported: list[str] = []
    model = _analysis_model(value)
    normalized = _normalize_value(model, root=True, unsupported=unsupported)
    normalized = _canonicalize_analysis_declarations(normalized)
    if isinstance(normalized, dict) and unsupported:
        # A marker makes low-confidence normalizations distinguishable without
        # incorporating machine-specific unsupported source text into the key.
        normalized = dict(normalized)
        normalized["$unsupported_ir"] = tuple(sorted(set(unsupported)))
    return normalized


_DECLARATION_COLLECTIONS = frozenset({
    "headers", "macros", "enums", "structs", "variables", "functions",
})


def _canonicalize_analysis_declarations(value: Any) -> Any:
    """Canonicalize unordered ModuleAnalysis declaration collections.

    Function/body and expression sequences are intentionally untouched; only the
    module-level declaration arrays whose order is an artifact of the parser are
    sorted.  XML SourceAnalysis already has an equivalent top-level child sort,
    while this pass makes the legacy dataclass representation agree with it.
    """

    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    for key, items in tuple(result.items()):
        if _key_name(key) not in _DECLARATION_COLLECTIONS:
            continue
        if isinstance(items, list):
            result[key] = sorted(
                items,
                key=lambda item: (
                    _item_name_from_normalized(item),
                    _canonical_sort_key(item),
                ),
            )
    return result


def _item_name_from_normalized(item: Any) -> str:
    if isinstance(item, Mapping):
        attributes = item.get("attributes")
        if isinstance(attributes, Mapping) and attributes.get("name") is not None:
            return str(attributes.get("name"))
        if item.get("name") is not None:
            return str(item.get("name"))
    if isinstance(item, list) and item:
        return str(item[0])
    return ""


normalized_analysis = normalize_analysis
normalize_source_analysis = normalize_analysis
normalize_db = normalize_analysis
normalized_db = normalize_analysis


def analysis_fingerprint(value: object) -> str:
    """Hash the normalized semantic analysis payload (not raw XML bytes)."""

    return _hash_payload(normalize_analysis(value), prefix="a_")


fingerprint_analysis = analysis_fingerprint


def normalized_subtree(value: object) -> Any:
    """Compatibility spelling for callers that pass one DB subtree."""

    unsupported: list[str] = []
    source = _read_input(value)
    result = _normalize_value(
        source,
        root=isinstance(source, ET.Element) and _local_tag(source.tag).lower() == "sourceanalysis",
        unsupported=unsupported,
    )
    if isinstance(result, dict) and unsupported:
        result = dict(result)
        result["$unsupported_ir"] = tuple(sorted(set(unsupported)))
    return result


def _generator_components(
    *,
    generator_version: object = DEFAULT_GENERATOR_VERSION,
    generator_schema: object = DEFAULT_GENERATOR_SCHEMA,
) -> dict[str, str]:
    version = _normalize_text(generator_version, source_text=False) or DEFAULT_GENERATOR_VERSION
    schema = _normalize_text(generator_schema, source_text=False) or DEFAULT_GENERATOR_SCHEMA
    return {"generator_version": version, "generator_schema": schema}


def _function_components(
    function: object,
    *,
    generator_version: object = DEFAULT_GENERATOR_VERSION,
    generator_schema: object = DEFAULT_GENERATOR_SCHEMA,
) -> tuple[dict[str, Any], list[str]]:
    unsupported: list[str] = []
    payload = _function_payload(function, unsupported)

    # Keep the component labels explicit so change explanations can identify the
    # smallest source of a regeneration decision.  A FunctionIR/Function XML payload
    # already contains these subtrees; extracting them here also avoids accidentally
    # dropping a component when the serializer gains a new field.
    if isinstance(payload, Mapping):
        def find(name: str, default: Any = None) -> Any:
            if name in payload:
                return payload[name]
            attrs = payload.get("attributes")
            if isinstance(attrs, Mapping):
                aliases = {name, name.lower(), name.replace("_", "")}
                for key in aliases:
                    if key in attrs:
                        return attrs[key]
            children = payload.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, Mapping) and str(child.get("tag", "")).lower() == name.lower():
                        return child
            return default

        signature = {
            "name": find("name", ""),
            "return": find("return", find("return_type", "")),
            "static": find("static", False),
            "params": find("params", ()),
        }
        leaves = find("leaves", ())
        body = find("body", ())
        dataflow = find("dataflow", ())
        decisions = find("decisions", ())
        stubs = find("stubs", ())
        # For XML, collections are represented as child lists rather than named
        # fields.  Preserve semantic order for body/decisions and collect declarations
        # into their corresponding components.
        children = payload.get("children")
        if isinstance(children, list):
            params = [child for child in children if isinstance(child, Mapping) and str(child.get("tag", "")).lower() == "param"]
            leaves = [child for child in children if isinstance(child, Mapping) and str(child.get("tag", "")).lower() == "leaf"]
            stubs = [child for child in children if isinstance(child, Mapping) and str(child.get("tag", "")).lower() == "stub"]
            decisions = [child for child in children if isinstance(child, Mapping) and str(child.get("tag", "")).lower() == "decision"]
            body = next((child for child in children if isinstance(child, Mapping) and str(child.get("tag", "")).lower() == "body"), body)
            dataflow = next((child for child in children if isinstance(child, Mapping) and str(child.get("tag", "")).lower() == "dataflow"), dataflow)
            signature["params"] = params

        # ``find`` above handles model-field keys but the XML attribute name is
        # normalized to ``return``.  Preserve only semantic subtrees in each bucket.
        components: dict[str, Any] = {
            "signature": signature,
            "leaves": leaves,
            "body": body,
            "dataflow": dataflow,
            "decisions": decisions,
            "stubs": stubs,
            "stub_callees": [
                name for name in _calls(function)
                if name in {
                    str(item.get("name", "")) for item in (stubs if isinstance(stubs, list) else ())
                    if isinstance(item, Mapping)
                }
            ] or _calls(function),
            **_generator_components(generator_version=generator_version, generator_schema=generator_schema),
        }
    else:  # pragma: no cover - defensive fallback for a pathological adapter
        components = {"function": payload, **_generator_components(generator_version=generator_version, generator_schema=generator_schema)}
    return components, unsupported


def function_fingerprint_details(
    analysis_or_function: object,
    function: object | None = None,
    *,
    generator_version: object = DEFAULT_GENERATOR_VERSION,
    generator_schema: object = DEFAULT_GENERATOR_SCHEMA,
    generator: object | None = None,
    version: object | None = None,
    schema: object | None = None,
) -> FingerprintDetails:
    """Compute a normalized per-function fingerprint and confidence.

    ``analysis_or_function`` may be a ``FunctionIR``, XML ``Function`` element,
    ``ModuleAnalysis``, ``AnalysisSnapshot``, XML path/bytes, or a mapping.  When the
    first argument is an analysis, ``function`` may be the function name or object.
    """

    if version is not None:
        generator_version = version
    if schema is not None:
        generator_schema = schema
    if generator is not None:
        if isinstance(generator, Mapping):
            generator_version = generator.get("version", generator.get("generator_version", generator_version))
            generator_schema = generator.get("schema", generator.get("generator_schema", generator_schema))
        elif isinstance(generator, str):
            generator_version = generator
        else:
            generator_version = getattr(generator, "version", getattr(generator, "generator_version", generator_version))
            generator_schema = getattr(generator, "schema", getattr(generator, "generator_schema", generator_schema))
    selected, _analysis = _select_function(analysis_or_function, function)
    components, unsupported = _function_components(
        selected,
        generator_version=generator_version,
        generator_schema=generator_schema,
    )
    confidence = FingerprintConfidence.LOW.value if unsupported else FingerprintConfidence.HIGH.value
    reasons = tuple(sorted(set(unsupported)))
    if reasons:
        reasons = ("unsupported IR/analysis data requires conservative handling",) + reasons
    payload = {"schema": FINGERPRINT_SCHEMA_VERSION, "components": components, "confidence": confidence}
    return FingerprintDetails(
        _hash_payload(payload, prefix="f_"),
        components,
        payload,
        confidence=confidence,
        conservative=bool(unsupported),
        reasons=reasons,
    )


def function_fingerprint(
    analysis_or_function: object,
    function: object | None = None,
    *,
    generator_version: object = DEFAULT_GENERATOR_VERSION,
    generator_schema: object = DEFAULT_GENERATOR_SCHEMA,
    generator: object | None = None,
    version: object | None = None,
    schema: object | None = None,
) -> str:
    return function_fingerprint_details(
        analysis_or_function,
        function,
        generator_version=generator_version,
        generator_schema=generator_schema,
        generator=generator,
        version=version,
        schema=schema,
    ).fingerprint


compute_function_fingerprint = function_fingerprint
function_fingerprint_result = function_fingerprint_details


def _config_payload(config: object, *, generator_version: object | None = None, generator_schema: object | None = None) -> dict[str, Any]:
    # Config values are semantic only for generation.  Keep unknown keys rather than
    # silently skipping a future policy; mapping order and machine path prefixes are
    # normalized by _normalize_value.
    unsupported: list[str] = []
    normalized = _normalize_value(config if config is not None else {}, unsupported=unsupported)
    if not isinstance(normalized, Mapping):
        normalized = {"value": normalized}
    result: dict[str, Any] = dict(normalized)
    if generator_version is not None:
        result["generator_version"] = _normalize_text(generator_version, source_text=False)
    if generator_schema is not None:
        result["generator_schema"] = _normalize_text(generator_schema, source_text=False)
    if unsupported:
        result["$unsupported_config"] = tuple(sorted(set(unsupported)))
    return {str(key): result[key] for key in sorted(result)}


def config_fingerprint(
    config: object = None,
    *,
    generator_version: object | None = None,
    generator_schema: object | None = None,
    generator: object | None = None,
    **config_fields: object,
) -> str:
    if config_fields:
        if config is None:
            config = dict(config_fields)
        elif isinstance(config, Mapping):
            merged = dict(config)
            merged.update(config_fields)
            config = merged
        else:
            raise TypeError("keyword config fields require a mapping config")
    if generator is not None:
        if isinstance(generator, Mapping):
            generator_version = generator.get("version", generator.get("generator_version", generator_version))
            generator_schema = generator.get("schema", generator.get("generator_schema", generator_schema))
        elif isinstance(generator, str):
            generator_version = generator
        else:
            generator_version = getattr(generator, "version", getattr(generator, "generator_version", generator_version))
            generator_schema = getattr(generator, "schema", getattr(generator, "generator_schema", generator_schema))
    return _hash_payload(
        {"schema": FINGERPRINT_SCHEMA_VERSION, "config": _config_payload(config, generator_version=generator_version, generator_schema=generator_schema)},
        prefix="c_",
    )


compute_config_fingerprint = config_fingerprint


def _as_named_map(items: Iterable[object], *, name_getter=_function_name) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        name = name_getter(item)
        if name:
            result[name] = item
    return result


def _calls(value: object) -> tuple[str, ...]:
    names: set[str] = set()

    def visit(item: object, seen: set[int]) -> None:
        if item is None or isinstance(item, (str, bytes, bytearray, int, float, bool, Enum, Path, PurePath)):
            return
        object_id = id(item)
        if object_id in seen:
            return
        seen.add(object_id)
        if isinstance(item, ET.Element):
            tag = _local_tag(item.tag).lower()
            if tag in {"call", "callexpression"} and item.get("name"):
                names.add(item.get("name", ""))
            for child in item:
                visit(child, seen)
            seen.remove(object_id)
            return
        if isinstance(item, Mapping):
            tag = str(item.get("tag", "")).lower()
            if tag in {"call", "callexpression"} and item.get("name"):
                names.add(str(item["name"]))
            for key, child in item.items():
                if _key_name(key) not in _NON_SEMANTIC_FIELDS:
                    visit(child, seen)
            seen.remove(object_id)
            return
        if is_dataclass(item):
            if type(item).__name__.lower() in {"calle", "call", "callexpression"} and getattr(item, "name", None):
                names.add(str(getattr(item, "name")))
            for field in fields(item):
                if _key_name(field.name) not in _NON_SEMANTIC_FIELDS:
                    visit(getattr(item, field.name), seen)
            seen.remove(object_id)
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, seen)
        seen.remove(object_id)

    visit(value, set())
    return tuple(sorted(name for name in names if name))


def _analysis_collection(analysis: object, name: str) -> tuple[object, ...]:
    analysis = _analysis_model(analysis)
    if isinstance(analysis, ET.Element):
        tag_name = name.lower()
        if tag_name == "headers":
            includes = next((child for child in analysis if _local_tag(child.tag).lower() == "includes"), None)
            if includes is None:
                return ()
            return tuple(child for child in includes if _local_tag(child.tag).lower() == "header")
        return tuple(child for child in analysis if _local_tag(child.tag).lower() == tag_name.rstrip("s"))
    items = getattr(analysis, name, None)
    if items is None and isinstance(analysis, Mapping):
        items = analysis.get(name) or analysis.get(name.rstrip("s")) or ()
    return tuple(items or ())


def _item_name(item: object) -> str:
    if isinstance(item, ET.Element):
        return item.get("name", "")
    if isinstance(item, Mapping):
        return str(item.get("name", ""))
    return str(getattr(item, "name", ""))


def _item_hash(item: object) -> str:
    if isinstance(item, ET.Element):
        return item.get("sha256", item.get("hash", ""))
    if isinstance(item, Mapping):
        return str(item.get("sha256", item.get("hash", "")))
    # Header tuples from ModuleIndexer are (name, path, sha256).
    if isinstance(item, (tuple, list)) and len(item) >= 3:
        return str(item[2])
    return ""


def _item_semantics(item: object, unsupported: list[str]) -> Any:
    if isinstance(item, (tuple, list)) and len(item) >= 3:
        return {
            "name": _normalize_text(item[0], source_text=False),
            "sha256": _normalize_text(item[2], source_text=False),
        }
    return _normalize_value(item, unsupported=unsupported)


def _type_names(function: object, analysis: object | None) -> tuple[str, ...]:
    names: set[str] = set()
    # C types in signatures/leaves/stubs are relevant schema inputs.  Generic text
    # traversal is intentionally conservative and also catches nested member types.
    def visit(item: object, seen: set[int] | None = None) -> None:
        if seen is None:
            seen = set()
        if isinstance(item, Mapping):
            if id(item) in seen:
                return
            seen.add(id(item))
            for key, value in item.items():
                name = _key_name(key)
                if name in {"type", "typename", "return", "returntype", "type_name"} and isinstance(value, str):
                    names.add(_normalize_text(value, source_text=False))
                if name not in _NON_SEMANTIC_FIELDS:
                    visit(value, seen)
            seen.remove(id(item))
        elif isinstance(item, ET.Element):
            if id(item) in seen:
                return
            seen.add(id(item))
            for key in ("type", "return", "return_type"):
                if item.get(key):
                    names.add(_normalize_text(item.get(key), source_text=False))
            for child in item:
                visit(child, seen)
            seen.remove(id(item))
        elif is_dataclass(item):
            if id(item) in seen:
                return
            seen.add(id(item))
            for field in fields(item):
                if _key_name(field.name) not in _NON_SEMANTIC_FIELDS:
                    visit(getattr(item, field.name), seen)
            seen.remove(id(item))
        elif isinstance(item, (list, tuple, set, frozenset)):
            for value in item:
                visit(value, seen)
    visit(function)
    if analysis is not None:
        # Referenced type resolution is not available in all legacy XML databases;
        # including all definitions is conservative and guarantees no missed caller.
        for collection_name in ("structs", "enums"):
            for item in _analysis_collection(analysis, collection_name):
                name = _item_name(item)
                if name:
                    names.add(name)
    return tuple(sorted(name for name in names if name))


def _macro_payloads(analysis: object | None, function: object, unsupported: list[str]) -> list[Any]:
    macros = _analysis_collection(analysis, "macros") if analysis is not None else ()
    if not macros:
        constants = getattr(function, "constants", None)
        if isinstance(constants, Mapping):
            return [
                {"name": str(key), "value": _normalize_value(value, unsupported=unsupported)}
                for key, value in sorted(constants.items(), key=lambda item: str(item[0]))
            ]
    return [
        _item_semantics(item, unsupported)
        for item in sorted(macros, key=lambda value: (_item_name(value), _canonical_sort_key(_normalize_value(value, unsupported=unsupported))))
    ]


def _header_payloads(analysis: object | None, unsupported: list[str]) -> list[Any]:
    headers = _analysis_collection(analysis, "headers") if analysis is not None else ()
    return [
        _item_semantics(item, unsupported)
        for item in sorted(headers, key=lambda value: (_item_name(value), _item_hash(value)))
    ]


def _include_dir_payloads(analysis: object | None) -> tuple[str, ...]:
    """Return normalized include-search directories from the source analysis.

    Include directories are parser inputs even when no individual Header record
    changed.  Keep a semantic suffix (via normalize_path) and canonical order,
    while dropping machine-specific absolute prefixes.
    """

    if analysis is None:
        return ()
    model = _analysis_model(analysis)
    values: list[object] = []
    source_file = ""
    if isinstance(model, ET.Element):
        source_file = model.get("file", "")
        includes = next((child for child in model if _local_tag(child.tag).lower() == "includes"), None)
        if includes is not None:
            values.extend(
                child.get("path", "")
                for child in includes
                if _local_tag(child.tag).lower() in {"dir", "includedir", "include_dir"}
            )
    elif isinstance(model, Mapping):
        source_file = str(model.get("file", "") or model.get("source", ""))
        values = list(model.get("include_dirs", model.get("includedirs", ())) or ())
    else:
        source_file = str(getattr(model, "file", "") or "")
        values = list(getattr(model, "include_dirs", ()) or ())
    source_text = source_file.replace("\\", "/").rstrip("/")
    source_root = source_text.rsplit("/", 1)[0] if "/" in source_text else ""
    return tuple(sorted({
        normalize_path(value, root=source_root or None)
        for value in values
        if str(value)
    }))


def _struct_payloads(analysis: object | None, unsupported: list[str]) -> list[Any]:
    structs = _analysis_collection(analysis, "structs") if analysis is not None else ()
    enums = _analysis_collection(analysis, "enums") if analysis is not None else ()
    values = list(structs) + list(enums)
    return [
        _item_semantics(item, unsupported)
        for item in sorted(values, key=lambda value: (_item_name(value), _canonical_sort_key(_normalize_value(value, unsupported=unsupported))))
    ]


def _helper_payloads(
    function: object,
    analysis: object | None,
    *,
    generator_version: object,
    generator_schema: object,
    unsupported: list[str],
    _stack: tuple[int, ...] = (),
) -> list[Any]:
    names = _calls(function)
    if not names:
        return []
    functions = _as_named_map(_module_functions(analysis), name_getter=_function_name) if analysis is not None else {}
    local = getattr(function, "local_functions", None)
    if isinstance(local, Mapping):
        functions.update({str(name): value for name, value in local.items() if value is not None})
    current_id = id(function)
    stack = _stack if current_id in _stack else (*_stack, current_id)
    result = []
    for name in names:
        helper = functions.get(name)
        if helper is None:
            unsupported.append(f"unresolved helper dependency: {name}")
            result.append({"name": name, "status": "unresolved"})
            continue
        if helper is function or id(helper) in stack:
            # A known recursive edge is complete information, not an unresolved
            # dependency.  Emit a name-only cycle marker so hashing terminates and
            # remains independent of object IDs or traversal order.
            result.append({"name": name, "status": "cycle"})
            continue
        helper_components, helper_unsupported = _function_components(
            helper,
            generator_version=generator_version,
            generator_schema=generator_schema,
        )
        unsupported.extend(helper_unsupported)
        nested = _helper_payloads(
            helper,
            analysis,
            generator_version=generator_version,
            generator_schema=generator_schema,
            unsupported=unsupported,
            _stack=stack,
        )
        if any(
            isinstance(item, Mapping) and item.get("status") == "unresolved"
            for item in nested
        ):
            unsupported.append(f"transitive unresolved helper dependency: {name}")
        semantic = {
            "name": name,
            "function": helper_components,
            "callees": _calls(helper),
            "dependencies": nested,
        }
        result.append({
            "name": name,
            "fingerprint": _hash_payload(semantic),
            "dependencies": nested,
        })
    return result


def _config_dependency_payload(config: object) -> dict[str, Any]:
    # Keep the policy surface explicit in the docs and retain unknown future fields.
    normalized = _config_payload(config)
    if not isinstance(config, Mapping):
        return normalized
    selected: dict[str, Any] = {}
    for key, value in normalized.items():
        name = _key_name(key)
        if (
            name in {
                "defines", "define", "includes", "include", "include_dirs", "includedirs",
                "no_stub", "nostub", "char_signed", "charsigned", "signed_char", "signedchar",
                "abi", "target", "triple", "pointer_width", "pointerwidth", "generation_policy",
                "generationpolicy", "policy", "execute_static_helpers", "executestatichelpers",
                "const_globals", "constglobals", "tessy_dialect", "tessydialect",
            }
            or name.endswith("policy")
            or name.endswith("abi")
        ):
            selected[str(key)] = value
    return selected


def dependency_fingerprint_details(
    analysis_or_function: object,
    function: object | None = None,
    config: object = None,
    *,
    generator_version: object = DEFAULT_GENERATOR_VERSION,
    generator_schema: object = DEFAULT_GENERATOR_SCHEMA,
    generator: object | None = None,
    version: object | None = None,
    schema: object | None = None,
    generator_config: object | None = None,
) -> FingerprintDetails:
    """Compute dependency/config-sensitive fingerprint for one function.

    The first implementation is intentionally conservative: all available header,
    macro, and type definitions are included when the legacy analysis DB cannot prove
    precise references.  This may regenerate an extra caller but never silently skips
    a relevant helper/header/macro/type change.
    """

    if version is not None:
        generator_version = version
    if schema is not None:
        generator_schema = schema
    if generator_config is not None:
        config = generator_config
    if config is None and function is not None and isinstance(function, Mapping):
        # A common compact call form is dependency_fingerprint(function, config).
        # Distinguish it from dependency_fingerprint(analysis, function_mapping)
        # by requiring the first argument to already look like a function record.
        if _function_name(analysis_or_function):
            config, function = function, None
    if generator is not None:
        if isinstance(generator, Mapping):
            generator_version = generator.get("version", generator.get("generator_version", generator_version))
            generator_schema = generator.get("schema", generator.get("generator_schema", generator_schema))
        elif isinstance(generator, str):
            generator_version = generator
        else:
            generator_version = getattr(generator, "version", getattr(generator, "generator_version", generator_version))
            generator_schema = getattr(generator, "schema", getattr(generator, "generator_schema", generator_schema))
    selected, analysis = _select_function(analysis_or_function, function)
    unsupported: list[str] = []
    # Dependency confidence must inherit unsupported IR status from the selected
    # function as well; otherwise an opaque node could accidentally take the fast
    # path even though the function fingerprint is conservative.
    _function_payload(selected, unsupported)
    dependency_config = _config_dependency_payload(config)
    config_by_name = {
        _key_name(key): value for key, value in dependency_config.items()
    }
    components: dict[str, Any] = {
        "helpers": _helper_payloads(
            selected,
            analysis,
            generator_version=generator_version,
            generator_schema=generator_schema,
            unsupported=unsupported,
        ),
        "macros": _macro_payloads(analysis, selected, unsupported),
        "types": _struct_payloads(analysis, unsupported),
        "type_names": _type_names(selected, analysis),
        "headers": _header_payloads(analysis, unsupported),
        "include_dirs": _include_dir_payloads(analysis),
        "config": dependency_config,
        # Named dependency inputs make reports useful to callers without requiring
        # them to understand the merged config mapping.  The merged ``config`` field
        # remains the canonical source used for hashing.
        "defines": config_by_name.get("defines", config_by_name.get("define", ())),
        "includes": config_by_name.get("includes", config_by_name.get("include", config_by_name.get("include_dirs", ()))),
        "abi": config_by_name.get("abi", config_by_name.get("target", config_by_name.get("triple"))),
        "no_stub": config_by_name.get("no_stub", config_by_name.get("nostub")),
        "policy": {
            key: value for key, value in dependency_config.items()
            if _key_name(key).endswith("policy") or _key_name(key) in {"policy", "generation_policy"}
        },
        "stubs": _normalize_value(getattr(selected, "stubs", None), unsupported=unsupported),
        "callees": _calls(selected),
        **_generator_components(generator_version=generator_version, generator_schema=generator_schema),
    }
    # Missing local-function/module information means a caller relationship could not
    # be fully proven.  Mark it low confidence even if no explicit Unknown node exists.
    if _calls(selected):
        if any(item.get("status") == "unresolved" for item in components["helpers"] if isinstance(item, Mapping)):
            unsupported.append("local helper dependency graph unavailable")
    confidence = FingerprintConfidence.LOW.value if unsupported else FingerprintConfidence.HIGH.value
    reasons_list = list(sorted(set(unsupported)))
    # Until precise reference extraction is available, module-level macro/type/
    # header collections are intentionally included wholesale.  This is safe but
    # conservative; expose that fact to callers so a later precision pass (PR14)
    # can distinguish it from a high-confidence selective dependency proof.
    whole_module_declarations = bool(analysis is not None)
    if whole_module_declarations:
        reasons_list.insert(0, "module declarations (macros/types/headers) are included conservatively")
    reasons = tuple(dict.fromkeys(reasons_list))
    if unsupported:
        reasons = ("dependency graph is incomplete; conservative regeneration required",) + reasons
    payload = {"schema": FINGERPRINT_SCHEMA_VERSION, "components": components, "confidence": confidence}
    return FingerprintDetails(
        _hash_payload(payload, prefix="dep_"),
        components,
        payload,
        confidence=confidence,
        conservative=bool(unsupported) or whole_module_declarations,
        reasons=reasons,
    )


def dependency_fingerprint(
    analysis_or_function: object,
    function: object | None = None,
    config: object = None,
    *,
    generator_version: object = DEFAULT_GENERATOR_VERSION,
    generator_schema: object = DEFAULT_GENERATOR_SCHEMA,
    generator: object | None = None,
    version: object | None = None,
    schema: object | None = None,
    generator_config: object | None = None,
) -> str:
    return dependency_fingerprint_details(
        analysis_or_function,
        function,
        config,
        generator_version=generator_version,
        generator_schema=generator_schema,
        generator=generator,
        version=version,
        schema=schema,
        generator_config=generator_config,
    ).fingerprint


compute_dependency_fingerprint = dependency_fingerprint
dependency_fingerprint_result = dependency_fingerprint_details


def _decision_data(decision: object) -> tuple[str, Any, Any]:
    if isinstance(decision, ET.Element):
        kind = decision.get("kind", _local_tag(decision.tag).lower())
        guards = next((child for child in decision if _local_tag(child.tag).lower() == "guards"), None)
        guard_data = _normalize_element(guards, seen=set(), unsupported=[]) if guards is not None else ()
        expressions = [child for child in decision if _local_tag(child.tag).lower() != "guards"]
        expression = expressions[0] if expressions else None
        expr_data = _normalize_element(expression, seen=set(), unsupported=[]) if expression is not None else None
        return _normalize_text(kind, source_text=False), expr_data, guard_data
    if isinstance(decision, Mapping):
        kind = decision.get("kind", decision.get("type", "decision"))
        expr = decision.get("expr", decision.get("expression", decision.get("condition")))
        guards = decision.get("guards", decision.get("context", ()))
        return _normalize_text(kind, source_text=False), expr, guards
    kind = getattr(decision, "kind", type(decision).__name__.lower())
    expr = getattr(decision, "expr", getattr(decision, "condition", None))
    guards = getattr(decision, "guards", ())
    return _normalize_text(kind, source_text=False), expr, guards


def _structural_context(decision: object, *, context: object = None) -> Any:
    if context is not None:
        return _normalize_value(context, unsupported=[])
    # Guards are semantic reachability context and remain stable when source lines or
    # analysis-run IDs change.  A direct Decision has no body path, so guards provide
    # the strongest available disambiguator.
    _kind, _expr, guards = _decision_data(decision)
    return _normalize_value(guards, unsupported=[])


def decision_identity_payload(
    decision: object | None = None,
    *,
    context: object = None,
    occurrence: object = 0,
    kind: object | None = None,
    expression: object | None = None,
    guards: object | None = None,
) -> dict[str, Any]:
    if decision is None:
        decision = {
            "kind": "decision" if kind is None else kind,
            "expr": expression,
            "guards": () if guards is None else guards,
        }
    elif expression is not None or kind is not None or guards is not None:
        # Also accept decision_key("if", expression=...) style calls while keeping
        # a supplied Decision object authoritative for fields not explicitly passed.
        source = {"kind": kind, "expr": expression, "guards": guards}
        if isinstance(decision, str):
            source["kind"] = decision if kind is None else kind
            source["guards"] = () if guards is None else guards
            decision = source
    kind, expression, guards = _decision_data(decision)
    normalized_expression = _normalize_value(expression, unsupported=[])
    normalized_context = _normalize_value(context, unsupported=[]) if context is not None else _normalize_value(guards, unsupported=[])
    try:
        normalized_occurrence = int(occurrence)
    except (TypeError, ValueError):
        normalized_occurrence = 0
    return {
        "kind": kind,
        "expression": normalized_expression,
        "context": normalized_context,
        "occurrence": max(0, normalized_occurrence),
    }


def decision_key(
    decision: object | None = None,
    context: object = None,
    occurrence: object = 0,
    *,
    function_key: object | None = None,
    structural_context: object | None = None,
    occurrence_index: object | None = None,
    kind: object | None = None,
    expression: object | None = None,
    guards: object | None = None,
) -> str:
    """Return stable normalized decision identity, never using transient ID/line."""

    if structural_context is not None:
        context = structural_context
    if occurrence_index is not None:
        occurrence = occurrence_index
    payload = decision_identity_payload(
        decision,
        context=context,
        occurrence=occurrence,
        kind=kind,
        expression=expression,
        guards=guards,
    )
    if function_key is not None:
        # Function scope prevents equal decisions in different functions from
        # accidentally sharing a provenance key while retaining function rename policy
        # at the merge layer.
        payload["function"] = _normalize_text(function_key, source_text=False)
    return _hash_payload(payload, prefix="d_")


make_decision_key = decision_key
normalized_decision_key = decision_key
decision_identity = decision_key


def _decision_records(function: object) -> tuple[tuple[object, Any], ...]:
    if isinstance(function, ET.Element):
        return tuple((child, None) for child in function if _local_tag(child.tag).lower() == "decision")
    if isinstance(function, Mapping):
        values = function.get("decisions", function.get("decision", ()))
        return tuple((value, None) for value in (values or ()))
    values = getattr(function, "decisions", ()) or ()
    return tuple((value, None) for value in values)


def _decision_contexts(function: object) -> dict[int, Any]:
    """Best-effort structural paths for FunctionIR/XML body decision references."""

    contexts: dict[int, Any] = {}
    body = None
    if isinstance(function, ET.Element):
        body = next((child for child in function if _local_tag(child.tag).lower() == "body"), None)
    elif isinstance(function, Mapping):
        body = function.get("body")
    else:
        body = getattr(function, "body", None)

    def visit(value: object, path: tuple[Any, ...]) -> None:
        if isinstance(value, ET.Element):
            tag = _local_tag(value.tag).lower()
            decision_id = value.get("decision")
            if decision_id not in {None, "", "0"}:
                try:
                    contexts[int(decision_id)] = path + (tag,)
                except ValueError:
                    pass
            for index, child in enumerate(value):
                if isinstance(child.tag, str):
                    visit(child, path + ((tag, index),))
            return
        if isinstance(value, Mapping):
            decision_id = value.get("decision_id", value.get("decision"))
            if decision_id not in {None, "", 0, "0"}:
                try:
                    contexts[int(decision_id)] = path + (str(value.get("type", value.get("tag", "node"))),)
                except (TypeError, ValueError):
                    pass
            for index, (key, child) in enumerate(sorted(value.items(), key=lambda item: str(item[0]))):
                if _is_decision_id_key(key):
                    continue
                visit(child, path + ((str(key), index),))
            return
        if is_dataclass(value):
            decision_id = getattr(value, "decision_id", None)
            if decision_id not in {None, 0, "0"}:
                try:
                    contexts[int(decision_id)] = path + (type(value).__name__.lower(),)
                except (TypeError, ValueError):
                    pass
            for index, item in enumerate(fields(value)):
                if _is_decision_id_key(item.name):
                    continue
                if item.name in {"local_functions", "line", "location"}:
                    continue
                visit(getattr(value, item.name), path + ((item.name, index),))
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, path + (index,))

    visit(body, ())
    return contexts


def decision_keys(function: object, *, function_key: object | None = None) -> tuple[str, ...]:
    """Return keys in the function's decision order with stable occurrence context."""

    records = _decision_records(function)
    contexts = _decision_contexts(function)
    seen: dict[str, int] = {}
    result: list[str] = []
    for decision, _ in records:
        identifier = getattr(decision, "id", None)
        if isinstance(decision, Mapping):
            identifier = decision.get("id", decision.get("decision_id"))
        if isinstance(decision, ET.Element):
            identifier = decision.get("id", decision.get("decision_id"))
        try:
            structural = contexts.get(int(identifier)) if identifier not in {None, ""} else None
        except (TypeError, ValueError):
            structural = None
        payload_without_occurrence = decision_identity_payload(decision, context=structural, occurrence=0)
        signature = canonical_json(payload_without_occurrence)
        occurrence = seen.get(signature, 0)
        seen[signature] = occurrence + 1
        result.append(decision_key(decision, context=structural, occurrence=occurrence, function_key=function_key))
    return tuple(result)


decision_identities = decision_keys


def decision_key_map(function: object, *, function_key: object | None = None) -> dict[int, str]:
    """Map each concrete decision record in a function to its stable key.

    Integer analysis IDs are intentionally absent from semantic identities and can
    be reused by a parser.  The map is therefore keyed by object identity and is
    aligned with the function's ordered decision records, which supplies the stable
    occurrence disambiguator for otherwise identical decisions.
    """

    records = _decision_records(function)
    keys = decision_keys(function, function_key=function_key)
    return {id(record[0]): key for record, key in zip(records, keys)}


def decision_key_for(
    function: object,
    decision: object,
    *,
    function_key: object | None = None,
) -> str:
    """Resolve a decision key in its containing function's occurrence scope."""

    mapped = decision_key_map(function, function_key=function_key).get(id(decision))
    if mapped is not None:
        return mapped
    # A copied/mapped decision may not be the same object as the function record;
    # preserve a safe standalone key instead of inventing a transient ID.
    return decision_key(decision, function_key=function_key)


def _atom_value(atom: object) -> Any:
    return _normalize_value(atom, unsupported=[])


def atom_key(
    atom: object,
    *,
    occurrence: object = 0,
    decision: object | None = None,
    occurrence_index: object | None = None,
) -> str:
    if occurrence_index is not None:
        occurrence = occurrence_index
    payload = {"atom": _atom_value(atom), "occurrence": max(0, int(occurrence)) if str(occurrence).lstrip("-").isdigit() else 0}
    # Atom occurrence is already scoped by the containing decision obligation.  Do
    # not add the transient/object-derived decision payload here: callers creating an
    # atom key directly and callers creating it through obligations_for_decision must
    # receive the same stable value.
    return _hash_payload(payload, prefix="a_")


make_atom_key = atom_key


def _atoms(decision: object) -> tuple[object, ...]:
    try:
        from utgen.model import atoms_of

        _kind, expression, _guards = _decision_data(decision)
        # atoms_of understands the legacy IR; XML/mapping expressions are handled by
        # the generic fallback below.
        values = atoms_of(expression)
        return tuple(values)
    except (ImportError, AttributeError, TypeError, KeyError):
        pass
    if isinstance(decision, ET.Element):
        return tuple(child for child in decision.iter() if _local_tag(child.tag).lower() == "atom")
    return ()


def atom_keys(decision: object) -> tuple[str, ...]:
    return tuple(atom_key(atom, occurrence=index) for index, atom in enumerate(_atoms(decision)))


def logical_argument_key(argument: object, *, function_key: object | None = None) -> str:
    """Return stable logical argument identity, independent of Excel/XML position."""

    payload = {"argument": _normalize_value(argument, unsupported=[])}
    if function_key is not None:
        payload["function"] = _normalize_text(function_key, source_text=False)
    return _hash_payload(payload, prefix="arg_")


make_logical_argument_key = logical_argument_key


def _decision_key_from_value(value: object, *, function_key: object | None = None) -> str:
    if isinstance(value, str) and (value.startswith("d_") or value.startswith("decision_")):
        return value
    return decision_key(value, function_key=function_key)


def _atom_key_from_value(value: object, *, decision: object | None = None, occurrence: object = 0) -> str:
    if isinstance(value, str) and (value.startswith("a_") or value.startswith("atom_")):
        return value
    return atom_key(value, occurrence=occurrence, decision=decision)


def obligation_key(
    kind: str,
    decision: object | None = None,
    atom: object | None = None,
    outcome: object | None = None,
    *,
    decision_key_value: object | None = None,
    atom_key_value: object | None = None,
    logical_argument: object | None = None,
    logical_argument_key_value: object | None = None,
    function_key: object | None = None,
    occurrence: object = 0,
    **aliases: object,
) -> str:
    """Build a stable structured generator objective key.

    Supported forms mirror the specification examples, while keyword spellings make
    the helper convenient for generator code.  Unknown objective kinds are rejected
    rather than silently creating an unmatchable row provenance key.
    """

    # Accept the short names used in design notes in addition to the explicit
    # ``*_value`` spellings (the latter avoid shadowing decision_key()/atom_key()).
    if decision_key_value is None:
        decision_key_value = aliases.pop("decision_key", aliases.pop("decision_id_key", None))
    if atom_key_value is None:
        atom_key_value = aliases.pop("atom_key", aliases.pop("atom_id_key", None))
    if logical_argument_key_value is None:
        logical_argument_key_value = aliases.pop("argument_key", aliases.pop("logical_arg_key", None))
    if aliases:
        unknown = ", ".join(sorted(str(key) for key in aliases))
        raise TypeError(f"unknown obligation_key arguments: {unknown}")
    objective = _normalize_text(kind, source_text=False).lower().replace("_", "-")
    if objective in {"branch", "decision"}:
        if decision_key_value is None:
            if decision is None:
                raise FingerprintError("branch obligation requires a decision")
            decision_key_value = _decision_key_from_value(decision, function_key=function_key)
        value = _normalize_text(outcome, source_text=False).lower() if isinstance(outcome, str) else str(bool(outcome)).lower()
        if value not in {"true", "false"}:
            raise FingerprintError("branch outcome must be true or false")
        return f"branch:{decision_key_value}:{value}"

    if objective in {"mcdc", "mc/dc", "condition"}:
        if decision_key_value is None:
            if decision is None:
                raise FingerprintError("MC/DC obligation requires a decision")
            decision_key_value = _decision_key_from_value(decision, function_key=function_key)
        if atom_key_value is None:
            if atom is None:
                raise FingerprintError("MC/DC obligation requires an atom")
            atom_key_value = _atom_key_from_value(atom, decision=decision, occurrence=occurrence)
        if objective == "condition":
            prefix = "condition"
        else:
            prefix = "mcdc"
        if outcome is None:
            return f"{prefix}:{decision_key_value}:{atom_key_value}"
        value = _normalize_text(outcome, source_text=False).lower() if isinstance(outcome, str) else str(bool(outcome)).lower()
        return f"{prefix}:{decision_key_value}:{atom_key_value}:{value}"

    if objective in {"boundary", "bound"}:
        if decision_key_value is None:
            if decision is None:
                raise FingerprintError("boundary obligation requires a decision")
            decision_key_value = _decision_key_from_value(decision, function_key=function_key)
        if atom_key_value is None:
            if atom is None:
                raise FingerprintError("boundary obligation requires an atom")
            atom_key_value = _atom_key_from_value(atom, decision=decision, occurrence=occurrence)
        if outcome is None:
            raise FingerprintError("boundary obligation requires an offset/outcome")
        offset = _normalize_text(outcome, source_text=False)
        # Boundary generators historically formatted the zero offset with an
        # explicit sign (``+0``).  Zero has one canonical identity in the
        # structured obligation grammar, so accept both spellings while
        # normalizing ``+0``/``-0`` to ``0``.  Likewise, ``1`` is the unsigned
        # spelling of the positive boundary step.
        if offset in {"-1", "0", "+0", "-0", "+1", "1"}:
            if offset in {"+0", "-0"}:
                offset = "0"
            elif offset == "1":
                offset = "+1"
        else:
            raise FingerprintError("boundary offset must be -1, 0, or +1")
        return f"boundary:{decision_key_value}:{atom_key_value}:{offset}"

    if objective in {"fault", "fault-injection", "faultinjection"}:
        if logical_argument_key_value is None:
            if logical_argument is None:
                raise FingerprintError("fault obligation requires a logical argument")
            logical_argument_key_value = logical_argument_key(logical_argument, function_key=function_key)
        value = _normalize_text("" if outcome is None else outcome, source_text=False)
        if not value:
            raise FingerprintError("fault obligation requires a fault value")
        return f"fault:{logical_argument_key_value}:{value}"

    raise FingerprintError(f"unsupported obligation kind: {kind!r}")


make_obligation_key = obligation_key
normalized_obligation_key = obligation_key
obligation_identity = obligation_key
obligation_id = obligation_key


def obligations_for_decision(
    decision: object,
    *,
    function: object | None = None,
    function_key: object | None = None,
    context: object | None = None,
    occurrence: object = 0,
    include_condition_outcomes: bool = True,
) -> tuple[str, ...]:
    d_key = (
        decision_key_for(function, decision, function_key=function_key)
        if function is not None
        else decision_key(decision, context=context, occurrence=occurrence, function_key=function_key)
    )
    result = [obligation_key("branch", decision_key_value=d_key, outcome=value) for value in (False, True)]
    atoms = _atoms(decision)
    for index, atom in enumerate(atoms):
        a_key = atom_key(atom, occurrence=index)
        if include_condition_outcomes:
            result.extend(
                obligation_key("condition", decision_key_value=d_key, atom_key_value=a_key, outcome=value)
                for value in (False, True)
            )
        if getattr(atom, "var", None) is not None or isinstance(atom, ET.Element):
            result.append(obligation_key("mcdc", decision_key_value=d_key, atom_key_value=a_key))
    return tuple(sorted(set(result)))


def obligation_keys_for_decision(*args, **kwargs) -> tuple[str, ...]:
    return obligations_for_decision(*args, **kwargs)


def _decision_lookup(function: object) -> dict[int, object]:
    result: dict[int, object] = {}
    for decision in _decision_records(function):
        item = decision[0]
        value = getattr(item, "id", None)
        if isinstance(item, Mapping):
            value = item.get("id", item.get("decision_id"))
        if isinstance(item, ET.Element):
            value = item.get("id", item.get("decision_id"))
        try:
            if value is not None:
                result[int(value)] = item
        except (TypeError, ValueError):
            continue
    return result


def obligation_keys_for_intent(
    function: object,
    intent: Mapping[object, object] | None = None,
    *,
    kind: str | None = None,
    atom: object | None = None,
    boundary_offset: object | None = None,
    fault_argument: object | None = None,
    fault_value: object | None = None,
) -> tuple[str, ...]:
    """Translate legacy integer decision intent to stable obligation keys."""

    intent = intent or {}
    lookup = _decision_lookup(function)
    stable_keys = decision_key_map(function)
    result: set[str] = set()
    for raw_id, outcome in intent.items():
        try:
            identifier = int(raw_id)
        except (TypeError, ValueError):
            continue
        decision = lookup.get(identifier)
        if decision is None:
            continue
        result.add(obligation_key(
            "branch",
            decision_key_value=stable_keys.get(id(decision), decision_key(decision)),
            outcome=outcome,
        ))
    if atom is not None and intent:
        # The caller can request an explicit boundary or MC/DC objective while using
        # the same legacy decision intent dictionary.
        decision = next((lookup.get(int(key)) for key in intent if str(key).lstrip("-").isdigit() and int(key) in lookup), None)
        if decision is not None:
            if boundary_offset is not None:
                result.add(obligation_key(
                    "boundary",
                    decision_key_value=stable_keys.get(id(decision), decision_key(decision)),
                    atom=atom,
                    outcome=boundary_offset,
                ))
            elif kind and kind.lower() in {"mcdc", "mc/dc"}:
                result.add(obligation_key(
                    "mcdc",
                    decision_key_value=stable_keys.get(id(decision), decision_key(decision)),
                    atom=atom,
                ))
    if fault_argument is not None and fault_value is not None:
        result.add(obligation_key("fault", logical_argument=fault_argument, outcome=fault_value))
    return tuple(sorted(result))


def merge_obligation_keys(*values: object) -> tuple[str, ...]:
    """Union row obligation sets deterministically, retaining every objective."""

    result: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, Mapping):
            value = value.get("obligation_keys", ())
        elif hasattr(value, "obligation_keys"):
            value = getattr(value, "obligation_keys")
        if isinstance(value, str):
            result.add(value)
            continue
        try:
            for item in value:  # type: ignore[union-attr]
                if item is not None and str(item):
                    result.add(str(item))
        except TypeError:
            result.add(str(value))
    return tuple(sorted(result))


def explain_component_change(before: object, after: object) -> ComponentChangeReport:
    """Explain which normalized fingerprint components changed.

    Inputs may be ``FingerprintDetails`` instances, detail mappings, or arbitrary
    component mappings.  A raw string comparison is still reported as a generic
    fingerprint change rather than disappearing into a boolean.
    """

    def parts(value: object) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
        if isinstance(value, FingerprintDetails):
            return value.components, value.fingerprint, value.confidence
        if isinstance(value, Mapping):
            components = value.get("components")
            if isinstance(components, Mapping):
                return components, value.get("fingerprint", value.get("value")), value.get("confidence")
            # A plain mapping is itself a component mapping.
            return value, value.get("fingerprint", value.get("value")), value.get("confidence")
        return None, str(value), None

    left, left_hash, left_conf = parts(before)
    right, right_hash, right_conf = parts(after)
    report = ComponentChangeReport()
    changed: list[str] = []
    if left is not None and right is not None:
        names = sorted(set(left) | set(right))
        for name in names:
            old = left.get(name)
            new = right.get(name)
            is_changed = old != new
            report[name] = {"before": old, "after": new, "changed": is_changed}
            if is_changed:
                changed.append(name)
    else:
        is_changed = left_hash != right_hash
        report["fingerprint"] = {"before": left_hash, "after": right_hash, "changed": is_changed}
        if is_changed:
            changed.append("fingerprint")
    report["changed_components"] = tuple(changed)
    report["before_confidence"] = left_conf
    report["after_confidence"] = right_conf
    report["conservative"] = left_conf == FingerprintConfidence.LOW.value or right_conf == FingerprintConfidence.LOW.value
    return report


explain_component_changes = explain_component_change
explain_fingerprint_change = explain_component_change
explain_change = explain_component_change
fingerprint_explanation = explain_component_change


def fingerprint_changed(before: object, after: object) -> bool:
    report = explain_component_change(before, after)
    return bool(report.changed_components)


__all__ = [
    "DEFAULT_GENERATOR_SCHEMA",
    "DEFAULT_GENERATOR_VERSION",
    "FINGERPRINT_SCHEMA_VERSION",
    "ComponentChangeReport",
    "FingerprintConfidence",
    "FingerprintDetails",
    "FingerprintError",
    "FingerprintExplanation",
    "FingerprintResult",
    "analysis_fingerprint",
    "fingerprint_analysis",
    "atom_key",
    "atom_keys",
    "canonical_fingerprint_payload",
    "canonical_json",
    "compute_config_fingerprint",
    "compute_dependency_fingerprint",
    "compute_function_fingerprint",
    "config_fingerprint",
    "decision_identity_payload",
    "decision_identity",
    "decision_identities",
    "decision_key",
    "decision_key_for",
    "decision_key_map",
    "decision_keys",
    "dependency_fingerprint",
    "dependency_fingerprint_details",
    "dependency_fingerprint_result",
    "explain_change",
    "explain_component_change",
    "explain_component_changes",
    "explain_fingerprint_change",
    "fingerprint_changed",
    "function_fingerprint",
    "function_fingerprint_details",
    "function_fingerprint_result",
    "logical_argument_key",
    "make_atom_key",
    "make_decision_key",
    "make_logical_argument_key",
    "make_obligation_key",
    "obligation_id",
    "obligation_identity",
    "merge_obligation_keys",
    "normalized_analysis",
    "normalized_decision_key",
    "normalized_obligation_key",
    "normalize_analysis",
    "normalize_db",
    "normalize_path",
    "normalize_source_analysis",
    "normalized_subtree",
    "normalized_db",
    "obligation_key",
    "obligation_keys_for_decision",
    "obligation_keys_for_intent",
    "obligations_for_decision",
    "fingerprint_explanation",
]
