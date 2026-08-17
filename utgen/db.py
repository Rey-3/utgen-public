from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Mapping

from clang.cindex import CursorKind

from utgen.atoms import AtomBuilder, _fold_int
from utgen.clang_setup import configure_libclang
from utgen.dataflow import DataflowEngine
from utgen.decisions import DecisionExtractor
from utgen.includes import IncludeScanner
from utgen.inventory import InventoryBuilder
from utgen.irbuild import BodyIrBuilder
from utgen.parsing import ClangParser, function_definitions
from utgen.stubs import DEFAULT_NO_STUB
from utgen.model import (And, Atom, Binop, Break, CallE, Cast, Const, CType,
                         DataflowPoint, DataflowResult, DataflowState, Decl,
                         Decision, Direction, DoWhile, EnumDef, ExprStmt, For,
                         FunctionIR, GlobalDef, Guard, If, Index, MacroDef,
                         MemberDef, MemberE, ModuleAnalysis, Not, Op, Or,
                         PathConstraint, Phi, Ref, Return, StubInfo, StructDef,
                         Switch, SwitchCase, Ternary, Unary, Unknown, Variable,
                         While, Assign)


class ModuleIndexer:

    def __init__(self, config=None):
        # Store the configuration used during indexing (define marco or include file)
        self.config = config or {}

    def index(self, source: Path, root_path: Path | None = None) -> ModuleAnalysis:
        """
        Index and analyze a C source file.

        Args:
            source : Path to the C source (.c) file that will be indexed and analyzed.

            root_path: Project root path for resolving dependencies.

        Returns:
            ModuleAnalysis:
                The analysis result for the source file, containing the indexed
                module information and discovered code structure.
        """
        source = Path(source)
        root_path = Path(root_path or self._get("include_root", source.parent))
        include_dirs, headers = self._includes(source, root_path)

        configure_libclang(self._get("libclang"))

        tu = ClangParser().parse(source, include_dirs, self._get("defines", ()))
        macros = self._macros(tu)
        inventory_builder = InventoryBuilder(self._get("char_signed", False),
                                             self._get("const_globals", "inject"),
                                             self._get("no_stub", DEFAULT_NO_STUB))

        self._register_globals(tu, inventory_builder)

        functions = []
        for cursor in function_definitions(tu).values():
            functions.append(self._function(cursor, tu, inventory_builder, macros))

        analysis = ModuleAnalysis(str(source), _sha256(source), include_dirs, headers,
                                  macros, list(inventory_builder.type_graph.enums.values()),
                                  list(inventory_builder.type_graph.structs.values()),
                                  self._globals(tu, inventory_builder), functions)
        _set_constants(analysis)
        _set_local_functions(analysis)
        return analysis

    def _function(self, cursor, tu, inventory_builder, macros=None):
        """ This converts one clang function definition into a complete `FunctionIR`"""
        inventory = inventory_builder.build(cursor, tu)
        atom_builder = AtomBuilder(inventory, macros=macros)
        decisions = DecisionExtractor(atom_builder).extract(cursor)
        body = BodyIrBuilder(atom_builder, decisions, macros).build(cursor)
        params = []
        for param in cursor.get_arguments():
            params.append((param.type.spelling, param.spelling))
        function = FunctionIR(cursor.spelling, cursor.location.line,
                              cursor.result_type.spelling, _is_static(cursor), params,
                              inventory.stubs, inventory.variables, decisions,
                              tuple(body))
        DataflowEngine().analyze(function)
        return function

    def _includes(self, source: Path, root_path: Path):
        scanner = IncludeScanner(str(source), str(root_path))
        dirs = []
        configured = self._get("includes", ())
        for directory in configured:
            if directory not in dirs:
                dirs.append(str(directory))
        for directory in scanner.get_unique_include_dirs():
            if directory not in dirs:
                dirs.append(directory)
        headers = []
        for name, path in scanner.resolved.items():
            if path:
                headers.append((name, path, _sha256(Path(path))))
        return dirs, headers

    def _register_globals(self, tu, builder):
        for cursor in _walk(tu.cursor):
            if cursor.kind == CursorKind.ENUM_DECL: builder.type_graph.register(cursor.type)
            if cursor.kind == CursorKind.VAR_DECL and _is_global(cursor):
                builder.type_graph.describe(cursor.type, cursor.spelling)

    def _globals(self, tu, builder):
        """ This creates persistent `GlobalDef` records"""
        result, seen = [], set()
        for cursor in _walk(tu.cursor):
            if cursor.kind != CursorKind.VAR_DECL or not _is_global(cursor):
                continue
            key = (cursor.spelling, str(cursor.location.file or ""))
            if key in seen:
                continue
            seen.add(key)
            member = builder.type_graph.describe(cursor.type, cursor.spelling)
            result.append(GlobalDef(cursor.spelling, member.type_name, member.dims,
                                    _storage(cursor, "extern"), _storage(cursor, "static"),
                                    cursor.type.is_const_qualified(),
                                    cursor.type.is_volatile_qualified(),
                                    str(cursor.location.file or "")))
        return result

    def _macros(self, tu):
        """ macro extraction """
        result, seen = [], set()
        for cursor in _walk(tu.cursor):
            if cursor.kind != CursorKind.MACRO_DEFINITION or cursor.spelling.startswith("__"):
                continue
            macro = _macro(cursor)
            if macro is not None and macro.name not in seen:
                seen.add(macro.name)
                result.append(macro)
        values = {}
        changed = True
        while changed:
            changed = False
            for macro in result:
                if macro.kind == "object" and macro.name not in values:
                    value = _fold_int(macro.raw, values)
                    if value is not None:
                        values[macro.name] = value
                        changed = True
        resolved = []
        for macro in result:
            resolved.append(MacroDef(macro.name, macro.kind, macro.raw,
                                     values.get(macro.name), macro.params, macro.file))
        return resolved

    def _get(self, name, default=None):
        if isinstance(self.config, Mapping):
            return self.config.get(name, default)
        return getattr(self.config, name, default)


