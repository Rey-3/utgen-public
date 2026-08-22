
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from clang.cindex import Cursor, CursorKind

from utgen.model import And, Atom, Decision, Guard, Not, Op, Or


class _SourceAtomBuilder:
    """Fallback builder that keeps a condition as source text.

    It is used when no semantic AtomBuilder is supplied. The resulting atom is
    intentionally uncontrollable: it preserves the decision for reporting even
    when UTGen cannot solve concrete values for it.
    """

    def build(self, cursor: Cursor) -> Atom:
        return Atom(None, Op.TRUTHY, text=_source(cursor))


@dataclass
class _Arm:
    """One logical switch arm. Consecutive case labels can share one body."""

    labels: list[Cursor]
    body: list[Cursor]
    default: bool = False


class DecisionExtractor:
    """Extract control-flow decisions and the guards required to reach them.

    Besides normal if/loop conditions, this also finds decisions hidden inside
    expressions (for example ternaries and short-circuit assignments). The guard
    stack records which earlier outcomes must hold before a decision is reachable.
    """

    def __init__(self, atom_builder=None):
        self.atom_builder = atom_builder or _SourceAtomBuilder()
        self._decisions: list[Decision] = []
        self._guards: list[Guard] = []
        self._next_id = 1

    def extract(self, body_cursor: Cursor) -> list[Decision]:
        """Extract decisions from a function cursor or a statement subtree."""

        # Extraction state is per function; decision IDs restart from 1.
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
        elif kind in (CursorKind.DECL_STMT, CursorKind.BINARY_OPERATOR,
                      CursorKind.COMPOUND_ASSIGNMENT_OPERATOR, CursorKind.UNARY_OPERATOR):
            if cursor.kind == CursorKind.BINARY_OPERATOR and _operator(cursor) == "=":
                # A plain assignment is not a decision, but a logical RHS uses
                # C short-circuit evaluation and therefore has decision behavior.
                condition = self._assignment_condition(cursor)
                if condition is not None:
                    self._record(self._build_source(condition), "expr", condition)
                self._nested_expr(cursor)
                self._record_cursor(cursor)
            else:
                self._record_cursor(cursor)
                self._nested_expr(cursor)
        elif kind in (CursorKind.RETURN_STMT, CursorKind.CALL_EXPR, CursorKind.NULL_STMT):
            self._nested_expr(cursor)
        else:
            for child in cursor.get_children():
                if child.kind not in (CursorKind.BREAK_STMT, CursorKind.CONTINUE_STMT):
                    self._statement(child)

    def _block(self, cursor: Cursor) -> None:
        for child in cursor.get_children():
            self._statement(child)
            # Do not extract decisions from statements that are syntactically
            # unreachable after an unconditional return.
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

        # Copy-propagation must follow each branch independently. A write in the
        # then branch must not affect how the else branch is interpreted.
        before = self._copy_state()
        then_state = before
        if then_cursor is not None:
            with self._guard(decision.expr, True):
                self._statement(then_cursor)
            then_state = self._copy_state()
        self._restore_state(before)
        then_returns = then_cursor is not None and self._ends_in_return(then_cursor)
        else_returns = False
        else_state = before
        if else_cursor is not None:
            with self._guard(decision.expr, False):
                self._statement(else_cursor)
            else_state = self._copy_state()
            else_returns = self._ends_in_return(else_cursor)
        # After the branch, only bindings that are valid across both paths should
        # survive. The AtomBuilder decides how those snapshots are merged.
        self._restore_state(self._merge_states(then_state, else_state))
        del self._guards[depth:]

        # If the true branch returns, later statements are reachable only when
        # this condition was false. Keep that outcome as a guard for what follows.
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

        # Build the complete case set first: the default branch is equivalent to
        # "none of the explicit case comparisons matched".
        case_values: list[tuple[int | None, str]] = []
        for arm in arms:
            for label in arm.labels:
                case_values.append((_literal_value(label), _source(label)))
        case_atoms = []
        for arm in arms:
            if not arm.default:
                case_atoms.extend(self._case_atoms(subject, arm.labels))
        returning: list[object] = []
        before, states = self._copy_state(), []
        for arm in arms:
            # Analyze each switch arm from the same incoming propagation state so
            # assignments in one alternative do not leak into another.
            self._restore_state(before)
            if arm.default:
                expr = self._default_expr(_source(subject), case_values, case_atoms)
            else:
                expr = self._case_expr(subject, arm.labels)
            decision = self._record(expr, "case", arm.labels[0] if arm.labels else cursor)
            with self._guard(decision.expr, True):
                for statement in arm.body:
                    self._statement(statement)
            states.append(self._copy_state())
            if any(self._ends_in_return(statement) for statement in arm.body):
                returning.append(decision.expr)
        if not any(arm.default for arm in arms):
            states.append(before)
        self._restore_state(self._merge_states(*states))

        # A case that returns cannot reach statements after the switch. Record the
        # opposite outcome so later decisions are guarded away from that arm.
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
        """Flatten Clang's nested CASE_STMT representation for shared labels.

        Source such as ``case 1: case 2: stmt;`` is represented as one CASE_STMT
        inside another. UTGen stores both labels as one logical arm.
        """
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
        before = self._copy_state()

        # Traverse the body under the loop-true path to discover nested decisions,
        # then restore propagation state because the loop may execute zero or many
        # times and a single-pass state would be unsafe to carry forward.
        with self._guard(self._build(condition), True):
            self._statement(body)
        self._restore_state(before)
        self._record_cursor(cursor)

    def _do_loop(self, cursor: Cursor) -> None:
        children = list(cursor.get_children())
        before = self._copy_state()

        # A do/while executes its body before testing the condition, so nested body
        # decisions are visited first. Propagation is still restored afterwards to
        # avoid treating one observed iteration as the post-loop state.
        if children:
            self._statement(children[0])
            self._restore_state(before)
            self._record_cursor(cursor)
        if len(children) > 1:
            self._nested_expr(children[1])

    def _copy_state(self):
        """Snapshot optional copy-propagation state used by the AtomBuilder."""
        copyprop = getattr(self.atom_builder, "copyprop", None)
        return copyprop.snapshot() if copyprop is not None and hasattr(copyprop, "snapshot") else None

    def _restore_state(self, state) -> None:
        copyprop = getattr(self.atom_builder, "copyprop", None)
        if state is not None and copyprop is not None and hasattr(copyprop, "restore"):
            copyprop.restore(state)

    def _merge_states(self, *states):
        """Merge only propagation facts that remain valid across paths."""
        copyprop = getattr(self.atom_builder, "copyprop", None)
        valid = []
        for state in states:
            if state is not None:
                valid.append(state)
        return copyprop.merge(*valid) if valid and copyprop is not None and hasattr(copyprop, "merge") else None

    def _nested_expr(self, cursor: Cursor) -> None:
        # Decisions can exist inside an expression without a standalone statement.
        # The ternary operator is the important case: each arm has its own guard.
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

    @staticmethod
    def _assignment_condition(cursor: Cursor) -> Cursor | None:
        """Return a logical RHS that introduces short-circuit control flow."""
        if cursor.kind != CursorKind.BINARY_OPERATOR or _operator(cursor) != "=":
            return None
        children = list(cursor.get_children())
        rhs = _unwrap(children[-1]) if len(children) == 2 else None
        return rhs if rhs is not None and rhs.kind == CursorKind.BINARY_OPERATOR and _operator(rhs) in ("&&", "||") else None

    def _record(self, expr: object, kind: str, cursor: Cursor) -> Decision:
        # Freeze the current guard stack into the Decision so later analysis knows
        # the exact path requirements that make this decision reachable.
        decision = Decision(self._next_id, expr, kind, tuple(self._guards), _location(cursor))
        self._next_id += 1
        self._decisions.append(decision)
        return decision

    def _build(self, cursor: Cursor) -> object:
        builder = self.atom_builder
        if hasattr(builder, "build"):
            return builder.build(cursor)
        return builder(cursor)

    def _build_source(self, cursor: Cursor) -> object:
        builder = self.atom_builder
        return builder.build_source(cursor) if hasattr(builder, "build_source") else self._build(cursor)

    def _record_cursor(self, cursor: Cursor) -> None:
        copyprop = getattr(self.atom_builder, "copyprop", None)
        if copyprop is not None and hasattr(copyprop, "record"):
            copyprop.record(cursor)

    def _case_expr(self, subject: Cursor, labels: list[Cursor]) -> object:
        # Multiple labels sharing one body are one decision arm: any label match is
        # sufficient, so their equality atoms are joined with OR.
        atoms = self._case_atoms(subject, labels)
        if len(atoms) == 1:
            return atoms[0]
        return Or(tuple(atoms))

    def _case_atoms(self, subject: Cursor, labels: list[Cursor]) -> list[Atom]:
        atoms = []
        for label in labels:
            label_text = _source(label)
            value = _literal_value(label)
            build_case = getattr(self.atom_builder, "build_case", None)
            if build_case is not None:
                atoms.append(build_case(subject, label))
            else:
                atoms.append(Atom(None, Op.EQ, rhs=value,
                                  text=f"{_source(subject)} == {label_text}"))
        return atoms

    def _default_expr(self, subject: str, values: list[tuple[int | None, str]],
                      case_atoms: list[Atom] | None = None) -> object:
        # default is selected only when every explicit case comparison is false.
        if case_atoms is not None:
            return And(tuple(Not(atom) for atom in case_atoms))
        atoms: list[Not] = []
        for value, label_text in values:
            eq = Atom(None, Op.EQ, rhs=value,
                      text=f"{subject} == {label_text}")
            atoms.append(Not(eq))
        return And(tuple(atoms))

    def _ends_in_return(self, cursor: Cursor) -> bool:
        """Conservatively detect subtrees that cannot continue past this point."""
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
        # Guards are scoped like a path stack: nested decisions inherit them and
        # the context manager guarantees cleanup after leaving the branch.
        self._guards.append(Guard(expr, outcome))
        try:
            yield
        finally:
            self._guards.pop()


