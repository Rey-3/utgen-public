"""Extract source-order decisions and FILO guards from clang cursors."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from clang.cindex import Cursor, CursorKind

from utgen.model import And, Atom, Decision, Guard, Not, Op, Or


class _SourceAtomBuilder:
    """Fallback atom builder used by Task 6 before real atom mapping exists."""

    def build(self, cursor: Cursor) -> Atom:
        """Build an uncontrollable truthy atom carrying source text."""
        return Atom(None, Op.TRUTHY, text=_source(cursor))


@dataclass
class _Arm:
    """One switch arm with one or more labels and its body cursors."""

    labels: list[Cursor]
    body: list[Cursor]
    default: bool = False


class DecisionExtractor:
    """Extract if, case, and ternary decisions with outer-to-inner guards."""

    def __init__(self, atom_builder=None):
        self.atom_builder = atom_builder or _SourceAtomBuilder()
        self._decisions: list[Decision] = []
        self._guards: list[Guard] = []
        self._next_id = 1

    def extract(self, body_cursor: Cursor) -> list[Decision]:
        """Return decisions in source order from a function or body cursor."""
        self._decisions = []
        self._guards = []
        self._next_id = 1
        body = body_cursor
        if body.kind == CursorKind.FUNCTION_DECL:
            body = self._function_body(body)
        if body is not None:
            self._statement(body)
        return self._decisions

    def _function_body(self, cursor: Cursor) -> Cursor | None:
        for child in cursor.get_children():
            if child.kind == CursorKind.COMPOUND_STMT:
                return child
        return None

    def _statement(self, cursor: Cursor) -> None:
        kind = cursor.kind
        if kind == CursorKind.COMPOUND_STMT:
            self._block(cursor)
        elif kind == CursorKind.IF_STMT:
            self._if(cursor)
        elif kind == CursorKind.SWITCH_STMT:
            self._switch(cursor)
        elif kind in (CursorKind.WHILE_STMT, CursorKind.FOR_STMT):
            self._loop(cursor)
        elif kind == CursorKind.DO_STMT:
            self._do_loop(cursor)
        elif kind in (CursorKind.DECL_STMT, CursorKind.RETURN_STMT,
                      CursorKind.BINARY_OPERATOR, CursorKind.UNARY_OPERATOR,
                      CursorKind.COMPOUND_ASSIGNMENT_OPERATOR,
                      CursorKind.CALL_EXPR, CursorKind.NULL_STMT):
            self._nested_expr(cursor)
        else:
            for child in cursor.get_children():
                if child.kind not in (CursorKind.BREAK_STMT, CursorKind.CONTINUE_STMT):
                    self._statement(child)

    def _block(self, cursor: Cursor) -> None:
        for child in cursor.get_children():
            self._statement(child)
            if self._ends_in_return(child):
                break

    def _if(self, cursor: Cursor) -> None:
        children = list(cursor.get_children())
        if not children:
            return
        condition = children[0]
        then_cursor = children[1] if len(children) > 1 else None
        else_cursor = children[2] if len(children) > 2 else None
        decision = self._decision(condition, "if")
        self._nested_expr(condition)
        depth = len(self._guards)
        if then_cursor is not None:
            with self._guard(decision.expr, True):
                self._statement(then_cursor)
        then_returns = then_cursor is not None and self._ends_in_return(then_cursor)
        else_returns = False
        if else_cursor is not None:
            with self._guard(decision.expr, False):
                self._statement(else_cursor)
            else_returns = self._ends_in_return(else_cursor)
        del self._guards[depth:]
        if then_returns and (else_cursor is None or not else_returns):
            self._guards.append(Guard(decision.expr, False))

    def _switch(self, cursor: Cursor) -> None:
        children = list(cursor.get_children())
        if len(children) < 2:
            return
        subject = children[0]
        body = children[1]
        self._nested_expr(subject)
        arms = self._arms(body)
        case_values: list[tuple[int | None, str]] = []
        for arm in arms:
            for label in arm.labels:
                case_values.append((_literal_value(label), _source(label)))
        returning: list[object] = []
        for arm in arms:
            if arm.default:
                expr = self._default_expr(_source(subject), case_values)
            else:
                expr = self._case_expr(_source(subject), arm.labels)
            decision = self._record(expr, "case", arm.labels[0] if arm.labels else cursor)
            with self._guard(decision.expr, True):
                for statement in arm.body:
                    self._statement(statement)
            if any(self._ends_in_return(statement) for statement in arm.body):
                returning.append(decision.expr)
        for expr in returning:
            self._guards.append(Guard(expr, False))

    def _arms(self, body: Cursor) -> list[_Arm]:
        arms: list[_Arm] = []
        for child in body.get_children():
            if child.kind == CursorKind.CASE_STMT:
                labels, statements = self._case_chain(child)
                arms.append(_Arm(labels, statements))
            elif child.kind == CursorKind.DEFAULT_STMT:
                arms.append(_Arm([], list(child.get_children()), True))
        return arms

    def _case_chain(self, cursor: Cursor) -> tuple[list[Cursor], list[Cursor]]:
        labels: list[Cursor] = []
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

    def _loop(self, cursor: Cursor) -> None:
        children = list(cursor.get_children())
        if not children:
            return
        body = children[-1]
        condition = children[1] if cursor.kind == CursorKind.FOR_STMT and len(children) > 2 else children[0]
        self._nested_expr(condition)
        with self._guard(self._build(condition), True):
            self._statement(body)

    def _do_loop(self, cursor: Cursor) -> None:
        children = list(cursor.get_children())
        if children:
            self._statement(children[0])
        if len(children) > 1:
            self._nested_expr(children[1])

    def _nested_expr(self, cursor: Cursor) -> None:
        if cursor.kind == CursorKind.CONDITIONAL_OPERATOR:
            children = list(cursor.get_children())
            if not children:
                return
            condition = children[0]
            decision = self._decision(condition, "if")
            if len(children) > 1:
                with self._guard(decision.expr, True):
                    self._nested_expr(children[1])
            if len(children) > 2:
                with self._guard(decision.expr, False):
                    self._nested_expr(children[2])
            return
        for child in cursor.get_children():
            if child.kind not in (CursorKind.DECL_REF_EXPR, CursorKind.INTEGER_LITERAL,
                                  CursorKind.FLOATING_LITERAL, CursorKind.CHARACTER_LITERAL):
                self._nested_expr(child)

    def _decision(self, condition: Cursor, kind: str) -> Decision:
        return self._record(self._build(condition), kind, condition)

    def _record(self, expr: object, kind: str, cursor: Cursor) -> Decision:
        decision = Decision(self._next_id, expr, kind, tuple(self._guards), _location(cursor))
        self._next_id += 1
        self._decisions.append(decision)
        return decision

    def _build(self, cursor: Cursor) -> object:
        builder = self.atom_builder
        if hasattr(builder, "build"):
            return builder.build(cursor)
        return builder(cursor)

    def _case_expr(self, subject: str, labels: list[Cursor]) -> object:
        atoms: list[Atom] = []
        for label in labels:
            label_text = _source(label)
            value = _literal_value(label)
            atoms.append(Atom(None, Op.EQ, rhs=value,
                              text=f"{subject} == {label_text}"))
        if len(atoms) == 1:
            return atoms[0]
        return Or(tuple(atoms))

    def _default_expr(self, subject: str, values: list[tuple[int | None, str]]) -> object:
        atoms: list[Not] = []
        for value, label_text in values:
            eq = Atom(None, Op.EQ, rhs=value,
                      text=f"{subject} == {label_text}")
            atoms.append(Not(eq))
        return And(tuple(atoms))

    def _ends_in_return(self, cursor: Cursor) -> bool:
        if cursor.kind == CursorKind.RETURN_STMT:
            return True
        if cursor.kind == CursorKind.COMPOUND_STMT:
            children = list(cursor.get_children())
            return bool(children) and self._ends_in_return(children[-1])
        if cursor.kind == CursorKind.IF_STMT:
            children = list(cursor.get_children())
            return len(children) > 2 and self._ends_in_return(children[1]) and self._ends_in_return(children[2])
        return False

    @contextmanager
    def _guard(self, expr: object, outcome: bool):
        self._guards.append(Guard(expr, outcome))
        try:
            yield
        finally:
            self._guards.pop()


def _source(cursor: Cursor) -> str:
    """Return normalized token text for a cursor."""
    return " ".join(token.spelling for token in cursor.get_tokens()).strip()


def _literal_value(cursor: Cursor) -> int | None:
    """Fold a simple case label to an integer when possible."""
    try:
        result = cursor.evaluate()
        if result is not None and hasattr(result, "as_int"):
            return result.as_int()
    except Exception:
        pass
    text = _source(cursor).strip()
    if len(text) >= 3 and text[0] == "'" and text[-1] == "'":
        return ord(text[1])
    suffix = text.rstrip("uUlL")
    try:
        return int(suffix, 0)
    except ValueError:
        return None


def _location(cursor: Cursor) -> str:
    """Return a stable file:line:column cursor location."""
    location = cursor.location
    file = location.file
    return f"{file}:{location.line}:{location.column}" if file else str(location)