class XmlDbWriter:
    """Write a deterministic ``SourceAnalysis`` XML document."""

    def write(self, analysis: ModuleAnalysis, path: Path) -> None:
        """Serialize ``analysis`` to ``path`` using the Task 9 schema."""
        root = ET.Element("SourceAnalysis", file=analysis.file,
                          sha256=analysis.sha256, tool="utgen 0.1")
        includes = ET.SubElement(root, "Includes")
        for directory in analysis.include_dirs:
            ET.SubElement(includes, "Dir", path=str(directory))
        for name, header, digest in analysis.headers:
            ET.SubElement(includes, "Header", name=name, path=header, sha256=digest)
        for macro in analysis.macros:
            self._macro(root, macro)
        for enum in analysis.enums:
            self._enum(root, enum)
        for struct in analysis.structs:
            self._struct(root, struct)
        for variable in analysis.variables:
            self._global(root, variable)
        for function in analysis.functions:
            self._function(root, function)
        _indent_xml(root, space="    ")
        payload = _serialize_xml(root)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'<?xml version="1.0" encoding="utf-8"?>\n' + payload + b"\n")

    def _macro(self, parent, macro):
        attrs = {"name": macro.name, "kind": macro.kind, "raw": macro.raw,
                 "file": macro.file}
        if macro.value is not None:
            attrs["value"] = str(macro.value)
        if macro.params:
            attrs["params"] = macro.params
        ET.SubElement(parent, "Macro", **attrs)

    def _enum(self, parent, enum):
        element = ET.SubElement(parent, "Enum", name=enum.name, file=enum.file)
        for name, value in enum.values:
            ET.SubElement(element, "Value", name=name, value=str(value))

    def _struct(self, parent, struct):
        element = ET.SubElement(parent, "Struct", name=struct.name,
                                kind=struct.kind, file=struct.file)
        for member in struct.members:
            self._member(element, member)

    def _member(self, parent, member):
        attrs = {"name": member.name, "type": member.type_name, "kind": member.kind,
                 "len": str(member.len_bits), "signed": str(member.signed).lower(),
                 "dims": _dims(member.dims), "bitfield": str(member.bitfield).lower(),
                 "const": str(member.const).lower()}
        ET.SubElement(parent, "Member", **attrs)

    def _global(self, parent, variable):
        attrs = {"name": variable.name, "type": variable.type_name,
                 "file": variable.file, "dims": _dims(variable.dims),
                 "extern": str(variable.extern).lower(), "static": str(variable.static).lower(),
                 "const": str(variable.const).lower(), "volatile": str(variable.volatile).lower()}
        ET.SubElement(parent, "Variable", **attrs)

    def _function(self, parent, function):
        attrs = {"name": function.name, "line": str(function.line),
                 "return": function.return_type, "static": str(function.static).lower()}
        element = ET.SubElement(parent, "Function", **attrs)
        for type_name, name in function.params:
            ET.SubElement(element, "Param", name=name, type=type_name)
        for stub in function.stubs:
            self._stub(element, stub)
        for leaf in function.leaves:
            self._leaf(element, leaf)
        for decision in function.decisions:
            self._decision(element, decision)
        body = ET.SubElement(element, "Body")
        for statement in function.body:
            self._statement(body, statement)
        if function.dataflow is not None:
            self._dataflow(element, function.dataflow)

    def _stub(self, parent, stub):
        attrs = {"name": stub.name, "return": stub.ret_struct or ""}
        if stub.ret_ctype is not None:
            attrs.update(type=stub.ret_ctype.name, bits=str(stub.ret_ctype.bits),
                         signed=str(stub.ret_ctype.signed).lower())
            if stub.ret_ctype.enum_values:
                attrs["enum"] = _enum_values(stub.ret_ctype.enum_values)
        element = ET.SubElement(parent, "Stub", **attrs)
        for type_name, name in stub.params:
            ET.SubElement(element, "Param", name=name, type=type_name)

    def _leaf(self, parent, leaf):
        attrs = {"name": leaf.name, "type": leaf.ctype.name, "bits": str(leaf.ctype.bits),
                 "len": leaf.xml_len, "format": leaf.fmt, "direction": str(leaf.direction),
                 "pointer": str(leaf.is_pointer).lower(), "signed": str(leaf.ctype.signed).lower()}
        if leaf.address_of:
            attrs["address_of"] = leaf.address_of
        if leaf.ctype.enum_values:
            attrs["enum"] = _enum_values(leaf.ctype.enum_values)
        ET.SubElement(parent, "Leaf", **attrs)

    def _decision(self, parent, decision):
        attrs = {"id": str(decision.id), "kind": decision.kind,
                 "line": str(_line(decision.location)), "location": decision.location}
        element = ET.SubElement(parent, "Decision", **attrs)
        if decision.guards:
            guards = ET.SubElement(element, "Guards")
            for guard in decision.guards:
                item = ET.SubElement(guards, "Guard", outcome=str(guard.outcome).lower())
                _write_node(item, guard.expr)
        _write_node(element, decision.expr)

    def _statement(self, parent, statement):
        """ It takes a UTGen statement object and writes it into XML."""
        if isinstance(statement, Decl):
            attrs = {"name": statement.name, "scope": statement.scope,
                     "pointer": str(statement.is_pointer).lower()}
            if statement.static_name:
                attrs["static"] = statement.static_name
            element = ET.SubElement(parent, "Decl", **attrs)
            if statement.init is not None:
                _write_expr(element, statement.init)
            return
        if isinstance(statement, Assign):
            attrs = {"op": statement.op}
            if statement.decision_id:
                attrs["decision"] = str(statement.decision_id)
            if isinstance(statement.lhs, Ref):
                attrs.update(lhs=statement.lhs.path, scope=statement.lhs.scope)
                element = ET.SubElement(parent, "Assign", **attrs)
            else:
                element = ET.SubElement(parent, "Assign", **attrs)
                left = ET.SubElement(element, "Lhs")
                _write_expr(left, statement.lhs)
            _write_expr(element, statement.value)
            return
        if isinstance(statement, If):
            element = ET.SubElement(parent, "If", decision=str(statement.decision_id))
            if statement.condition is not None:
                condition = ET.SubElement(element, "Condition")
                _write_node(condition, statement.condition)
            self._block(element, "Then", statement.then)
            self._block(element, "Else", statement.els)
            return
        if isinstance(statement, Switch):
            element = ET.SubElement(parent, "Switch")
            subject = ET.SubElement(element, "Subject")
            _write_expr(subject, statement.subject)
            for case in statement.cases:
                self._statement(element, case)
            return
        if isinstance(statement, For):
            element = ET.SubElement(parent, "For")
            self._block(element, "Init", statement.init)
            condition = ET.SubElement(element, "Condition")
            _write_node(condition, statement.cond)
            if statement.runtime_cond is not None:
                runtime = ET.SubElement(element, "RuntimeCondition")
                _write_expr(runtime, statement.runtime_cond)
            self._block(element, "Body", statement.body)
            self._block(element, "Increment", statement.increment)
            return
        if isinstance(statement, SwitchCase):
            attrs = {"decision": str(statement.decision_id), "labels": _values(statement.labels),
                     "default": str(statement.default).lower()}
            element = ET.SubElement(parent, "Case", **attrs)
            for child in statement.body:
                self._statement(element, child)
            return
        if isinstance(statement, (While, DoWhile)):
            element = ET.SubElement(parent, "DoWhile" if isinstance(statement, DoWhile) else "While")
            condition = ET.SubElement(element, "Condition")
            _write_node(condition, statement.cond)
            if statement.runtime_cond is not None:
                runtime = ET.SubElement(element, "RuntimeCondition")
                _write_expr(runtime, statement.runtime_cond)
            self._block(element, "Body", statement.body)
            return
        if isinstance(statement, ExprStmt):
            element = ET.SubElement(parent, "ExprStmt")
            _write_expr(element, statement.expr)
            return
        if isinstance(statement, Return):
            element = ET.SubElement(parent, "Return")
            if statement.value is not None:
                _write_expr(element, statement.value)
            return
        if isinstance(statement, Break):
            ET.SubElement(parent, "Break")
            return
        ET.SubElement(parent, "Unknown", text=statement.text)

    def _block(self, parent, tag, statements):
        element = ET.SubElement(parent, tag)
        for statement in statements:
            self._statement(element, statement)

    def _dataflow(self, parent, result):
        element = ET.SubElement(parent, "Dataflow")
        for point in result.points:
            item = ET.SubElement(element, "Point", decision=_optional(point.decision_id),
                                 location=point.location)
            for state in point.states:
                _write_state(item, state)
        returns = ET.SubElement(element, "Returns")
        for state, value in result.returns:
            item = ET.SubElement(returns, "ReturnState")
            _write_state(item, state)
            if value is not None:
                output = ET.SubElement(item, "Value")
                _write_expr(output, value)
        writes = ET.SubElement(element, "Writes")
        for name, value in result.writes:
            item = ET.SubElement(writes, "Write", name=str(name))
            _write_node(item, value)


