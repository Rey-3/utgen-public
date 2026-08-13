"""Map clang condition expressions to controllable boolean atoms."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from clang.cindex import Cursor, CursorKind

from utgen.model import And, Atom, Direction, Inventory, Not, Op, Or, Variable

_RELATIONS = {"==": Op.EQ, "!=": Op.NE, "<": Op.LT,
              "<=": Op.LE, ">": Op.GT, ">=": Op.GE}
_WRAPPERS = {CursorKind.UNEXPOSED_EXPR, CursorKind.PAREN_EXPR,
             getattr(CursorKind, "CSTYLE_CAST_EXPR", None)}
for _kind_name in ("IMPLICIT_CAST_EXPR", "CXX_STATIC_CAST_EXPR"):
    _kind = getattr(CursorKind, _kind_name, None)
    if _kind is not None:
        _WRAPPERS.add(_kind)
_WRAPPERS.discard(None)


@dataclass(frozen=True)
class _Value:
    """Resolved condition operand."""

    variable: Variable | None = None
    constant: int | None = None
    node: object = None


class CopyPropagation:
    """Track ordered local/member assignments for condition resolution."""

    def __init__(self):
        self._bindings: dict[str, Cursor | None] = {}
        self._member_bindings: dict[str, Cursor | None] = {}
        self._locals: set[str] = set()
        self._loop_written: set[str] = set()
        self._static_member_paths: set[str] = set()

    def record(self, stmt_cursor: Cursor) -> None:
        """Record declarations and assignments appearing before a condition."""
        kind = stmt_cursor.kind
        if kind == CursorKind.FUNCTION_DECL:
            for child in stmt_cursor.get_children():
                if child.kind == CursorKind.COMPOUND_STMT:
                    self.record(child)
            return
        if kind == CursorKind.COMPOUND_STMT:
            for child in stmt_cursor.get_children():
                self.record(child)
            return
        if kind == CursorKind.DECL_STMT:
            for child in stmt_cursor.get_children():
                if child.kind == CursorKind.VAR_DECL:
                    key = _cursor_key(child)
                    if key:
                        self._locals.add(key)
                    init = _last_non_type_child(child)
                    if init is not None:
                        self._set(child, init)
            return
        if kind in (CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
            if _operator(stmt_cursor) in ("=", "+=", "-=", "*=", "/=", "%="):
                children = list(stmt_cursor.get_children())
                if children:
                    self._set(children[0], None if _operator(stmt_cursor) != "=" else children[-1])
                return
        if kind in (CursorKind.WHILE_STMT, CursorKind.DO_STMT, CursorKind.FOR_STMT):
            body = list(stmt_cursor.get_children())[-1:]
            for child in body:
                self._mark_loop_writes(child)
            return
        for child in stmt_cursor.get_children():
            if child.kind in (CursorKind.DECL_STMT, CursorKind.BINARY_OPERATOR,
                              CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
                self.record(child)

    def resolve(self, expr_cursor: Cursor) -> str | None:
        """Resolve an expression to a source path when it is copy-propagatable."""
        value = self.resolve_expr(expr_cursor)
        if isinstance(value, Cursor):
            return _raw_path(value)
        return value if isinstance(value, str) else None

    def resolve_expr(self, expr_cursor: Cursor, seen: set[str] | None = None):
        """Return the latest source cursor for an expression or ``None``."""
        seen = set() if seen is None else seen
        expr_cursor = _unwrap(expr_cursor)
        path = _raw_path(expr_cursor)
        if path and path in self._loop_written:
            return None
        member_binding = self._member_bindings.get(path) if path else None
        if path in self._member_bindings:
            return self._follow(member_binding, seen)
        key = _cursor_key(expr_cursor)
        if key not in self._bindings:
            if expr_cursor.kind == CursorKind.UNARY_OPERATOR and _operator(expr_cursor) in ("&", "*"):
                children = list(expr_cursor.get_children())
                if children:
                    return self.resolve_expr(children[0], seen)
            if expr_cursor.kind == CursorKind.MEMBER_REF_EXPR:
                children = list(expr_cursor.get_children())
                if children:
                    base = self.resolve_expr(children[0], seen)
                    base_path = _raw_path(base) if isinstance(base, Cursor) else base
                    if base_path:
                        return f"{base_path}.{expr_cursor.spelling}"
            if expr_cursor.kind == CursorKind.ARRAY_SUBSCRIPT_EXPR:
                children = list(expr_cursor.get_children())
                if children:
                    base = self.resolve_expr(children[0], seen)
                    base_path = _raw_path(base) if isinstance(base, Cursor) else base
                    if base_path:
                        return f"{base_path}[*]"
            return expr_cursor
        return self._follow(self._bindings[key], seen)

    def _follow(self, value, seen: set[str]):
        if value is None or not isinstance(value, Cursor):
            return value
        key = _cursor_key(value) or _raw_path(value)
        if not key:
            return value
        if key in seen:
            return None
        seen.add(key)
        path = _raw_path(value)
        if path in self._member_bindings:
            return self._follow(self._member_bindings[path], seen)
        if key in self._bindings:
            return self._follow(self._bindings[key], seen)
        return value

    def snapshot(self):
        """Capture bindings before taking a control-flow branch."""
        return (self._bindings.copy(), self._member_bindings.copy(), self._locals.copy(),
                self._loop_written.copy(), self._static_member_paths.copy())

    def restore(self, state) -> None:
        """Restore a previously captured binding state."""
        self._bindings, self._member_bindings, self._locals, self._loop_written = (
            state[0].copy(), state[1].copy(), state[2].copy(), state[3].copy())
        self._static_member_paths = state[4].copy()

    def merge(self, *states):
        """Keep only bindings unchanged on every reachable branch."""
        if not states:
            return self.snapshot()
        bindings = _common_bindings(*(state[0] for state in states))
        members = _common_bindings(*(state[1] for state in states))
        static_paths = set().union(*(state[4] for state in states))
        for path in static_paths:
            sources = [state[1][path] for state in states if path in state[1]]
            if sources and all(source is sources[0] for source in sources):
                members[path] = sources[0]
            else:
                members.pop(path, None)
        return (bindings, members, set().union(*(state[2] for state in states)),
                set().union(*(state[3] for state in states)), static_paths)

    def _set(self, lhs: Cursor, rhs: Cursor | None) -> None:
        key = _cursor_key(lhs)
        if key and not _is_static(lhs):
            self._bindings[key] = rhs
        path = _raw_path(lhs)
        if path and self._is_local(lhs):
            self._member_bindings[path] = rhs
            if _is_static(lhs):
                self._static_member_paths.add(path)

    def _is_local(self, cursor: Cursor) -> bool:
        return (_cursor_key(_root(cursor)) or "") in self._locals

    def _mark_loop_writes(self, cursor: Cursor) -> None:
        if cursor.kind in (CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
            if _operator(cursor) in ("=", "+=", "-=", "*=", "/=", "%="):
                children = list(cursor.get_children())
                if children:
                    key = _cursor_key(children[0]) or _raw_path(children[0])
                    if key:
                        self._loop_written.add(key)
                    path = _raw_path(children[0])
                    if path:
                        self._loop_written.add(path)
        for child in cursor.get_children():
            self._mark_loop_writes(child)


class AtomBuilder:
    """Build boolean atom trees mapped to an ``Inventory``."""

    def __init__(self, inventory: Inventory, copyprop: CopyPropagation | None = None, macros=None):
        self.inventory = inventory
        self.copyprop = copyprop or CopyPropagation()
        self._macro_values = {}
        for macro in macros or ():
            self._macro_values[macro.name] = macro.value if macro.value is not None else macro.raw
        self.constants: dict[str, bool] = {}
        self.uncontrollable: list[str] = []

    def build(self, cond_cursor: Cursor):
        """Build an ``Atom``/``And``/``Or``/``Not`` tree for a condition."""
        return self._node(cond_cursor)

    def build_case(self, subject_cursor: Cursor, label_cursor: Cursor) -> Atom:
        """Build a switch-label equality using the real subject expression."""
        subject = self._value(subject_cursor)
        label = _constant(_unwrap(label_cursor))
        if label is None:
            label = _macro_constant(_unwrap(label_cursor), self._macro_values)
        text = f"{_source(subject_cursor)} == {_source(label_cursor)}"
        if subject.variable is not None and label is not None:
            return Atom(subject.variable, Op.EQ, rhs=label, text=text)
        if subject.constant is not None and label is not None:
            self.constants[text] = subject.constant == label
        if text not in self.uncontrollable:
            self.uncontrollable.append(text)
        return Atom(None, Op.EQ, rhs=label, text=text)

    def _node(self, cursor: Cursor):
        cursor = _unwrap(cursor)
        kind = cursor.kind
        if kind == CursorKind.BINARY_OPERATOR:
            children = list(cursor.get_children())
            op = _operator(cursor)
            if op in ("&&", "||") and len(children) == 2:
                nodes = []
                for child in children:
                    nodes.append(self._node(child))
                return And(tuple(nodes)) if op == "&&" else Or(tuple(nodes))
            if op in _RELATIONS and len(children) == 2:
                return self._relation(cursor, children[0], children[1], _RELATIONS[op])
            if op == "&" and len(children) == 2:
                left = self._value(children[0])
                right = self._value(children[1])
                if left.variable is not None and right.constant is not None:
                    return Atom(left.variable, Op.BITSET, rhs=right.constant,
                                text=_source(cursor))
            return self._unknown(cursor)
        if kind == CursorKind.UNARY_OPERATOR:
            children = list(cursor.get_children())
            op = _operator(cursor)
            if op == "!" and children:
                return Not(self._node(children[0]))
            return self._unknown(cursor)
        value = self._value(cursor)
        if value.variable is not None:
            return Atom(value.variable, Op.TRUTHY, text=_source(cursor))
        if value.constant is not None:
            result = value.constant != 0
            self.constants[_source(cursor)] = result
            return Atom(None, Op.TRUTHY, text=_source(cursor))
        if value.node is not None:
            return value.node
        return self._unknown(cursor)

    def _relation(self, cursor: Cursor, left_cursor: Cursor, right_cursor: Cursor, op: Op):
        left = self._value(left_cursor)
        right = self._value(right_cursor)
        if left.variable is not None:
            if right.constant is not None:
                return Atom(left.variable, op, rhs=right.constant, text=_source(cursor))
            if right.variable is not None:
                return Atom(left.variable, op, rhs_var=right.variable, text=_source(cursor))
        if left.constant is not None and right.constant is not None:
            result = _compare(left.constant, right.constant, op)
            self.constants[_source(cursor)] = result
        bit_test = self._bit_relation(cursor, left, right, op)
        if bit_test is not None:
            return bit_test
        linear = self._linear(left_cursor)
        if linear is not None and right.constant is not None:
            variable, offset = linear
            return Atom(variable, op, rhs=right.constant - offset, text=_source(cursor))
        return self._unknown(cursor)

    @staticmethod
    def _bit_relation(cursor: Cursor, left: _Value, right: _Value, op: Op):
        bit = left.node if isinstance(left.node, Atom) and left.node.op == Op.BITSET else None
        if bit is None or right.constant is None or bit.rhs is None:
            return None
        atom = Atom(bit.var, Op.BITSET, rhs=bit.rhs, text=_source(cursor))
        single = bit.rhs & (bit.rhs - 1) == 0
        if right.constant == bit.rhs and single:
            return atom if op == Op.EQ else Not(atom) if op == Op.NE else None
        if right.constant == 0:
            return Not(atom) if op == Op.EQ else atom if op == Op.NE else None
        return None

    def _value(self, cursor: Cursor) -> _Value:
        original = cursor
        resolved = self.copyprop.resolve_expr(cursor)
        if resolved is None:
            return _Value()
        if isinstance(resolved, str):
            variable = self._find_variable(resolved)
            return _Value(variable=variable) if variable is not None else _Value()
        if isinstance(resolved, Cursor) and resolved is not cursor:
            cursor = resolved
        cursor = _unwrap(cursor)
        constant = _constant(cursor)
        if constant is None:
            constant = _macro_constant(cursor, self._macro_values)
        if constant is not None:
            return _Value(constant=constant)
        path = _raw_path(cursor)
        variable = self._find_variable(path, cursor.kind == CursorKind.DECL_REF_EXPR)
        if variable is not None:
            return _Value(variable=variable)
        if cursor.kind == CursorKind.BINARY_OPERATOR:
            node = self._node(cursor)
            if not isinstance(node, Atom) or node.var is not None:
                return _Value(node=node)
        if cursor.kind == CursorKind.CALL_EXPR:
            name = _callee_name(cursor)
            variable = self._find_variable(f"tessy_{name}") if name else None
            if variable is not None:
                return _Value(variable=variable)
        if original is not cursor:
            return self._value(cursor)
        return _Value()

    def _linear(self, cursor: Cursor):
        cursor = _unwrap(cursor)
        value = self._value(cursor)
        if value.variable is not None:
            return value.variable, 0
        if cursor.kind != CursorKind.BINARY_OPERATOR:
            return None
        children = list(cursor.get_children())
        op = _operator(cursor)
        if len(children) != 2 or op not in ("+", "-"):
            return None
        left = self._linear(children[0])
        right = self._value(children[1])
        if left is not None and right.constant is not None:
            offset = left[1] + right.constant if op == "+" else left[1] - right.constant
            return left[0], offset
        if op == "+":
            right_linear = self._linear(children[1])
            left_constant = self._value(children[0])
            if right_linear is not None and left_constant.constant is not None:
                return right_linear[0], right_linear[1] + left_constant.constant
        return None

    def _find_variable(self, path: str | None, address=False) -> Variable | None:
        if not path:
            return None
        if address:
            for variable in self.inventory.variables:
                if variable.address_of == path:
                    return variable
        direct = self.inventory.find(path)
        if direct is not None:
            return direct
        normalized = re.sub(r"\[[^\]]*\]", "", path)
        for variable in self.inventory.variables:
            if re.sub(r"\[[^\]]*\]", "", variable.name) == normalized:
                return variable
            static = re.sub(r"::([^.#]+)#\d+", r"::\1", variable.name)
            if re.sub(r"\[[^\]]*\]", "", static).endswith(f"::{normalized}"):
                return variable
        return None

    def _unknown(self, cursor: Cursor) -> Atom:
        text = _source(cursor)
        if text not in self.uncontrollable:
            self.uncontrollable.append(text)
        return Atom(None, Op.TRUTHY, text=text)


def _unwrap(cursor: Cursor) -> Cursor:
    """Remove clang expression wrappers while preserving calls and literals."""
    while cursor.kind in _WRAPPERS:
        children = []
        for child in cursor.get_children():
            if child.kind not in (CursorKind.TYPE_REF, CursorKind.ANNOTATE_ATTR):
                children.append(child)
        if len(children) != 1:
            break
        cursor = children[0]
    return cursor


def _last_non_type_child(cursor: Cursor) -> Cursor | None:
    children = list(cursor.get_children())
    for child in reversed(children):
        if child.kind != CursorKind.TYPE_REF:
            return child
    return None


def _root(cursor: Cursor) -> Cursor:
    cursor = _unwrap(cursor)
    while cursor.kind in (CursorKind.MEMBER_REF_EXPR, CursorKind.ARRAY_SUBSCRIPT_EXPR):
        children = list(cursor.get_children())
        if not children:
            break
        cursor = _unwrap(children[0])
    return cursor


def _is_static(cursor: Cursor) -> bool:
    root = _root(cursor)
    declaration = root.referenced if root.kind == CursorKind.DECL_REF_EXPR else root
    storage = getattr(declaration, "storage_class", "")
    return getattr(storage, "name", str(storage)).lower() == "static"


def _common_bindings(*mappings):
    """Return entries whose cursor binding is identical in every mapping."""
    if not mappings:
        return {}
    common = {}
    for key, value in mappings[0].items():
        if all(key in mapping and mapping[key] is value for mapping in mappings[1:]):
            common[key] = value
    return common


def _cursor_key(cursor: Cursor) -> str | None:
    if cursor.kind == CursorKind.DECL_REF_EXPR and cursor.referenced is not None:
        return cursor.referenced.get_usr()
    if cursor.kind == CursorKind.VAR_DECL:
        return cursor.get_usr()
    return None


def _raw_path(cursor: Cursor) -> str:
    cursor = _unwrap(cursor)
    if cursor.kind in (CursorKind.DECL_REF_EXPR, CursorKind.VAR_DECL):
        return cursor.spelling
    if cursor.kind == CursorKind.MEMBER_REF_EXPR:
        children = list(cursor.get_children())
        base = _raw_path(children[0]) if children else ""
        return f"{base}.{cursor.spelling}" if base else cursor.spelling
    if cursor.kind == CursorKind.ARRAY_SUBSCRIPT_EXPR:
        children = list(cursor.get_children())
        return f"{_raw_path(children[0])}[*]" if children else ""
    if cursor.kind == CursorKind.UNARY_OPERATOR and _operator(cursor) in ("&", "*"):
        children = list(cursor.get_children())
        return _raw_path(children[0]) if children else ""
    if cursor.kind == CursorKind.CALL_EXPR:
        name = _callee_name(cursor)
        return f"tessy_{name}" if name else ""
    return ""


def _callee_name(cursor: Cursor) -> str:
    callee = cursor.referenced
    return callee.spelling if callee is not None else cursor.spelling


def _constant(cursor: Cursor) -> int | None:
    cursor = _unwrap(cursor)
    referenced = cursor.referenced
    if referenced is not None and referenced.kind == CursorKind.ENUM_CONSTANT_DECL:
        return referenced.enum_value
    if cursor.kind == CursorKind.UNARY_OPERATOR:
        children = list(cursor.get_children())
        value = _constant(children[0]) if len(children) == 1 else None
        op = _operator(cursor)
        if value is not None:
            if op == "+":
                return value
            if op == "-":
                return -value
            if op == "~":
                return ~value
    if cursor.kind == CursorKind.BINARY_OPERATOR:
        children = list(cursor.get_children())
        if len(children) == 2:
            left = _constant(children[0])
            right = _constant(children[1])
            op = _operator(cursor)
            if left is not None and right is not None:
                if op == "+":
                    return left + right
                if op == "-":
                    return left - right
                if op == "*":
                    return left * right
                if op == "/" and right:
                    quotient = abs(left) // abs(right)
                    return -quotient if (left < 0) != (right < 0) else quotient
                if op == "%" and right:
                    return left - (int(abs(left) // abs(right)) * (1 if left >= 0 else -1)) * right
                if op == "&":
                    return left & right
                if op == "|":
                    return left | right
                if op == "^":
                    return left ^ right
                if op == "<<" and right >= 0:
                    return left << right
                if op == ">>" and right >= 0:
                    return left >> right
    try:
        result = cursor.evaluate()
        if result is not None and hasattr(result, "as_int"):
            return result.as_int()
    except Exception:
        pass
    text = _source(cursor).strip().rstrip("uUlL")
    if len(text) >= 3 and text[0] == "'" and text[-1] == "'":
        return ord(text[1])
    try:
        return int(text, 0)
    except ValueError:
        return None


def _macro_constant(cursor: Cursor, macros=None) -> int | None:
    """Resolve an object-like numeric macro behind an expanded literal."""
    file = cursor.location.file
    if file is None or cursor.location.line < 1:
        return None
    try:
        with open(str(file), "r", encoding="utf-8", errors="ignore") as handle:
            source = handle.read()
    except OSError:
        return None
    lines = source.splitlines()
    if cursor.location.line > len(lines):
        return None
    line = lines[cursor.location.line - 1]
    column = cursor.location.column - 1
    name = None
    for match in re.finditer(r"[A-Za-z_]\w*", line):
        if match.start() <= column < match.end():
            name = match.group(0)
            break
    if name is None:
        return None
    if macros is not None and name in macros:
        value = macros[name]
        value = getattr(value, "value", value)
        return value if isinstance(value, int) else _fold_int(value, macros, (name,))
    for match in re.finditer(r"^\s*#define\s+(\w+)\s+(.+)$", source, re.MULTILINE):
        if match.group(1) != name:
            continue
        return _fold_int(match.group(2), {name: match.group(2)}, (name,))
    return None


def _fold_int(raw, macros=None, seen=()):
    """Fold a small C integer expression used by object-like macros."""
    text = re.sub(r"/\*.*?\*/|//.*", "", str(raw), flags=re.S).strip()
    text = re.sub(r"\(\s*(?:const\s+)?[A-Za-z_]\w*\s*\*\s*\)", "", text)
    text = re.sub(r"\(\s*(?:unsigned\s+|signed\s+)?(?:char|short|int|long|bool|void|uint\d+|sint\d+)\s*\)", "", text)
    text = re.sub(r"(0[xX][0-9A-Fa-f]+|\d+)[uUlL]+\b", r"\1", text)
    try:
        node = ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError):
        return None
    return _fold_node(node, macros or {}, seen)


def _fold_node(node, macros, seen):
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name) and node.id in macros and node.id not in seen:
        value = macros[node.id]
        value = getattr(value, "value", value)
        return value if isinstance(value, int) else _fold_int(value, macros, seen + (node.id,))
    if isinstance(node, ast.UnaryOp):
        value = _fold_node(node.operand, macros, seen)
        if value is not None and isinstance(node.op, ast.UAdd):
            return value
        if value is not None and isinstance(node.op, ast.USub):
            return -value
        if value is not None and isinstance(node.op, ast.Invert):
            return ~value
    if isinstance(node, ast.BinOp):
        left = _fold_node(node.left, macros, seen)
        right = _fold_node(node.right, macros, seen)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, (ast.Div, ast.FloorDiv)) and right:
            quotient = abs(left) // abs(right)
            return -quotient if (left < 0) != (right < 0) else quotient
        if isinstance(node.op, ast.Mod) and right:
            quotient = abs(left) // abs(right)
            return left - (quotient if left >= 0 else -quotient) * right
        if isinstance(node.op, ast.BitAnd):
            return left & right
        if isinstance(node.op, ast.BitOr):
            return left | right
        if isinstance(node.op, ast.BitXor):
            return left ^ right
        if isinstance(node.op, ast.LShift) and right >= 0:
            return left << right
        if isinstance(node.op, ast.RShift) and right >= 0:
            return left >> right
    return None


def _operator(cursor: Cursor) -> str:
    spelling = getattr(cursor, "spelling", "")
    operators = ("&&", "||", "==", "!=", "<", "<=", ">", ">=", "&", "|", "^",
                 "+", "-", "*", "/", "%", "<<", ">>", "=", "+=", "-=", "*=", "/=", "%=",
                 "!", "++", "--", "~")
    if spelling in operators:
        return spelling
    for token in cursor.get_tokens():
        if token.spelling in operators:
            return token.spelling
    return ""


def _compare(left: int, right: int, op: Op) -> bool:
    if op == Op.EQ:
        return left == right
    if op == Op.NE:
        return left != right
    if op == Op.LT:
        return left < right
    if op == Op.LE:
        return left <= right
    if op == Op.GT:
        return left > right
    return left >= right


def _source(cursor: Cursor) -> str:
    return " ".join(token.spelling for token in cursor.get_tokens()).strip()