def _source(cursor: Cursor) -> str:
    """Reconstruct readable source text from the tokens covered by a cursor."""
    return " ".join(token.spelling for token in cursor.get_tokens()).strip()


def _operator(cursor: Cursor) -> str:
    """Get the small operator subset needed by decision extraction.

    Newer libclang bindings expose ``binary_operator`` directly. The token scan is
    a compatibility fallback; it starts after the left child so operators inside
    the left expression are not mistaken for the parent operator.
    """
    operator = getattr(cursor, "binary_operator", None)
    if operator is not None and getattr(operator, "name", "Invalid") != "Invalid":
        return {"Assign": "=", "LAnd": "&&", "LOr": "||"}.get(operator.name, "")
    children = list(cursor.get_children())
    if len(children) != 2:
        return ""
    left_end = children[0].extent.end.offset
    for token in cursor.get_tokens():
        if token.extent.start.offset >= left_end:
            return token.spelling if token.spelling in ("=", "&&", "||") else ""
    return ""


def _unwrap(cursor: Cursor) -> Cursor:
    """Strip AST wrapper nodes that hide the semantic RHS expression."""
    while cursor.kind in (CursorKind.UNEXPOSED_EXPR, CursorKind.PAREN_EXPR):
        children = list(cursor.get_children())
        if len(children) != 1:
            break
        cursor = children[0]
    return cursor


def _literal_value(cursor: Cursor) -> int | None:
    """Resolve a case label to an integer when Clang or simple text can do so."""

    # Prefer Clang evaluation because it also handles constant expressions.
    try:
        result = cursor.evaluate()
        if result is not None and hasattr(result, "as_int"):
            return result.as_int()
    except Exception:
        pass
    # Fall back to common literal spellings when evaluation is unavailable.
    text = _source(cursor).strip()
    if len(text) >= 3 and text[0] == "'" and text[-1] == "'":
        return ord(text[1])
    suffix = text.rstrip("uUlL")
    try:
        return int(suffix, 0)
    except ValueError:
        return None


def _location(cursor: Cursor) -> str:

    location = cursor.location
    file = location.file
    return f"{file}:{location.line}:{location.column}" if file else str(location)