class XmlDbReader:

    def read(self, path: Path) -> ModuleAnalysis:
        root = ET.parse(path).getroot()
        includes = root.find("Includes")
        dirs, headers = [], []
        if includes is not None:
            for item in includes.findall("Dir"):
                dirs.append(item.get("path", ""))
            for item in includes.findall("Header"):
                headers.append((item.get("name", ""), item.get("path", ""), item.get("sha256", "")))
        macros, enums, structs, variables, functions = [], [], [], [], []
        for item in root:
            if item.tag == "Macro":
                macros.append(_read_macro(item))
            elif item.tag == "Enum":
                enums.append(_read_enum(item))
            elif item.tag == "Struct":
                structs.append(_read_struct(item))
            elif item.tag == "Variable":
                variables.append(_read_global(item))
            elif item.tag == "Function":
                functions.append(_read_function(item))
        analysis = ModuleAnalysis(root.get("file", ""), root.get("sha256", ""), dirs, headers,
                                  macros, enums, structs, variables, functions)
        _set_constants(analysis)
        _set_local_functions(analysis)
        _link_local_call_stubs(analysis)
        return analysis


def _link_local_call_stubs(analysis: ModuleAnalysis) -> None:
    """Propagate external inputs used by same-module helper calls."""
    for function in analysis.functions:
        names = _called_names(function.body)
        for name in names:
            helper = analysis.function(name)
            if helper is None or name == function.name:
                continue
            for leaf in helper.leaves:
                if leaf.direction in (Direction.IN, Direction.INOUT) and not any(item.name == leaf.name for item in function.leaves):
                    function.leaves.append(leaf)
            for stub in helper.stubs:
                if not any(item.name == stub.name for item in function.stubs):
                    function.stubs.append(stub)


