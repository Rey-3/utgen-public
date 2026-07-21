"""Translate clang function bodies into the clang-free UTGen IR."""
from __future__ import annotations

import ast

from clang.cindex import Cursor, CursorKind

from utgen.decisions import DecisionExtractor
from utgen.model import (Assign, Binop, Break, CallE, Cast, Const, Decl,
                         DoWhile, ExprStmt, If, Index, MemberE, Ref, Return,
                         Switch, SwitchCase, Ternary, Unary, Unknown, While)


_WRAPPERS = {CursorKind.UNEXPOSED_EXPR, CursorKind.PAREN_EXPR}
for _kind_name in ("IMPLICIT_CAST_EXPR", "CXX_STATIC_CAST_EXPR"):
    _kind = getattr(CursorKind, _kind_name, None)
    if _kind is not None:
        _WRAPPERS.add(_kind)
_CASTS = set()
for _kind_name in ("CSTYLE_CAST_EXPR", "CXX_STATIC_CAST_EXPR", "CXX_REINTERPRET_CAST_EXPR"):
    _kind = getattr(CursorKind, _kind_name, None)
    if _kind is not None:
        _CASTS.add(_kind)
_ASSIGNMENT_KINDS = {CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR}


class BodyIrBuilder:
    """Build statement and expression IR while linking decisions by location."""

    def __init__(self, atom_builder, decisions=None):
        self.atom_builder = atom_builder
        self._provided_decisions = decisions
        self.decisions = []
        self._decision_map = {}
        self._body_atom_builder = atom_builder

    def build(self, func_cursor: Cursor) -> list:
        """Return the top-level statements for a function definition or body."""
        if self._provided_decisions is None:
            self.decisions = DecisionExtractor(self.atom_builder).extract(func_cursor)
        else:
            self.decisions = list(self._provided_decisions)
        self._decision_map = {}
        for decision in self.decisions:
            key = (decision.kind, decision.location)
            self._decision_map.setdefault(key, []).append(decision)
        self._body_atom_builder = _fresh_atom_builder(self.atom_builder)
        body = _function_body(func_cursor) if func_cursor.kind == CursorKind.FUNCTION_DECL else func_cursor
        return self._block(body) if body is not None else []

    def _block(self, cursor: Cursor) -> list:
        statements = []
        for child in cursor.get_children():
            statements.extend(self._statement(child))
        return statements

    def _statement(self, cursor: Cursor) -> list:
        kind = cursor.kind
        if kind == CursorKind.COMPOUND_STMT:
            return self._block(cursor)
        if kind == CursorKind.DECL_STMT:
            return self._declarations(cursor)
        if kind == CursorKind.IF_STMT:
            return [self._if(cursor)]
        if kind == CursorKind.SWITCH_STMT:
            return [self._switch(cursor)]
        if kind in (CursorKind.WHILE_STMT, CursorKind.FOR_STMT):
            return [self._loop(cursor)]
        if kind == CursorKind.DO_STMT:
            return [self._do_loop(cursor)]
        if kind == CursorKind.RETURN_STMT:
            children = list(cursor.get_children())
            return [Return(self._expr(children[0])) if children else Return()]
        if kind == CursorKind.BREAK_STMT:
            return [Break()]
        if kind in _ASSIGNMENT_KINDS:
            op = _operator(cursor)
            if op in _ASSIGNMENTS:
                children = list(cursor.get_children())
                if len(children) == 2:
                    return [Assign(self._expr(children[0]), self._expr(children[1]), op)]
            return [ExprStmt(self._expr(cursor))]
        if kind in (CursorKind.CALL_EXPR, CursorKind.UNARY_OPERATOR,
                    CursorKind.CONDITIONAL_OPERATOR, CursorKind.NULL_STMT):
            if kind == CursorKind.NULL_STMT:
                return []
            return [ExprStmt(self._expr(cursor))]
        if kind in (CursorKind.CASE_STMT, CursorKind.DEFAULT_STMT):
            return self._block(cursor)
        return [Unknown(_source(cursor))]

    def _declarations(self, cursor: Cursor) -> list:
        declarations = []
        for child in cursor.get_children():
            if child.kind != CursorKind.VAR_DECL:
                continue
            init = _initializer(child)
            declarations.append(Decl(child.spelling, self._expr(init) if init is not None else None))
        return declarations

    def _if(self, cursor: Cursor) -> If:
        children = list(cursor.get_children())
        condition = children[0] if children else None
        decision_id = self._decision_id("if", condition)
        then = self._arm(children[1]) if len(children) > 1 else ()
        els = self._arm(children[2]) if len(children) > 2 else ()
        return If(decision_id, then, els)

    def _switch(self, cursor: Cursor) -> Switch:
        children = list(cursor.get_children())
        subject = self._expr(children[0]) if children else Unknown(_source(cursor))
        body = children[1] if len(children) > 1 else None
        cases = []
        for labels, statements, default in _arms(body):
            key_cursor = cursor if default else labels[0]
            decision_id = self._decision_id("case", key_cursor)
            case_body = []
            for statement in statements:
                case_body.extend(self._statement(statement))
            cases.append(SwitchCase(decision_id, tuple(case_body)))
        return Switch(subject, tuple(cases))

    def _loop(self, cursor: Cursor) -> While:
        children = list(cursor.get_children())
        body_cursor = children[-1] if children else None
        before_body = children[:-1]
        condition = _loop_condition(cursor, before_body)
        body = []
        if cursor.kind == CursorKind.FOR_STMT:
            for item in _for_side_statements(before_body, condition, self):
                body.extend(item)
        if body_cursor is not None:
            body.extend(self._statement(body_cursor))
        if cursor.kind == CursorKind.FOR_STMT:
            increment = _for_increment(before_body, condition)
            if increment is not None:
                body.extend(self._statement(increment))
        return While(self._boolean(condition), tuple(body))

    def _do_loop(self, cursor: Cursor) -> DoWhile:
        children = list(cursor.get_children())
        body = self._statement(children[0]) if children else []
        condition = children[1] if len(children) > 1 else None
        return DoWhile(self._boolean(condition), tuple(body))

    def _arm(self, cursor: Cursor) -> tuple:
        if cursor is None:
            return ()
        return tuple(self._statement(cursor))

    def _boolean(self, cursor: Cursor):
        if cursor is None:
            return Const(1)
        builder = self._body_atom_builder
        if hasattr(builder, "build"):
            return builder.build(cursor)
        return Unknown(_source(cursor))

    def _expr(self, cursor: Cursor | None):
        if cursor is None:
            return Unknown("")
        cursor = _unwrap(cursor)
        kind = cursor.kind
        if kind in (CursorKind.INTEGER_LITERAL, CursorKind.CHARACTER_LITERAL):
            return Const(_literal(cursor))
        if kind == CursorKind.FLOATING_LITERAL:
            return Const(_source(cursor))
        if kind in (CursorKind.DECL_REF_EXPR, CursorKind.VAR_DECL,
                    CursorKind.PARM_DECL):
            return Ref(cursor.spelling)
        if kind == CursorKind.MEMBER_REF_EXPR:
            children = list(cursor.get_children())
            base = self._expr(children[0]) if children else Unknown("")
            return MemberE(base, cursor.spelling)
        if kind == CursorKind.ARRAY_SUBSCRIPT_EXPR:
            children = list(cursor.get_children())
            if len(children) == 2:
                return Index(self._expr(children[0]), self._expr(children[1]))
            return Unknown(_source(cursor))
        if kind == CursorKind.CALL_EXPR:
            callee = _callee_name(cursor)
            args = tuple(self._expr(child) for child in _call_arguments(cursor))
            return CallE(callee, args)
        if kind in _CASTS:
            children = list(cursor.get_children())
            value = self._expr(children[-1]) if children else Unknown(_source(cursor))
            return Cast(getattr(cursor.type, "spelling", ""), value)
        if kind == CursorKind.CONDITIONAL_OPERATOR:
            children = list(cursor.get_children())
            if len(children) == 3:
                return Ternary(self._expr(children[0]), self._expr(children[1]),
                               self._expr(children[2]))
            return Unknown(_source(cursor))
        if kind == CursorKind.UNARY_OPERATOR:
            children = list(cursor.get_children())
            return Unary(_operator(cursor), self._expr(children[0])) if children else Unknown(_source(cursor))
        if kind in (CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
            children = list(cursor.get_children())
            if len(children) == 2:
                return Binop(_operator(cursor), self._expr(children[0]), self._expr(children[1]))
            return Unknown(_source(cursor))
        return Unknown(_source(cursor))

    def _decision_id(self, kind: str, cursor: Cursor | None) -> int:
        if cursor is None:
            return 0
        key = (kind, _location(cursor))
        decisions = self._decision_map.get(key, [])
        if decisions:
            return decisions.pop(0).id
        return 0


_ASSIGNMENTS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


def _function_body(cursor: Cursor) -> Cursor | None:
    for child in cursor.get_children():
        if child.kind == CursorKind.COMPOUND_STMT:
            return child
    return None


def _initializer(cursor: Cursor) -> Cursor | None:
    for child in reversed(list(cursor.get_children())):
        if child.kind not in (CursorKind.TYPE_REF, CursorKind.ANNOTATE_ATTR):
            return child
    return None


def _arms(body: Cursor | None):
    if body is None:
        return []
    result = []
    for child in body.get_children():
        if child.kind == CursorKind.CASE_STMT:
            labels, statements = _case_chain(child)
            result.append((labels, statements, False))
        elif child.kind == CursorKind.DEFAULT_STMT:
            result.append(([], list(child.get_children()), True))
    return result


def _case_chain(cursor: Cursor):
    labels = []
    current = cursor
    while current.kind == CursorKind.CASE_STMT:
        children = list(current.get_children())
        if not children:
            break
        labels.append(children[0])
        nested = children[1:]
        if len(nested) == 1 and nested[0].kind == CursorKind.CASE_STMT:
            current = nested[0]
            continue
        return labels, nested
    return labels, []


def _loop_condition(cursor: Cursor, before_body: list[Cursor]):
    if cursor.kind == CursorKind.WHILE_STMT or cursor.kind == CursorKind.DO_STMT:
        return before_body[0] if before_body else None
    for child in before_body:
        if child.kind not in (CursorKind.DECL_STMT, CursorKind.NULL_STMT):
            return child
    return None


def _for_side_statements(before_body, condition, builder):
    if not before_body:
        return []
    first = before_body[0]
    if first is condition or first.kind == CursorKind.NULL_STMT:
        return []
    return [builder._statement(first)]


def _for_increment(before_body, condition):
    candidates = [child for child in before_body if child is not condition]
    if len(candidates) > 1:
        return candidates[-1]
    return None


def _unwrap(cursor: Cursor) -> Cursor:
    while cursor.kind in _WRAPPERS:
        children = list(cursor.get_children())
        if len(children) != 1:
            break
        cursor = children[0]
    return cursor


def _call_arguments(cursor: Cursor):
    callee = cursor.referenced
    args = []
    for child in cursor.get_children():
        unwrapped = _unwrap(child)
        referenced = getattr(unwrapped, "referenced", None)
        if callee is not None and referenced is not None:
            if referenced.get_usr() == callee.get_usr():
                continue
        args.append(child)
    return args


def _callee_name(cursor: Cursor) -> str:
    referenced = cursor.referenced
    return referenced.spelling if referenced is not None else cursor.spelling


def _literal(cursor: Cursor):
    text = _source(cursor).strip()
    if text.startswith("'") and text.endswith("'"):
        try:
            return ord(ast.literal_eval(text))
        except (SyntaxError, ValueError, TypeError):
            return text
    value = text.rstrip("uUlL")
    try:
        return int(value, 0)
    except ValueError:
        return text


def _operator(cursor: Cursor) -> str:
    spelling = getattr(cursor, "spelling", "")
    operators = ("&&", "||", "==", "!=", "<=", ">=", "<<=", ">>=", "<<", ">>",
                 "<", ">", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
                 "=", "&", "|", "^", "+", "-", "*", "/", "%", "!", "++", "--", "~")
    if spelling in operators:
        return spelling
    for token in cursor.get_tokens():
        if token.spelling in operators:
            return token.spelling
    return ""


def _source(cursor: Cursor) -> str:
    return " ".join(token.spelling for token in cursor.get_tokens()).strip()


def _location(cursor: Cursor) -> str:
    location = cursor.location
    file = location.file
    return f"{file}:{location.line}:{location.column}" if file else str(location)


def _fresh_atom_builder(builder):
    try:
        from utgen.atoms import AtomBuilder, CopyPropagation
        if isinstance(builder, AtomBuilder):
            return AtomBuilder(builder.inventory, CopyPropagation())
    except ImportError:
        pass
    return builder
