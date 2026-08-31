"""Stub-analysis XML adapter for PR06.

Stub XML is intentionally kept separate from value XML.  It describes source
call signatures and synthetic return/argument storage names; arbitrary TESSY
``Stub Body`` text belongs to iteration state and is not fabricated here.  The
adapter nevertheless preserves every called-function child/attribute so an
analysis XML round-trip cannot silently lose a payload that this version does
not understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from ._xml_common import (
    LegacyXmlMetadataError,
    XmlAdapterError,
    XmlMetadata,
    XmlMetadataError,
    XmlParseError,
    VersionMetadataMismatchError,
    clone_element,
    coerce_metadata,
    local_attributes,
    parse_metadata,
    parse_xml_root,
    serialize_xml,
    set_root_metadata,
    validate_metadata_pair,
)
from ..model import DisplayVersion


class StubXmlError(XmlAdapterError):
    """Malformed or unsupported stub-analysis XML."""


class StubXmlMetadataError(XmlMetadataError, StubXmlError):
    """Missing, invalid, or incomplete stub-XML version metadata."""


def _raw_signature_value(
    element: ET.Element | None,
    *,
    attribute: str,
    child: str,
) -> str | None:
    """Return the typed value originally accepted from one signature node.

    Stub XML commonly carries two names for a parameter: the ``name``
    attribute is the source parameter (for example ``Arg0``), while the
    child ``<name>`` is TESSY's synthetic storage name.  Keeping this small
    distinction lets an unchanged snapshot retain the complete raw payload,
    while a caller edit can still be detected and propagated to both known
    representations.
    """

    if element is None:
        return None
    if attribute in element.attrib:
        return element.attrib[attribute]
    child_element = element.find(child)
    return child_element.text if child_element is not None else None


@dataclass
class StubParameter:
    """One captured called-function parameter, with raw compatibility data."""

    type_name: str
    name: str
    attributes: Mapping[str, str] = field(default_factory=dict)
    type_text: str | None = None
    name_text: str | None = None
    _raw_element: ET.Element | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.type_name) is not str or not self.type_name:
            raise StubXmlError("stub parameter type must be a non-empty string")
        if type(self.name) is not str or not self.name:
            raise StubXmlError("stub parameter name must be a non-empty string")
        self.attributes = dict(self.attributes)
        if self._raw_element is None:
            self.attributes.setdefault("type", self.type_name)
            self.attributes.setdefault("name", self.name)
            if self.type_text is None:
                self.type_text = self.type_name
            if self.name_text is None:
                self.name_text = self.name
        else:
            # ``dataclasses.replace`` is the normal immutable-edit idiom for
            # snapshots.  If a typed field was edited, make the diagnostic
            # text field reflect that accepted state too; an unchanged parsed
            # parameter keeps its original child spelling (which can differ
            # from the source ``name`` attribute).
            original_type = _raw_signature_value(
                self._raw_element, attribute="type", child="type"
            )
            original_name = _raw_signature_value(
                self._raw_element, attribute="name", child="name"
            )
            if self.type_name != original_type:
                self.type_text = self.type_name
            if self.name != original_name:
                self.name_text = self.name

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "type": self.type_name,
            "name": self.name,
            "attributes": dict(self.attributes),
            "type_text": self.type_text,
            "name_text": self.name_text,
        }

    @property
    def extra_attributes(self) -> dict[str, str]:
        return {key: value for key, value in self.attributes.items() if key not in {"type", "name"}}

    @property
    def raw_attributes(self) -> dict[str, str]:
        return dict(self.attributes)


@dataclass
class StubReturn:
    """Return signature and synthetic storage name from a ``<return>`` node."""

    type_name: str | None
    name: str | None
    attributes: Mapping[str, str] = field(default_factory=dict)
    type_text: str | None = None
    name_text: str | None = None
    _raw_element: ET.Element | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.attributes = dict(self.attributes)
        if self._raw_element is None:
            if self.type_text is None:
                self.type_text = self.type_name
            if self.name_text is None:
                self.name_text = self.name
        else:
            # Keep diagnostic child text synchronized when this return record
            # is edited through ``dataclasses.replace``.
            original_type = _raw_signature_value(
                self._raw_element, attribute="type", child="type"
            )
            original_name = _raw_signature_value(
                self._raw_element, attribute="name", child="name"
            )
            if self.type_name != original_type:
                self.type_text = self.type_name
            if self.name != original_name:
                self.name_text = self.name

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "type": self.type_name,
            "name": self.name,
            "attributes": dict(self.attributes),
            "type_text": self.type_text,
            "name_text": self.name_text,
        }

    @property
    def raw_attributes(self) -> dict[str, str]:
        return dict(self.attributes)


@dataclass
class CalledFunction:
    """One called-function signature and its complete XML payload."""

    name: str
    file: str = ""
    line: str = ""
    return_type: str | None = None
    return_name: str | None = None
    arguments: tuple[StubParameter, ...] = ()
    attributes: Mapping[str, str] = field(default_factory=dict)
    return_attributes: Mapping[str, str] = field(default_factory=dict)
    return_type_text: str | None = None
    return_name_text: str | None = None
    _raw_element: ET.Element | None = field(default=None, repr=False, compare=False)
    _raw_return: ET.Element | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise StubXmlError("called function name must be a non-empty string")
        if type(self.file) is not str:
            self.file = str(self.file)
        if type(self.line) is not str:
            self.line = str(self.line)
        self.arguments = tuple(self.arguments)
        if not all(isinstance(item, StubParameter) for item in self.arguments):
            raise StubXmlError("called-function arguments must be StubParameter records")
        self.attributes = dict(self.attributes)
        if self._raw_element is None:
            self.attributes.setdefault("name", self.name)
            self.attributes.setdefault("file", self.file)
            self.attributes.setdefault("line", self.line)
        self.return_attributes = dict(self.return_attributes)
        if self._raw_return is not None:
            original_type = _raw_signature_value(
                self._raw_return, attribute="type", child="type"
            )
            original_name = _raw_signature_value(
                self._raw_return, attribute="name", child="name"
            )
            if self.return_type != original_type:
                self.return_type_text = self.return_type
            if self.return_name != original_name:
                self.return_name_text = self.return_name

    @property
    def signature(self) -> tuple[Any, ...]:
        return (
            self.name,
            self.return_type,
            self.return_name,
            tuple((argument.type_name, argument.name) for argument in self.arguments),
        )

    @property
    def return_info(self) -> StubReturn:
        return StubReturn(
            self.return_type,
            self.return_name,
            self.return_attributes,
            self.return_type_text,
            self.return_name_text,
            self._raw_return,
        )

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "attributes": dict(self.attributes),
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "return": self.return_info.payload,
            "arguments": tuple(argument.payload for argument in self.arguments),
        }

    @property
    def raw_payload(self) -> bytes | None:
        return ET.tostring(self._raw_element, encoding="utf-8") if self._raw_element is not None else None

    @property
    def raw_attributes(self) -> dict[str, str]:
        return dict(self.attributes)

    def with_bodyless_signature(
        self,
        *,
        return_type: str | None = None,
        return_name: str | None = None,
        arguments: tuple[StubParameter, ...] | None = None,
    ) -> "CalledFunction":
        return replace(
            self,
            return_type=return_type if return_type is not None else self.return_type,
            return_name=return_name if return_name is not None else self.return_name,
            arguments=self.arguments if arguments is None else arguments,
        )


@dataclass
class StubSnapshot:
    """Normalized ``function_analysis`` XML for one test object."""

    function_name: str
    file: str = ""
    line: str = ""
    called_functions: tuple[CalledFunction, ...] = ()
    attributes: Mapping[str, str] = field(default_factory=dict)
    display_version: DisplayVersion | None = None
    root_tag: str = "function_analysis"
    legacy_metadata: bool = False
    _raw_root: ET.Element | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.function_name) is not str or not self.function_name:
            raise StubXmlError("stub XML function_name must be a non-empty string")
        if type(self.file) is not str:
            self.file = str(self.file)
        if type(self.line) is not str:
            self.line = str(self.line)
        self.called_functions = tuple(self.called_functions)
        if not all(isinstance(item, CalledFunction) for item in self.called_functions):
            raise StubXmlError("called_functions must contain CalledFunction records")
        self.attributes = dict(self.attributes)
        if self._raw_root is None:
            self.attributes.setdefault("function_name", self.function_name)
            self.attributes.setdefault("file", self.file)
            self.attributes.setdefault("line", self.line)
        if self.display_version is not None and not isinstance(self.display_version, DisplayVersion):
            if isinstance(self.display_version, XmlMetadata):
                self.display_version = self.display_version.display_version
            else:
                raise StubXmlError("display_version must be a DisplayVersion")
        if self.root_tag != "function_analysis":
            raise StubXmlError("stub snapshots must use the <function_analysis> root")

    @property
    def metadata(self) -> XmlMetadata | None:
        return XmlMetadata(self.display_version) if self.display_version is not None else None

    @property
    def version(self) -> str | None:
        return self.metadata.version if self.metadata is not None else None

    @property
    def updated_at(self) -> str | None:
        return self.metadata.updated_at if self.metadata is not None else None

    @property
    def utgen_version(self) -> str | None:
        return self.version

    @property
    def utgen_updated_at(self) -> str | None:
        return self.updated_at

    @property
    def root_attributes(self) -> dict[str, str]:
        return dict(self.attributes)

    @property
    def attrs(self) -> dict[str, str]:
        return self.root_attributes

    @property
    def raw_attributes(self) -> dict[str, str]:
        return self.root_attributes

    @property
    def raw_root(self) -> ET.Element | None:
        return clone_element(self._raw_root)

    @property
    def raw_xml(self) -> bytes | None:
        return ET.tostring(self._raw_root, encoding="utf-8") if self._raw_root is not None else None

    @property
    def called_functions_count(self) -> int:
        return len(self.called_functions)

    @property
    def payload(self) -> tuple[dict[str, Any], ...]:
        return tuple(function.payload for function in self.called_functions)

    def with_metadata(self, display_version: DisplayVersion) -> "StubSnapshot":
        return replace(self, display_version=display_version, legacy_metadata=False)


def _legacy_opt_in(
    adapter_default: bool,
    allow_legacy: bool | None,
    *,
    bootstrap: bool = False,
    migration: bool = False,
    allow_legacy_metadata: bool = False,
    legacy: bool = False,
    mode: str | None = None,
) -> bool:
    if mode is not None and mode not in {"bootstrap", "migration", "legacy", "strict"}:
        raise StubXmlError(f"unsupported XML read mode {mode!r}")
    return bool(
        adapter_default
        or allow_legacy
        or bootstrap
        or migration
        or allow_legacy_metadata
        or legacy
        or mode in {"bootstrap", "migration", "legacy"}
    )


def _element_text(parent: ET.Element | None, name: str) -> str | None:
    if parent is None:
        return None
    child = parent.find(name)
    return child.text if child is not None else None


def _parse_parameter(element: ET.Element) -> StubParameter:
    attrs = local_attributes(element)
    type_text = _element_text(element, "type")
    name_text = _element_text(element, "name")
    type_name = attrs.get("type", type_text)
    name = attrs.get("name", name_text)
    if not type_name or not name:
        raise StubXmlError("stub argument must provide type and name metadata")
    return StubParameter(
        type_name=type_name,
        name=name,
        attributes=attrs,
        type_text=type_text,
        name_text=name_text,
        _raw_element=clone_element(element),
    )


def _parse_called_function(element: ET.Element) -> CalledFunction:
    attrs = local_attributes(element)
    name = attrs.get("name")
    if not name:
        raise StubXmlError("called function is missing name")
    return_element = element.find("return")
    return_attrs = local_attributes(return_element) if return_element is not None else {}
    return_type_text = _element_text(return_element, "type")
    return_name_text = _element_text(return_element, "name")
    return_type = return_attrs.get("type", return_type_text)
    return_name = return_attrs.get("name", return_name_text)
    arguments_parent = element.find("arguments")
    arguments = (
        tuple(_parse_parameter(item) for item in arguments_parent.findall("argument"))
        if arguments_parent is not None
        else ()
    )
    return CalledFunction(
        name=name,
        file=attrs.get("file", ""),
        line=attrs.get("line", ""),
        return_type=return_type,
        return_name=return_name,
        arguments=arguments,
        attributes=attrs,
        return_attributes=return_attrs,
        return_type_text=return_type_text,
        return_name_text=return_name_text,
        _raw_element=clone_element(element),
        _raw_return=clone_element(return_element),
    )


def _set_text_child(parent: ET.Element, tag: str, value: str | None) -> None:
    child = parent.find(tag)
    if value is None:
        if child is not None:
            parent.remove(child)
        return
    if child is None:
        child = ET.Element(tag)
        parent.append(child)
    child.text = value


def _sync_signature_child(
    parent: ET.Element,
    *,
    tag: str,
    typed_value: str | None,
    original_value: str | None,
    raw_parent: ET.Element | None,
) -> None:
    """Update a known child only when its accepted typed value was edited."""

    child = parent.find(tag)
    if child is None:
        # A direct constructor has no raw payload to preserve.  For a parsed
        # attr-only/non-standard node, add the child only when the caller
        # actually changed the typed value.
        if raw_parent is None or typed_value != original_value:
            _set_text_child(parent, tag, typed_value)
        return
    if raw_parent is None or typed_value != original_value:
        if typed_value is None:
            parent.remove(child)
        else:
            child.text = typed_value


def _render_parameter(parameter: StubParameter) -> ET.Element:
    element = clone_element(parameter._raw_element)
    if element is None:
        element = ET.Element("argument")
    attrs = dict(parameter.attributes)
    original_type = _raw_signature_value(
        parameter._raw_element, attribute="type", child="type"
    )
    original_name = _raw_signature_value(
        parameter._raw_element, attribute="name", child="name"
    )
    if parameter._raw_element is None or "type" in attrs:
        attrs["type"] = parameter.type_name
    if parameter._raw_element is None or "name" in attrs:
        attrs["name"] = parameter.name
    element.attrib.clear()
    element.attrib.update(attrs)
    # ``type_name`` and ``name`` are the accepted typed fields.  The
    # ``*_text`` members retain the original spelling for diagnostics, but
    # must never overwrite a caller's typed edit on render.  Preserve an
    # unusual missing child when the original raw payload did not have one.
    _sync_signature_child(
        element,
        tag="type",
        typed_value=parameter.type_name,
        original_value=original_type,
        raw_parent=parameter._raw_element,
    )
    _sync_signature_child(
        element,
        tag="name",
        typed_value=parameter.name,
        original_value=original_name,
        raw_parent=parameter._raw_element,
    )
    return element


def _render_called_function(function: CalledFunction) -> ET.Element:
    element = clone_element(function._raw_element)
    if element is None:
        element = ET.Element("function")
    attrs = dict(function.attributes)
    attrs["name"] = function.name
    if function._raw_element is None or "file" in attrs:
        attrs["file"] = function.file
    if function._raw_element is None or "line" in attrs:
        attrs["line"] = function.line
    element.attrib.clear()
    element.attrib.update(attrs)

    return_element = element.find("return")
    if function.return_type is not None or function.return_name is not None:
        if return_element is None:
            return_element = ET.Element("return")
            # Preserve the conventional placement before <arguments>.
            arguments_element = element.find("arguments")
            if arguments_element is None:
                element.append(return_element)
            else:
                position = list(element).index(arguments_element)
                element.insert(position, return_element)
        original_return_type = _raw_signature_value(
            function._raw_return, attribute="type", child="type"
        )
        original_return_name = _raw_signature_value(
            function._raw_return, attribute="name", child="name"
        )
        return_attributes = dict(function.return_attributes)
        if (
            "type" in return_attributes
            and function.return_type is not None
            and function.return_type != original_return_type
        ):
            return_attributes["type"] = function.return_type
        if (
            "name" in return_attributes
            and function.return_name is not None
            and function.return_name != original_return_name
        ):
            return_attributes["name"] = function.return_name
        return_element.attrib.clear()
        return_element.attrib.update(return_attributes)
        # Existing child placement and text are retained for an unchanged
        # snapshot.  Typed edits, however, update the corresponding child so
        # a stale raw payload cannot win over accepted signature state.
        _sync_signature_child(
            return_element,
            tag="type",
            typed_value=function.return_type,
            original_value=original_return_type,
            raw_parent=function._raw_return,
        )
        _sync_signature_child(
            return_element,
            tag="name",
            typed_value=function.return_name,
            original_value=original_return_name,
            raw_parent=function._raw_return,
        )

    rendered_parameters = [
        _render_parameter(parameter)
        for parameter in function.arguments
        if "@address" not in parameter.name
    ]
    arguments_element = element.find("arguments")
    if rendered_parameters:
        if arguments_element is None:
            arguments_element = ET.Element("arguments")
            element.append(arguments_element)
        children: list[ET.Element] = []
        index = 0
        for child in list(arguments_element):
            if child.tag != "argument":
                children.append(child)
                continue
            if index < len(rendered_parameters):
                children.append(rendered_parameters[index])
                index += 1
        children.extend(rendered_parameters[index:])
        arguments_element[:] = children
    elif arguments_element is not None:
        # Keep unknown children in an existing arguments container, but remove
        # known argument records represented by an empty normalized signature.
        arguments_element[:] = [child for child in list(arguments_element) if child.tag != "argument"]
    return element


class StubXmlAdapter:
    """Read/render called-function signatures with strict root metadata."""

    def __init__(self, *, allow_legacy: bool = False) -> None:
        self.allow_legacy = bool(allow_legacy)

    def read(
        self,
        value: Any,
        *,
        allow_legacy: bool | None = None,
        bootstrap: bool = False,
        migration: bool = False,
        allow_legacy_metadata: bool = False,
        legacy: bool = False,
        mode: str | None = None,
    ) -> StubSnapshot:
        legacy_ok = _legacy_opt_in(
            self.allow_legacy,
            allow_legacy,
            bootstrap=bootstrap,
            migration=migration,
            allow_legacy_metadata=allow_legacy_metadata,
            legacy=legacy,
            mode=mode,
        )
        try:
            root = parse_xml_root(value, "function_analysis")
        except XmlParseError as exc:
            raise StubXmlError(str(exc)) from exc
        try:
            metadata = parse_metadata(root.attrib, allow_legacy=legacy_ok)
        except XmlMetadataError as exc:
            raise StubXmlMetadataError(str(exc)) from exc
        function_name = root.attrib.get("function_name")
        if not function_name:
            raise StubXmlError("stub XML root is missing function_name")
        called_parent = root.find("called_functions")
        functions = (
            tuple(_parse_called_function(item) for item in called_parent.findall("function"))
            if called_parent is not None
            else ()
        )
        return StubSnapshot(
            function_name=function_name,
            file=root.attrib.get("file", ""),
            line=root.attrib.get("line", ""),
            called_functions=functions,
            attributes=local_attributes(root),
            display_version=metadata.display_version if metadata is not None else None,
            legacy_metadata=metadata is None,
            _raw_root=clone_element(root),
        )

    def read_legacy(self, value: Any, **kwargs: Any) -> StubSnapshot:
        """Explicit migration/bootstrap spelling for legacy XML."""

        kwargs["allow_legacy"] = True
        return self.read(value, **kwargs)

    def render(
        self,
        snapshot: StubSnapshot,
        *,
        display_version: DisplayVersion | XmlMetadata | str | None = None,
        version: DisplayVersion | XmlMetadata | str | None = None,
        updated_at: str | None = None,
        metadata: DisplayVersion | XmlMetadata | None = None,
    ) -> bytes:
        if not isinstance(snapshot, StubSnapshot):
            raise TypeError("snapshot must be a StubSnapshot")
        if metadata is not None:
            if display_version is not None or version is not None:
                raise StubXmlMetadataError("metadata cannot be combined with display_version/version")
            display_version = metadata
        try:
            metadata = coerce_metadata(
                snapshot.metadata,
                display_version=display_version,
                version=version,
                updated_at=updated_at,
            )
        except (XmlMetadataError, TypeError) as exc:
            raise StubXmlMetadataError(str(exc)) from exc

        root = clone_element(snapshot._raw_root)
        if root is None:
            root = ET.Element("function_analysis")
        root.tag = "function_analysis"
        root.attrib.clear()
        root.attrib.update(dict(snapshot.attributes))
        root.set("function_name", snapshot.function_name)
        if snapshot._raw_root is None or "file" in snapshot.attributes:
            root.set("file", snapshot.file)
        if snapshot._raw_root is None or "line" in snapshot.attributes:
            root.set("line", snapshot.line)
        set_root_metadata(root, metadata)

        rendered_functions = [_render_called_function(function) for function in snapshot.called_functions]
        called_parent = root.find("called_functions")
        if called_parent is None:
            called_parent = ET.Element("called_functions")
            root.append(called_parent)
        called_parent.set("count", str(len(rendered_functions)))
        children: list[ET.Element] = []
        index = 0
        for child in list(called_parent):
            if child.tag != "function":
                children.append(child)
                continue
            if index < len(rendered_functions):
                children.append(rendered_functions[index])
                index += 1
        children.extend(rendered_functions[index:])
        called_parent[:] = children
        payload = serialize_xml(root)
        try:
            ET.fromstring(payload)
        except ET.ParseError as exc:  # pragma: no cover - serializer invariant
            raise StubXmlError("rendered stub XML is malformed") from exc
        if b"@address" in payload:
            raise StubXmlError("internal @address argument leaked into stub XML")
        return payload

    def write(
        self,
        snapshot: StubSnapshot,
        path: Any,
        *,
        display_version: DisplayVersion | XmlMetadata | str | None = None,
        version: DisplayVersion | XmlMetadata | str | None = None,
        updated_at: str | None = None,
        metadata: DisplayVersion | XmlMetadata | None = None,
    ) -> None:
        self.write_file(
            snapshot,
            path,
            display_version=display_version,
            version=version,
            updated_at=updated_at,
            metadata=metadata,
        )

    def write_file(
        self,
        snapshot: StubSnapshot,
        path: Any,
        *,
        display_version: DisplayVersion | XmlMetadata | str | None = None,
        version: DisplayVersion | XmlMetadata | str | None = None,
        updated_at: str | None = None,
        metadata: DisplayVersion | XmlMetadata | None = None,
    ) -> None:
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            self.render(
                snapshot,
                display_version=display_version,
                version=version,
                updated_at=updated_at,
                metadata=metadata,
            )
        )

def read_stub_xml(value: Any, **kwargs: Any) -> StubSnapshot:
    return StubXmlAdapter().read(value, **kwargs)


def read(value: Any, **kwargs: Any) -> StubSnapshot:
    return read_stub_xml(value, **kwargs)


def render_stub_xml(
    snapshot: StubSnapshot,
    *,
    display_version: DisplayVersion | XmlMetadata | str | None = None,
    version: DisplayVersion | XmlMetadata | str | None = None,
    updated_at: str | None = None,
    metadata: DisplayVersion | XmlMetadata | None = None,
) -> bytes:
    return StubXmlAdapter().render(
        snapshot,
        display_version=display_version,
        version=version,
        updated_at=updated_at,
        metadata=metadata,
    )


def render(
    snapshot: StubSnapshot,
    *,
    display_version: DisplayVersion | XmlMetadata | str | None = None,
    version: DisplayVersion | XmlMetadata | str | None = None,
    updated_at: str | None = None,
    metadata: DisplayVersion | XmlMetadata | None = None,
) -> bytes:
    return render_stub_xml(
        snapshot,
        display_version=display_version,
        version=version,
        updated_at=updated_at,
        metadata=metadata,
    )


__all__ = [
    "CalledFunction",
    "LegacyXmlMetadataError",
    "StubParameter",
    "StubReturn",
    "StubSnapshot",
    "StubXmlAdapter",
    "StubXmlError",
    "StubXmlMetadataError",
    "VersionMetadataMismatchError",
    "XmlAdapterError",
    "XmlMetadata",
    "XmlMetadataError",
    "XmlParseError",
    "read_stub_xml",
    "render_stub_xml",
    "read",
    "render",
    "validate_metadata_pair",
]