def _set_constants(analysis: ModuleAnalysis) -> None:
    """Give each function the persisted object-macro and enum values."""
    values = {}
    for macro in analysis.macros:
        if macro.value is not None:
            values[macro.name] = macro.value
    for enum in analysis.enums:
        for name, value in enum.values:
            values[name] = value
    for function in analysis.functions:
        function.constants = values


def _set_local_functions(analysis: ModuleAnalysis) -> None:
    """ This is later used by the interpreter to execute same-module helper calls """
    functions = {function.name: function for function in analysis.functions}
    for function in analysis.functions:
        function.local_functions = functions


def _called_names(statements) -> set[str]:
    """ finds called function names"""
    names = set()
    for statement in statements:
        for node in _nodes(statement):
            if isinstance(node, CallE):
                names.add(node.name)
    return names


def _nodes(node):
    """ recursively walks through the AST/data structure and finds every CallE node """
    if isinstance(node, CallE):
        yield node
    if is_dataclass(node):
        for field in fields(node):
            value = getattr(node, field.name)
            if isinstance(value, (tuple, list)):
                for item in value:
                    yield from _nodes(item)
            elif value is not None:
                yield from _nodes(value)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _walk(cursor):
    """ Clang AST cursor tree. """
    yield cursor
    for child in cursor.get_children():
        yield from _walk(child)


def _is_global(cursor):
    parent = cursor.semantic_parent
    return parent is not None and parent.kind == CursorKind.TRANSLATION_UNIT


def _storage(cursor, wanted):
    # static, extern ...
    storage = getattr(cursor, "storage_class", "")
    return getattr(storage, "name", str(storage)).lower() == wanted


def _is_static(cursor):
    return _storage(cursor, "static")


def _macro(cursor):
    """ return marco details (#define ADD(a, b) ((a) + (b)))"""
    file = str(cursor.location.file or "")
    line = _source_line(file, cursor.location.line)
    match = re.match(r"\s*#define\s+([A-Za-z_]\w*)(\(([^)]*)\))?\s*(.*)$", line)
    if not match:
        return None
    name, params, raw = match.group(1), match.group(3), match.group(4)
    if not raw:
        return None
    kind = "function" if params is not None else "object"
    return MacroDef(name, kind, raw, _fold_int(raw) if kind == "object" else None,
                    params or "", file)


def _source_line(file, line):
    """ reads one specific line from a source file """
    if not file or not line:
        return ""
    try:
        return Path(file).read_text(encoding="utf-8", errors="ignore").splitlines()[line - 1]
    except (OSError, IndexError):
        return ""


def _dims(dims):
    """ convert array dimensions"""
    return ",".join(str(dim) for dim in dims)


def _read_dims(value):
    """ convert dimension to turple"""
    if not value:
        return ()
    result = []
    for item in value.split(","):
        result.append(int(item))
    return tuple(result)


def _enum_values(values):
    """ serialize enum name-value pairs into text."""
    result = []
    for name, value in values:
        result.append(f"{name}:{value}")
    return ";".join(result)


def _read_enum_values(value):
    """ convert serialized enum text back into enum name-value pairs"""
    if not value:
        return ()
    result = []
    for item in value.split(";"):
        name, number = item.rsplit(":", 1)
        result.append((name, int(number)))
    return tuple(result)


def _values(values):
    """ serialize a list of values into text."""
    return ",".join(str(value) for value in values)


def _read_values(value):
    """ convert value in to tuple"""
    if not value:
        return ()
    result = []
    for item in value.split(","):
        try:
            result.append(int(item))
        except ValueError:
            result.append(item)
    return tuple(result)


def _optional(value):
    return "" if value is None else str(value)


