
"""Build the executable UTGen IR from libclang's C AST.

This module keeps source-level control flow and expressions in a compact IR used by
the interpreter, dataflow analysis, coverage generation, and database serialization.
Comments here focus on places where libclang's AST shape or UTGen's internal
representation is not obvious from the code alone.
"""

from __future__ import annotations

import ast
import linecache

from clang.cindex import Cursor, CursorKind, TypeKind

from utgen.atoms import _fold_int, _macro_constant
from utgen.decisions import DecisionExtractor
from utgen.model import (Assign, Binop, Break, CallE, Cast, Const, Decl,
                         DoWhile, ExprStmt, For, If, Index, MemberE, Ref,
                         Return, Switch, SwitchCase, Ternary, Unary, Unknown,
                         While)


# These AST nodes only wrap another expression. Removing them keeps the IR stable
# across libclang versions without discarding casts that change C semantics.
_WRAPPERS = {CursorKind.UNEXPOSED_EXPR, CursorKind.PAREN_EXPR}
for _kind_name in ("IMPLICIT_CAST_EXPR", "CXX_STATIC_CAST_EXPR"):
    _kind = getattr(CursorKind, _kind_name, None)
    if _kind is not None:
        _WRAPPERS.add(_kind)
# Casts are kept as explicit IR nodes, unlike the transparent wrappers above,
# because the target type can affect integer width/signedness during evaluation.
_CASTS = set()
for _kind_name in ("CSTYLE_CAST_EXPR", "CXX_STATIC_CAST_EXPR", "CXX_REINTERPRET_CAST_EXPR"):
    _kind = getattr(CursorKind, _kind_name, None)
    if _kind is not None:
        _CASTS.add(_kind)
_ASSIGNMENT_KINDS = {CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR}


