
from __future__ import annotations

import re

from clang.cindex import Cursor, CursorKind, TypeKind

from utgen.model import CType, Direction, Inventory, Variable
from utgen.stubs import DEFAULT_NO_STUB, StubFinder
from utgen.typegraph import LeafDeriver, TypeGraph


_INDEX = re.compile(r"\[[^\]]*\]")


class _Inventory(Inventory):
    """Inventory with tolerant lookup for concrete array element names.

    Type derivation may store an array leaf with one index spelling while later
    analysis refers to another concrete index. The fallback matches the same
    base path and array rank without treating different dimensions as equal.
    """

    def find(self, name: str) -> Variable | None:
        variable = super().find(name)
        if variable is not None:
            return variable
        # Ignore concrete subscripts for the fallback match, but preserve the
        # number of dimensions so ``a[i]`` cannot accidentally match ``a[i][j]``.
        normalized = _INDEX.sub("", name)
        rank = len(_INDEX.findall(name))
        for variable in self.variables:
            if (len(_INDEX.findall(variable.name)) == rank and
                    _INDEX.sub("", variable.name) == normalized):
                return variable
        return None


class InventoryBuilder:
    """Build the test-visible variable inventory for one C function.

    The inventory combines function parameters, return values, referenced
    globals, static locals, and variables introduced by stubbed calls.
    """

    def __init__(self, char_signed: bool = False, const_globals: str = "inject",
                 no_stub=DEFAULT_NO_STUB):
        self.const_globals = const_globals
        self.type_graph = TypeGraph(char_signed)
        self.deriver = LeafDeriver(self.type_graph)
        self.stub_finder = StubFinder(self.type_graph, self.deriver, no_stub)
        self._usage: dict[str, list] = {}
        self._order: list[str] = []

    def build(self, func_cursor: Cursor, tu) -> Inventory:
        """Collect every value that UTGen may need to drive or observe."""
        self._usage, self._order = {}, []

        # Scan before creating variables because global direction depends on how
        # each declaration is used by this function: read, written, or both.
        self._scan(func_cursor, writing=False)

        variables = []
        for param in func_cursor.get_arguments():
            # A data pointer gets a separate synthetic ``@name`` input. This lets
            # test generation control pointer validity (for example NULL) apart
            # from the value stored through the pointer.
            address = self._pointer_address(param)
            if address is not None:
                variables.append(address)
            member = self.type_graph.describe(param.type, param.spelling)
            variables.extend(self.deriver.leaves(member, self._param_direction(param)))
        # Model a non-void return value as a normal observable output leaf.
        result_type = func_cursor.result_type
        if result_type.get_canonical().kind != TypeKind.VOID:
            member = self.type_graph.describe(result_type, f"{func_cursor.spelling}()")
            variables.extend(self.deriver.leaves(member, Direction.OUT))
        for decl, direction in self._globals(func_cursor):
            member = self.type_graph.describe(decl.type, decl.spelling)
            variables.extend(self.deriver.leaves(member, direction))
        # Function-local statics are persistent state. They are exposed with a
        # synthetic qualified name so repeated local names remain distinguishable.
        for decl, name in self._statics(func_cursor):
            member = self.type_graph.describe(decl.type, name)
            variables.extend(self.deriver.leaves(member, Direction.IN))
        stubs, stub_vars = self.stub_finder.find(func_cursor)
        variables.extend(stub_vars)
        return _Inventory(variables, stubs)

    @staticmethod
    def _param_direction(param) -> Direction:
        """Infer whether a parameter can only be read or may also be modified."""
        canonical = param.type.get_canonical()
        if canonical.kind != TypeKind.POINTER:
            return Direction.IN
        return (Direction.IN if canonical.get_pointee().is_const_qualified()
                else Direction.INOUT)

    @staticmethod
    def _pointer_address(param) -> Variable | None:
        """Create the synthetic address-control variable for data pointers.

        Function pointers are excluded because they represent callable targets,
        not test data whose NULL/non-NULL address is modeled here.
        """
        canonical = param.type.get_canonical()
        if canonical.kind != TypeKind.POINTER or canonical.get_pointee().get_canonical().kind in (
                TypeKind.FUNCTIONPROTO, TypeKind.FUNCTIONNOPROTO):
            return None
        return Variable(f"@{param.spelling}", CType("pointer", 1, False), "pointer", "1",
                        Direction.IN, True, param.spelling)

    @staticmethod
    def _statics(func_cursor: Cursor) -> list:
        """Find static locals and assign stable names within this function.

        The ``#N`` suffix avoids collisions when different nested scopes declare
        static variables with the same spelling.
        """
        declarations, counts, result = [], {}, []

        def collect(cursor):
            if cursor.kind == CursorKind.VAR_DECL and _is_static(cursor):
                declarations.append(cursor)
            for child in cursor.get_children():
                collect(child)

        collect(func_cursor)
        for declaration in declarations:
            counts[declaration.spelling] = counts.get(declaration.spelling, 0) + 1
            result.append((declaration, f"{func_cursor.spelling}::{declaration.spelling}#{counts[declaration.spelling]}"))
        return result



    def _globals(self, func_cursor: Cursor) -> list:
        result = []
        for usr in self._order:
            decl, read, written = self._usage[usr]
            if decl.kind != CursorKind.VAR_DECL:
                continue
            # In fold mode, const globals are treated as compile-time data and
            # therefore should not become injectable test inputs.
            if self.const_globals == "fold" and decl.type.is_const_qualified():
                continue
            result.append((decl, self._direction_of(self._usage[usr])))
        return result

    def _direction_of(self, entry: list) -> Direction:
        _, read, written = entry
        if read and written:
            return Direction.INOUT
        return Direction.OUT if written else Direction.IN



    def _scan(self, cur: Cursor, writing: bool) -> None:
        """Walk the Clang AST and classify references as reads and/or writes.

        ``writing`` describes the context inherited from the parent expression.
        Assignment-like nodes override that context because their operands have
        different data-flow roles.
        """
        if cur.kind == CursorKind.DECL_REF_EXPR:
            self._record_ref(cur, writing)
            return
        if cur.kind == CursorKind.COMPOUND_ASSIGNMENT_OPERATOR:
            # ``x += y`` consumes the old value of x and then writes the new one,
            # so the left-hand side must be recorded as both read and written.
            lhs, rhs = self._two_children(cur)
            if lhs is not None:
                self._scan(lhs, writing=True)
                self._scan(lhs, writing=False)
                self._scan(rhs, writing=False)
                return
        if cur.kind == CursorKind.BINARY_OPERATOR and self._is_assign(cur):
            # Plain assignment writes the LHS but only reads the RHS.
            lhs, rhs = self._two_children(cur)
            if lhs is not None:
                self._scan(lhs, writing=True)
                self._scan(rhs, writing=False)
                return
        if cur.kind == CursorKind.UNARY_OPERATOR and self._first_token(cur) == "&":
            # Taking an address exposes the object to code reached through the
            # pointer. Treat it conservatively as both readable and writable.
            for child in cur.get_children():
                self._scan(child, writing=True)
                self._scan(child, writing=False)
            return
        for child in cur.get_children():
            self._scan(child, writing)

    def _record_ref(self, cur: Cursor, writing: bool) -> None:
        ref = cur.referenced
        if ref is None:
            return
        # Local automatic variables are internal implementation details; only
        # parameters and translation-unit globals belong in the external inventory.
        is_global = (ref.kind == CursorKind.VAR_DECL and ref.semantic_parent is not None
                     and ref.semantic_parent.kind == CursorKind.TRANSLATION_UNIT)
        if not is_global and ref.kind != CursorKind.PARM_DECL:
            return
        # Clang USR identifies the declaration itself, avoiding collisions when
        # unrelated declarations happen to use the same spelling.
        usr = ref.get_usr()
        entry = self._usage.get(usr)
        if entry is None:
            entry = [ref, False, False]
            self._usage[usr] = entry
            self._order.append(usr)
        if writing:
            entry[2] = True
        else:
            entry[1] = True



    def _is_assign(self, cur: Cursor) -> bool:
        """Recognize ``=`` across libclang versions.

        Prefer libclang's operator metadata when available. Older bindings may
        not expose it reliably, so token inspection is kept as a compatibility
        fallback.
        """
        op = getattr(cur, "binary_operator", None)
        if op is not None and getattr(op, "name", "Invalid") != "Invalid":
            return op.name == "Assign"
        lhs, _ = self._two_children(cur)
        if lhs is None:
            return False
        lhs_end = lhs.extent.end.offset
        for tok in cur.get_tokens():
            if tok.extent.start.offset >= lhs_end:
                return tok.spelling == "="
        return False

    def _two_children(self, cur: Cursor):
        children = list(cur.get_children())
        if len(children) == 2:
            return children[0], children[1]
        return None, None

    def _first_token(self, cur: Cursor) -> str:
        for tok in cur.get_tokens():
            return tok.spelling
        return ""


def _is_static(cursor: Cursor) -> bool:
    storage = getattr(cursor, "storage_class", "")
    return getattr(storage, "name", str(storage)).lower() == "static"