def _line(location):
    """ get the source-code line number"""
    match = re.search(r":(\d+)(?::\d+)?$", location or "")
    return match.group(1) if match else "0"


def _bool(value):
    return value.lower() == "true"


def _read_macro(element):
    value = element.get("value")
    return MacroDef(element.get("name", ""), element.get("kind", "object"),
                    element.get("raw", ""), int(value) if value is not None else None,
                    element.get("params", ""), element.get("file", ""))


def _read_enum(element):
    values = []
    for item in element.findall("Value"):
        values.append((item.get("name", ""), int(item.get("value", "0"))))
    return EnumDef(element.get("name", ""), tuple(values), element.get("file", ""))


def _read_member(element):
    return MemberDef(element.get("name", ""), element.get("type", ""),
                     element.get("kind", "unsupported"), int(element.get("len", "0")),
                     _read_dims(element.get("dims", "")), _bool(element.get("signed", "false")),
                     _bool(element.get("bitfield", "false")), _bool(element.get("const", "false")))


def _read_struct(element):
    members = []
    for item in element.findall("Member"):
        members.append(_read_member(item))
    return StructDef(element.get("name", ""), element.get("kind", "struct"),
                     tuple(members), element.get("file", ""))


def _read_global(element):
    return GlobalDef(element.get("name", ""), element.get("type", ""),
                     _read_dims(element.get("dims", "")), _bool(element.get("extern", "false")),
                     _bool(element.get("static", "false")), _bool(element.get("const", "false")),
                     _bool(element.get("volatile", "false")), element.get("file", ""))


def _ctype(element, prefix=""):
    name = element.get(f"{prefix}type", "")
    bits = int(element.get(f"{prefix}bits", "0"))
    signed = _bool(element.get(f"{prefix}signed", "false"))
    enum = _read_enum_values(element.get(f"{prefix}enum", ""))
    return CType(name, bits, signed, enum)


def _read_stub(element):
    ret_type = element.get("type")
    ret_ctype = _ctype(element) if ret_type else None
    params = []
    for item in element.findall("Param"):
        params.append((item.get("type", ""), item.get("name", "")))
    return StubInfo(element.get("name", ""), ret_ctype,
                    element.get("return") or None, params)


def _read_leaf(element):
    ctype = _ctype(element)
    return Variable(element.get("name", ""), ctype, element.get("format", ""),
                    element.get("len", ""), Direction(element.get("direction", "in")),
                    _bool(element.get("pointer", "false")), element.get("address_of", ""))


def _read_function(element):
    params = []

    # read parameters
    for item in element.findall("Param"):
        params.append((item.get("type", ""), item.get("name", "")))

    # Read stubs and leaves
    stubs, leaves = [], []
    for item in element:
        if item.tag == "Stub":
            stubs.append(_read_stub(item))
        elif item.tag == "Leaf":
            leaves.append(_read_leaf(item))

    # Build leaf lookup
    variables = {}
    for leaf in leaves:
        variables[leaf.name] = leaf

    # Read decisions
    decisions = []
    for item in element.findall("Decision"):
        decisions.append(_read_decision(item, variables))

    #  Read body IR
    body_element = element.find("Body")
    body = []
    if body_element is not None:
        for item in body_element:
            body.append(_read_statement(item, variables))

    # Read dataflow
    dataflow_element = element.find("Dataflow")
    dataflow = _read_dataflow(dataflow_element, variables) if dataflow_element is not None else None
    return FunctionIR(element.get("name", ""), int(element.get("line", "0")),
                      element.get("return_type", element.get("return", "")),
                      _bool(element.get("static", "false")), params, stubs, leaves,
                      decisions, tuple(body), dataflow)


def _read_decision(element, variables):
    guards_element = element.find("Guards")
    guards = []
    if guards_element is not None:
        for item in guards_element.findall("Guard"):
            child = item[0] if len(item) else None
            guards.append(Guard(_read_node(child, variables), _bool(item.get("outcome", "false"))))
    expression = None
    for child in element:
        if child.tag != "Guards":
            expression = _read_node(child, variables)
            break
    return Decision(int(element.get("id", "0")), expression, element.get("kind", "if"),
                    tuple(guards), element.get("location", ""))