class BodyIrBuilder:
    """Translate a function body from clang cursors into UTGen statement/expression IR."""

    def __init__(self, atom_builder, decisions=None, macros=None):
        self.atom_builder = atom_builder
        self._provided_decisions = decisions
        self.decisions = []
        self._decision_map = {}
        self._body_atom_builder = atom_builder
        self._local_scopes = {}
        self._static_names = {}
        self._macro_values = {}
        # Keep the raw macro text when no numeric value was pre-computed. _literal()
        # gets another chance to fold that text when it encounters the macro in an expression.
        for macro in macros or ():
            self._macro_values[macro.name] = macro.value if macro.value is not None else macro.raw

    def build(self, func_cursor: Cursor) -> list:
        """Build the body IR while keeping its control-flow nodes aligned with Decision IDs."""

        # Decision extraction is intentionally done before body conversion. Later IR nodes
        # refer back to these IDs so coverage, interpreter traces and dataflow share one identity.
        if self._provided_decisions is None:
            self.decisions = DecisionExtractor(self.atom_builder).extract(func_cursor)
        else:
            self.decisions = list(self._provided_decisions)
        # A location maps to a list rather than one ID because clang can expose multiple
        # decision records at the same source position. _decision_id() consumes them in order.
        self._decision_map = {}
        for decision in self.decisions:
            key = (decision.kind, decision.location)
            self._decision_map.setdefault(key, []).append(decision)
        # Decision extraction may have advanced CopyPropagation state in AtomBuilder.
        # Use a fresh builder for loop-condition IR so earlier analysis does not leak into it.
        self._body_atom_builder = _fresh_atom_builder(self.atom_builder)
        body = _function_body(func_cursor) if func_cursor.kind == CursorKind.FUNCTION_DECL else func_cursor
        self._local_scopes = _local_scopes(body)
        self._static_names = _static_names(body, func_cursor.spelling)
        return self._block(body) if body is not None else []

    def _block(self, cursor: Cursor) -> list:
        statements = []
        for child in cursor.get_children():
            statements.extend(self._statement(child))
        return statements

    def _statement(self, cursor: Cursor) -> list:
        """Convert one clang statement cursor; return a list because blocks/declarations can expand."""
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
                    # A logical RHS such as `x = a && b` is recorded as an expression
                    # decision. Store that ID on Assign so runtime tracing can attribute the
                    # observed boolean outcome to the original source decision.
                    decision_id = self._decision_id("expr", _unwrap(children[1])) if op == "=" else 0
                    return [Assign(self._expr(children[0]), self._expr(children[1]), op, decision_id)]
            return [ExprStmt(self._expr(cursor))]
        if kind in (CursorKind.CALL_EXPR, CursorKind.UNARY_OPERATOR,
                    CursorKind.CONDITIONAL_OPERATOR, CursorKind.NULL_STMT):
            if kind == CursorKind.NULL_STMT:
                return []
            return [ExprStmt(self._expr(cursor))]
        if kind in (CursorKind.CASE_STMT, CursorKind.DEFAULT_STMT):
            return self._block(cursor)
        # Do not silently drop syntax that UTGen does not model yet. Keeping its source as
        # Unknown lets later analysis degrade conservatively instead of inventing semantics.
        return [Unknown(_source(cursor))]

    def _declarations(self, cursor: Cursor) -> list:
        """Create Decl nodes and attach scope/static-storage identity needed by later analyses."""
        declarations = []
        for child in cursor.get_children():
            if child.kind != CursorKind.VAR_DECL:
                continue
            init = _initializer(child)
            # C guarantees zero-initialization for static storage. Automatic locals without an
            # initializer stay Unknown instead of being given a value they do not have in C.
            value = Const(0) if init is None and _is_static(child) else self._expr(init)
            scope = self._local_scopes.get(child.get_usr(), "")
            pointer = child.type.get_canonical().kind == TypeKind.POINTER
            declarations.append(Decl(child.spelling, value, scope, pointer, self._static_names.get(child.get_usr(), "")))
        return declarations

    def _if(self, cursor: Cursor) -> If:
        children = list(cursor.get_children())
        condition = children[0] if children else None
        decision_id = self._decision_id("if", condition)
        then = self._arm(children[1]) if len(children) > 1 else ()
        els = self._arm(children[2]) if len(children) > 2 else ()
        return If(decision_id, then, els, self._expr(condition))

    def _switch(self, cursor: Cursor) -> Switch:
        """Flatten clang's nested CASE_STMT shape into explicit SwitchCase IR arms."""
        children = list(cursor.get_children())
        subject = self._expr(children[0]) if children else Unknown(_source(cursor))
        body = children[1] if len(children) > 1 else None
        cases = []
        for labels, statements, default in _arms(body):
            # Default has no label cursor to key the decision with; DecisionExtractor records
            # it against the switch cursor, while normal cases use their first label cursor.
            key_cursor = cursor if default else labels[0]
            decision_id = self._decision_id("case", key_cursor)
            case_body = []
            for statement in statements:
                case_body.extend(self._statement(statement))
            case_labels = []
            for label in labels:
                case_labels.append(_literal(label, self._macro_values))
            cases.append(SwitchCase(decision_id, tuple(case_body), tuple(case_labels), default))
        return Switch(subject, tuple(cases))

    def _loop(self, cursor: Cursor) -> While | For:
        """Build loop IR with both solver-friendly and runtime forms of the condition."""
        children = list(cursor.get_children())
        body_cursor = children[-1] if children else None
        before_body = children[:-1]
        condition = _loop_condition(cursor, before_body)
        if cursor.kind == CursorKind.FOR_STMT:
            init = _for_part(before_body, 0, self)
            increment = _for_part(before_body, 2, self)
            body = self._statement(body_cursor) if body_cursor is not None else []
            # cond is the Atom/boolean form used for constraints; runtime_cond preserves the
            # general expression tree needed by the interpreter to execute the actual loop.
            return For(init, self._boolean(condition), increment, tuple(body), self._expr(condition))
        body = self._statement(body_cursor) if body_cursor is not None else []
        return While(self._boolean(condition), tuple(body), self._expr(condition))

    def _do_loop(self, cursor: Cursor) -> DoWhile:
        children = list(cursor.get_children())
        body = self._statement(children[0]) if children else []
        condition = children[1] if len(children) > 1 else None
        return DoWhile(self._boolean(condition), tuple(body), self._expr(condition))

    def _arm(self, cursor: Cursor) -> tuple:
        if cursor is None:
            return ()
        return tuple(self._statement(cursor))

    def _boolean(self, cursor: Cursor):
        """Convert a condition through AtomBuilder so solvers can reason about its predicates."""
        if cursor is None:
            # Missing for-loop conditions represent `for (;;)` and are therefore always true.
            return Const(1)
        builder = self._body_atom_builder
        if hasattr(builder, "build"):
            return builder.build(cursor)
        return Unknown(_source(cursor))

    def _expr(self, cursor: Cursor | None):
        """Convert a clang expression into executable IR, preserving Unknown when unsupported."""
        if cursor is None:
            return Unknown("")
        cursor = _unwrap(cursor)
        kind = cursor.kind
        if kind in (CursorKind.INTEGER_LITERAL, CursorKind.CHARACTER_LITERAL):
            value = _literal(cursor, self._macro_values)
            return Const(value, _literal_type(cursor, value))
        if kind == CursorKind.FLOATING_LITERAL:
            return Const(_spelling(cursor))
        if kind == CursorKind.INIT_LIST_EXPR:
            return Const(0) if _zero_initializer(cursor, self._macro_values) else Unknown(_source(cursor))
        if kind in (CursorKind.DECL_REF_EXPR, CursorKind.VAR_DECL,
                    CursorKind.PARM_DECL):
            # Resolve constants before creating a Ref. This keeps enum members and numeric
            # macros out of the runtime environment as if they were mutable variables.
            referenced = cursor.referenced if kind == CursorKind.DECL_REF_EXPR else cursor
            if referenced is not None and referenced.kind == CursorKind.ENUM_CONSTANT_DECL:
                return Const(referenced.enum_value)
            macro = _literal(cursor, self._macro_values)
            if isinstance(macro, int):
                return Const(macro)
            scope = self._local_scopes.get(referenced.get_usr(), "") if referenced is not None else ""
            return Ref(cursor.spelling, scope)
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
            # _call_arguments() removes clang's callee-reference child when it is present,
            # leaving only the source-level arguments in CallE.
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
                               self._expr(children[2]), self._decision_id("if", children[0]))
            return Unknown(_source(cursor))
        if kind == CursorKind.UNARY_OPERATOR:
            children = list(cursor.get_children())
            return Unary(_operator(cursor), self._expr(children[0])) if children else Unknown(_source(cursor))
        if kind in (CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
            children = list(cursor.get_children())
            if len(children) == 2:
                constant = _literal(cursor, self._macro_values)
                if isinstance(constant, int):
                    return Const(constant)
                return Binop(_operator(cursor), self._expr(children[0]), self._expr(children[1]))
            return Unknown(_source(cursor))
        return Unknown(_source(cursor))

    def _decision_id(self, kind: str, cursor: Cursor | None) -> int:
        """Match a body cursor back to the DecisionExtractor record at the same location."""
        if cursor is None:
            return 0
        key = (kind, _location(cursor))
        decisions = self._decision_map.get(key, [])
        if decisions:
            # Consume in extraction order so duplicate source locations do not all reuse one ID.
            return decisions.pop(0).id
        return 0


_ASSIGNMENTS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


def _function_body(cursor: Cursor) -> Cursor | None:
    for child in cursor.get_children():
        if child.kind == CursorKind.COMPOUND_STMT:
            return child
    return None


def _local_scopes(cursor: Cursor | None) -> dict:
    """Assign synthetic scopes only to shadowed local names that would otherwise collide."""
    declarations = []

    def collect(current):
        if current.kind == CursorKind.VAR_DECL:
            declarations.append(current)
        for child in current.get_children():
            collect(child)

    if cursor is not None:
        collect(cursor)
    counts = {}
    for declaration in declarations:
        counts[declaration.spelling] = counts.get(declaration.spelling, 0) + 1
    scopes, next_scope = {}, 1
    # USR identifies the declaration itself, so two locals both named `i` can still be
    # represented as separate Ref paths without changing names that are already unambiguous.
    for declaration in declarations:
        if counts[declaration.spelling] > 1:
            scopes[declaration.get_usr()] = f"scope{next_scope}"
            next_scope += 1
    return scopes


def _static_names(cursor: Cursor | None, function: str) -> dict:
    """Create stable inventory names for function-local static storage."""
    declarations, counts, names = [], {}, {}

    def collect(current):
        if current.kind == CursorKind.VAR_DECL and _is_static(current):
            declarations.append(current)
        for child in current.get_children():
            collect(child)

    if cursor is not None:
        collect(cursor)
    for declaration in declarations:
        counts[declaration.spelling] = counts.get(declaration.spelling, 0) + 1
        # The qualified name connects the local Ref to the persistent static leaf in Inventory.
        # The ordinal also protects against repeated spellings in nested scopes.
        names[declaration.get_usr()] = f"{function}::{declaration.spelling}#{counts[declaration.spelling]}"
    return names


def _initializer(cursor: Cursor) -> Cursor | None:
    """Find the expression child that is the initializer, ignoring clang metadata children."""
    children = list(cursor.get_children())
    # Prefer INIT_LIST_EXPR because declarations can also contain TYPE_REF/attribute
    # children that appear before or after the real initializer in clang's child list.
    for child in children:
        if child.kind == CursorKind.INIT_LIST_EXPR:
            return child
    if _is_array_type(cursor):
        return None
    for child in reversed(children):
        if child.kind not in (CursorKind.TYPE_REF, CursorKind.ANNOTATE_ATTR):
            return child
    return None


def _is_static(cursor: Cursor) -> bool:
    storage = getattr(cursor, "storage_class", "")
    return getattr(storage, "name", str(storage)).lower() == "static"


def _is_array_type(cursor: Cursor) -> bool:
    kind = getattr(cursor.type.get_canonical().kind, "name", "")
    return kind in {"CONSTANTARRAY", "INCOMPLETEARRAY", "VARIABLEARRAY", "DEPENDENTSIZEDARRAY"}


def _zero_initializer(cursor: Cursor, macros=None) -> bool:
    """Recognize aggregate initializers that are semantically all-zero."""
    for child in cursor.get_children():
        if getattr(child.kind, "name", "") == "IMPLICIT_VALUE_INIT_EXPR":
            continue
        if _literal(child, macros) != 0:
            return False
    return True


def _arms(body: Cursor | None):
    """Collect switch arms while preserving statements that fall through after a label."""
    if body is None:
        return []
    result = []
    active = None
    for child in body.get_children():
        if child.kind == CursorKind.CASE_STMT:
            labels, statements = _case_chain(child)
            active = [labels, list(statements), False]
            result.append(active)
        elif child.kind == CursorKind.DEFAULT_STMT:
            active = [[], list(child.get_children()), True]
            result.append(active)
        elif active is not None:
            # In clang, statements after a CASE_STMT can be siblings rather than children.
            # They still belong to the most recently opened arm until another label appears.
            active[1].append(child)
    arms = []
    for item in result:
        arms.append((item[0], item[1], item[2]))
    return arms


def _case_chain(cursor: Cursor):
    """Unroll stacked labels such as `case 1: case 2: ...` into one arm with two labels."""
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
    """Extract the condition from clang's different while/do/for child layouts."""
    if cursor.kind == CursorKind.WHILE_STMT or cursor.kind == CursorKind.DO_STMT:
        return before_body[0] if before_body else None
    condition = before_body[1] if len(before_body) > 1 else None
    return None if condition is None or condition.kind == CursorKind.NULL_STMT else condition


def _for_part(before_body, index, builder):
    if len(before_body) <= index or before_body[index].kind == CursorKind.NULL_STMT:
        return ()
    return tuple(builder._statement(before_body[index]))


def _unwrap(cursor: Cursor) -> Cursor:
    """Strip one-child AST wrappers that add no source-level operation to the IR."""
    while cursor.kind in _WRAPPERS:
        children = list(cursor.get_children())
        if len(children) != 1:
            break
        cursor = children[0]
    return cursor


def _call_arguments(cursor: Cursor):
    """Return actual CALL_EXPR arguments, excluding clang's optional callee-reference child."""
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


def _literal(cursor: Cursor, macros=None):
    """Best-effort constant evaluation used before falling back to expression IR."""
    cursor = _unwrap(cursor)
    if cursor.kind in _CASTS:
        children = list(cursor.get_children())
        return _literal(children[-1], macros) if children else ""
    if cursor.kind == CursorKind.DECL_REF_EXPR and cursor.referenced is not None:
        if cursor.referenced.kind == CursorKind.ENUM_CONSTANT_DECL:
            return cursor.referenced.enum_value
    if cursor.kind in (CursorKind.INTEGER_LITERAL, CursorKind.CHARACTER_LITERAL):
        # Prefer clang's evaluator because it already follows C literal syntax and target rules.
        try:
            result = cursor.evaluate()
            if result is not None and hasattr(result, "as_int"):
                return result.as_int()
        except Exception:
            pass
    # Macro lookup handles named constants; source folding handles small constant
    # expressions such as shifts/arithmetic that clang did not expose through evaluate().
    macro = _macro_constant(cursor, macros)
    if macro is not None:
        return macro
    folded = _fold_int(_physical_source(cursor), macros)
    if folded is not None:
        return folded
    text = _spelling(cursor)
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


def _literal_type(cursor: Cursor, value) -> str:
    """Preserve unsigned spelling when a large literal would otherwise look like signed int."""
    if not isinstance(value, int) or value <= 0x7FFFFFFF:
        return ""
    spelling = getattr(_unwrap(cursor).type, "spelling", "")
    return spelling if "unsigned" in spelling else ""


def _spelling(cursor: Cursor) -> str:
    spelling = getattr(cursor, "spelling", "").strip()
    if spelling:
        return spelling
    for token in cursor.get_tokens():
        return token.spelling
    return ""


def _physical_source(cursor: Cursor) -> str:
    """Read the exact one-line source slice when constant folding needs original spelling."""
    extent = cursor.extent
    file = extent.start.file
    if file is not None and extent.start.line == extent.end.line:
        line = linecache.getline(str(file), extent.start.line)
        return line[extent.start.column - 1:extent.end.column - 1]
    return _source(cursor)


def _operator(cursor: Cursor) -> str:
    """Recover an operator from cursor spelling or tokens, depending on libclang's AST output."""
    spelling = getattr(cursor, "spelling", "")
    operators = ("&&", "||", "==", "!=", "<=", ">=", "<<=", ">>=", "<<", ">>",
                 "<", ">", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
                 "=", "&", "|", "^", "+", "-", "*", "/", "%", "!", "++", "--", "~")
    if spelling in operators:
        return spelling
    # Operator cursors frequently have an empty spelling, while their token stream still
    # contains the exact C operator. Token fallback makes parsing less version-dependent.
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
    """Clone AtomBuilder configuration but reset copy-propagation state for body conversion."""
    try:
        from utgen.atoms import AtomBuilder, CopyPropagation
        if isinstance(builder, AtomBuilder):
            return AtomBuilder(builder.inventory, CopyPropagation())
    except ImportError:
        pass
    return builder