def _read_statement(element, variables):
    tag = element.tag
    if tag == "Decl":
        init = _read_expr(element[0], variables) if len(element) else None
        return Decl(element.get("name", ""), init, element.get("scope", ""),
                    _bool(element.get("pointer", "false")), element.get("static", ""))
    if tag == "Assign":
        lhs = Ref(element.get("lhs", ""), element.get("scope", "")) if element.get("lhs") is not None else None
        value = None
        for child in element:
            if child.tag == "Lhs":
                lhs = _read_expr(child[0], variables) if len(child) else None
            elif child.tag != "Lhs":
                value = _read_expr(child, variables)
        return Assign(lhs, value, element.get("op", "="), int(element.get("decision", "0")))
    if tag == "If":
        condition = _child_expr(element, "Condition", variables)
        return If(int(element.get("decision", "0")), _read_block(element, "Then", variables),
                  _read_block(element, "Else", variables), condition)
    if tag == "Switch":
        subject = _child_expr(element, "Subject", variables)
        cases = []
        for child in element:
            if child.tag == "Case":
                cases.append(_read_statement(child, variables))
        return Switch(subject, tuple(cases))
    if tag == "For":
        return For(_read_block(element, "Init", variables),
                   _child_expr(element, "Condition", variables),
                   _read_block(element, "Increment", variables),
                   _read_block(element, "Body", variables),
                   _child_expr(element, "RuntimeCondition", variables))
    if tag == "Case":
        body = []
        for child in element:
            body.append(_read_statement(child, variables))
        return SwitchCase(int(element.get("decision", "0")), tuple(body),
                          _read_values(element.get("labels", "")), _bool(element.get("default", "false")))
    if tag in ("While", "DoWhile"):
        return (DoWhile if tag == "DoWhile" else While)(_child_expr(element, "Condition", variables),
                                                          tuple(_read_block(element, "Body", variables)),
                                                          _child_expr(element, "RuntimeCondition", variables))
    if tag == "ExprStmt":
        return ExprStmt(_read_expr(element[0], variables) if len(element) else None)
    if tag == "Return":
        return Return(_read_expr(element[0], variables) if len(element) else None)
    if tag == "Break":
        return Break()
    return Unknown(element.get("text", ""))


def _read_block(parent, tag, variables):
    """ read all statements inside a block"""
    element = parent.find(tag)
    if element is None:
        return ()
    result = []
    for child in element:
        result.append(_read_statement(child, variables))
    return tuple(result)


def _child_expr(parent, tag, variables):
    element = parent.find(tag)
    return _read_node(element[0], variables) if element is not None and len(element) else None


def _write_expr(parent, expression):
    """Write an expression without recursive Python calls."""
    _write_node(parent, expression)


def _write_node(parent, node):
    """Write a node iteratively so deeply nested expressions are supported."""
    stack = [(parent, node)]

    while stack:
        current_parent, current = stack.pop()

        if current is None:
            ET.SubElement(current_parent, "None")
            continue

        if isinstance(current, Atom):
            attrs = {"op": str(current.op), "text": current.text}
            if current.var is not None:
                attrs["var"] = current.var.name
            if current.rhs is not None:
                attrs["rhs"] = str(current.rhs)
            if current.rhs_var is not None:
                attrs["rhs_var"] = current.rhs_var.name
            ET.SubElement(current_parent, "Atom", **attrs)
            continue

        if isinstance(current, (And, Or, Not)):
            tag = "And" if isinstance(current, And) else "Or" if isinstance(current, Or) else "Not"
            element = ET.SubElement(current_parent, tag)
            children = current.children if isinstance(current, (And, Or)) else (current.child,)
            for child in reversed(children):
                stack.append((element, child))
            continue

        if isinstance(current, Ref):
            attrs = {"path": current.path}
            if current.scope:
                attrs["scope"] = current.scope
            ET.SubElement(current_parent, "Ref", **attrs)
            continue

        if isinstance(current, Const):
            ET.SubElement(
                current_parent,
                "Const",
                value=str(current.value),
                type=current.type_name or ("str" if isinstance(current.value, str) else "int"),
            )
            continue

        if isinstance(current, Unary):
            element = ET.SubElement(current_parent, "Unary", op=current.op)
            stack.append((element, current.x))
            continue

        if isinstance(current, Binop):
            element = ET.SubElement(current_parent, "Binop", op=current.op)
            # Stack is LIFO, so push right before left to preserve XML order.
            stack.append((element, current.r))
            stack.append((element, current.l))
            continue

        if isinstance(current, CallE):
            element = ET.SubElement(current_parent, "Call", name=current.name)
            items = [ET.SubElement(element, "Arg") for _ in current.args]
            for item, argument in reversed(list(zip(items, current.args))):
                stack.append((item, argument))
            continue

        if isinstance(current, Ternary):
            attrs = {"decision": str(current.decision_id)} if current.decision_id else {}
            element = ET.SubElement(current_parent, "Ternary", **attrs)
            values = (
                (ET.SubElement(element, "Condition"), current.c),
                (ET.SubElement(element, "Then"), current.t),
                (ET.SubElement(element, "Else"), current.f),
            )
            for item, value in reversed(values):
                stack.append((item, value))
            continue

        if isinstance(current, Cast):
            element = ET.SubElement(current_parent, "Cast", type=current.type_name)
            stack.append((element, current.x))
            continue

        if isinstance(current, Index):
            element = ET.SubElement(current_parent, "Index")
            base = ET.SubElement(element, "Base")
            index = ET.SubElement(element, "Index")
            stack.append((index, current.idx))
            stack.append((base, current.base))
            continue

        if isinstance(current, MemberE):
            element = ET.SubElement(current_parent, "Member", name=current.name)
            stack.append((element, current.base))
            continue

        if isinstance(current, Phi):
            element = ET.SubElement(current_parent, "Phi")
            values = (
                (ET.SubElement(element, "Condition"), current.condition),
                (ET.SubElement(element, "True"), current.when_true),
                (ET.SubElement(element, "False"), current.when_false),
            )
            for item, value in reversed(values):
                stack.append((item, value))
            continue

        # Avoid str(current): dataclass repr/str can itself recurse on a deep tree.
        ET.SubElement(
            current_parent,
            "Unknown",
            text=getattr(current, "text", type(current).__name__),
        )


def _indent_xml(root, space="    ", max_depth=20):
    """Indent an ElementTree iteratively instead of using recursive ET.indent."""
    stack = [(root, 0)]

    while stack:
        element, level = stack.pop()
        children = list(element)
        if not children:
            continue

        child_indent = "\n" + space * min(level + 1, max_depth)
        closing_indent = "\n" + space * min(level, max_depth)

        if not element.text or not element.text.strip():
            element.text = child_indent

        for child in children[:-1]:
            if not child.tail or not child.tail.strip():
                child.tail = child_indent

        last = children[-1]
        if not last.tail or not last.tail.strip():
            last.tail = closing_indent

        for child in reversed(children):
            stack.append((child, level + 1))


def _escape_xml_text(value):
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_xml_attr(value):
    text = _escape_xml_text(value)
    return (text.replace('"', "&quot;")
                .replace("\t", "&#09;")
                .replace("\n", "&#10;")
                .replace("\r", "&#13;"))


def _serialize_xml(root):
    """Serialize XML iteratively, avoiding ElementTree's recursive serializer."""
    parts = []
    stack = [("start", root)]

    while stack:
        action, element = stack.pop()

        if action == "end":
            parts.append(f"</{element.tag}>")
            if element.tail:
                parts.append(_escape_xml_text(element.tail))
            continue

        attrs = "".join(
            f' {name}="{_escape_xml_attr(value)}"'
            for name, value in element.attrib.items()
        )
        children = list(element)
        text = element.text or ""

        if not children and not text:
            parts.append(f"<{element.tag}{attrs} />")
            if element.tail:
                parts.append(_escape_xml_text(element.tail))
            continue

        parts.append(f"<{element.tag}{attrs}>")
        if text:
            parts.append(_escape_xml_text(text))

        stack.append(("end", element))
        for child in reversed(children):
            stack.append(("start", child))

    return "".join(parts).encode("utf-8")

def _read_expr(element, variables):
    return _read_node(element, variables)


def _xml_child_node(parent, tag):
    wrapper = parent.find(tag)
    return wrapper[0] if wrapper is not None and len(wrapper) else None


def _node_children(element):
    """Return the actual child-node elements, excluding XML wrapper tags."""
    tag = element.tag

    if tag in ("Unary", "Cast", "Member", "Not"):
        return [element[0]] if len(element) else []

    if tag == "Binop":
        return list(element[:2])

    if tag == "Call":
        return [item[0] for item in element.findall("Arg") if len(item)]

    if tag == "Ternary":
        return [
            child for child in (
                _xml_child_node(element, "Condition"),
                _xml_child_node(element, "Then"),
                _xml_child_node(element, "Else"),
            ) if child is not None
        ]

    if tag == "Index":
        return [
            child for child in (
                _xml_child_node(element, "Base"),
                _xml_child_node(element, "Index"),
            ) if child is not None
        ]

    if tag == "Phi":
        return [
            child for child in (
                _xml_child_node(element, "Condition"),
                _xml_child_node(element, "True"),
                _xml_child_node(element, "False"),
            ) if child is not None
        ]

    if tag in ("And", "Or"):
        return list(element)

    return []


def _read_node(element, variables):
    """It reconstructs expression/boolean objects iteratively. """
    if element is None:
        return None

    # Maps each XML element to the UTGen IR object already built for it.
    # Parent nodes can only be created after their child nodes are ready.
    built = {}

    """
    The boolean flag means:
    False:
        children are not processed yet
    True:
        children have already been processed
    """
    stack = [(element, False)]

    def value(child):
        return built.get(id(child)) if child is not None else None

    while stack:
        current, ready = stack.pop()

        if not ready:
            # Put the current node back on the stack so we return to it
            # after all of its child expressions have been built
            stack.append((current, True))

            # Push children in reverse because the stack is LIFO
            # This preserves the original left-to-right expression order
            for child in reversed(_node_children(current)):
                stack.append((child, False))
            continue

        tag = current.tag

        if tag == "None":
            result = None
        elif tag == "Ref":
            result = Ref(current.get("path", ""), current.get("scope", ""))
        elif tag == "Const":
            raw = current.get("value", "")
            kind = current.get("type", "int")
            result = Const(raw if kind == "str" else int(raw),
                           "" if kind in ("int", "str") else kind)
        elif tag == "Unary":
            child = current[0] if len(current) else None
            result = Unary(current.get("op", ""), value(child))
        elif tag == "Binop":
            left = current[0] if len(current) > 0 else None
            right = current[1] if len(current) > 1 else None
            result = Binop(current.get("op", ""), value(left), value(right))
        elif tag == "Call":
            args = []
            for item in current.findall("Arg"):
                child = item[0] if len(item) else None
                args.append(value(child))
            result = CallE(current.get("name", ""), tuple(args))
        elif tag == "Ternary":
            result = Ternary(
                value(_xml_child_node(current, "Condition")),
                value(_xml_child_node(current, "Then")),
                value(_xml_child_node(current, "Else")),
                int(current.get("decision", "0")),
            )
        elif tag == "Cast":
            child = current[0] if len(current) else None
            result = Cast(current.get("type", ""), value(child))
        elif tag == "Index":
            result = Index(
                value(_xml_child_node(current, "Base")),
                value(_xml_child_node(current, "Index")),
            )
        elif tag == "Member":
            child = current[0] if len(current) else None
            result = MemberE(value(child), current.get("name", ""))
        elif tag == "Phi":
            result = Phi(
                value(_xml_child_node(current, "Condition")),
                value(_xml_child_node(current, "True")),
                value(_xml_child_node(current, "False")),
            )
        elif tag == "Atom":
            rhs = current.get("rhs")
            rhs_var = _read_variable(current.get("rhs_var", ""), variables)
            result = Atom(
                _read_variable(current.get("var", ""), variables),
                Op(current.get("op", "truthy")),
                int(rhs) if rhs is not None else None,
                rhs_var,
                current.get("text", ""),
            )
        elif tag in ("And", "Or"):
            children = tuple(value(child) for child in current)
            result = And(children) if tag == "And" else Or(children)
        elif tag == "Not":
            child = current[0] if len(current) else None
            result = Not(value(child))
        else:
            result = Unknown(current.get("text", ""))

        built[id(current)] = result

    return built[id(element)]


def _read_variable(name, variables):
    variable = variables.get(name)
    if variable is not None or not name:
        return variable
    normalized = re.sub(r"\[[^]]*\]", "", name)
    rank = len(re.findall(r"\[[^]]*\]", name))
    for declared in variables.values():
        if (len(re.findall(r"\[[^]]*\]", declared.name)) == rank and
                re.sub(r"\[[^]]*\]", "", declared.name) == normalized):
            variable = replace(declared, name=name)
            variables[name] = variable
            return variable
    return None

def _read_child_node(parent, tag, variables):
    item = parent.find(tag)
    return _read_node(item[0], variables) if item is not None and len(item) else None


def _write_state(parent, state):
    """ writes a UTGen execution/dataflow state into XML."""
    attrs = {"reachable": str(state.reachable).lower(), "unknown": _values(state.unknown)}
    element = ET.SubElement(parent, "State", **attrs)
    if state.condition is not None:
        condition = ET.SubElement(element, "Condition")
        _write_node(condition, state.condition)
    path = ET.SubElement(element, "Path")
    for constraint in state.path:
        item = ET.SubElement(path, "Constraint", decision=_optional(constraint.decision_id),
                             outcome=str(constraint.outcome).lower())
        _write_node(item, constraint.expr)
    values = ET.SubElement(element, "Values")
    for name, value in state.values.items():
        item = ET.SubElement(values, "Value", name=name)
        _write_node(item, value)
    aliases = ET.SubElement(element, "Aliases")
    for name, targets in state.aliases.items():
        attrs = {"name": name}
        if targets is None:
            attrs["unknown"] = "true"
        else:
            attrs["targets"] = _values(targets)
        ET.SubElement(aliases, "Alias", **attrs)


def _read_dataflow(element, variables):
    points, returns, writes = [], [], []
    for item in element.findall("Point"):
        decision = item.get("decision")
        states = []
        for child in item.findall("State"):
            states.append(_read_state(child, variables))
        points.append(DataflowPoint(int(decision) if decision else None, tuple(states),
                                    item.get("location", "")))
    returns_element = element.find("Returns")
    if returns_element is not None:
        for item in returns_element.findall("ReturnState"):
            state_element = item.find("State")
            state = _read_state(state_element, variables)
            value_element = item.find("Value")
            value = _read_node(value_element[0], variables) if value_element is not None and len(value_element) else None
            returns.append((state, value))
    writes_element = element.find("Writes")
    if writes_element is not None:
        for item in writes_element.findall("Write"):
            writes.append((item.get("name", ""), _read_node(item[0], variables) if len(item) else None))
    return DataflowResult(points, returns, writes)


def _read_state(element, variables):
    unknown = _read_values(element.get("unknown", ""))
    condition = _read_child_node(element, "Condition", variables)
    path_element = element.find("Path")
    path = []
    if path_element is not None:
        for item in path_element.findall("Constraint"):
            decision = item.get("decision")
            expression = _read_node(item[0], variables) if len(item) else None
            path.append(PathConstraint(int(decision) if decision else None, expression,
                                       _bool(item.get("outcome", "false"))))
    values, aliases = {}, {}
    values_element = element.find("Values")
    if values_element is not None:
        for item in values_element.findall("Value"):
            values[item.get("name", "")] = _read_node(item[0], variables) if len(item) else None
    aliases_element = element.find("Aliases")
    if aliases_element is not None:
        for item in aliases_element.findall("Alias"):
            aliases[item.get("name", "")] = None if _bool(item.get("unknown", "false")) else _read_values(item.get("targets", ""))
    return DataflowState(values, tuple(path), _bool(element.get("reachable", "true")),
                         tuple(unknown), condition, aliases)
