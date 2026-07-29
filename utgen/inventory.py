"""Per-function variable inventory: params, return value, globals.

Leaf explosion (nested structs, arrays, unions, enums, bitfields, pointers)
is delegated to the type graph (typegraph.py); this module decides WHICH
entities become leaves and with which direction.
"""
from __future__ import annotations

from clang.cindex import Cursor, CursorKind, TypeKind

from utgen.model import CType, Direction, Inventory, Variable
from utgen.stubs import DEFAULT_NO_STUB, StubFinder
from utgen.typegraph import LeafDeriver, TypeGraph


class InventoryBuilder:
    """Builds the injectable/observable leaf inventory of one function."""

    def __init__(self, char_signed: bool = False, const_globals: str = "inject",
                 no_stub=DEFAULT_NO_STUB):
        self.const_globals = const_globals      # "inject" | "fold" (B19)
        self.type_graph = TypeGraph(char_signed)
        self.deriver = LeafDeriver(self.type_graph)
        self.stub_finder = StubFinder(self.type_graph, self.deriver, no_stub)
        self._usage: dict[str, list] = {}       # decl usr -> [decl, read, written]
        self._order: list[str] = []

    def build(self, func_cursor: Cursor, tu) -> Inventory:
        self._usage, self._order = {}, []
        self._scan(func_cursor, writing=False)  # fills global + param usage

        variables = []
        for param in func_cursor.get_arguments():
            address = self._pointer_address(param)
            if address is not None:
                variables.append(address)
            member = self.type_graph.describe(param.type, param.spelling)
            variables.extend(self.deriver.leaves(member, Direction.IN))
        result_type = func_cursor.result_type
        if result_type.get_canonical().kind != TypeKind.VOID:
            member = self.type_graph.describe(result_type, f"{func_cursor.spelling}()")
            variables.extend(self.deriver.leaves(member, Direction.OUT))
        for decl, direction in self._globals(func_cursor):
            member = self.type_graph.describe(decl.type, decl.spelling)
            variables.extend(self.deriver.leaves(member, direction))
        for decl, name in self._statics(func_cursor):
            member = self.type_graph.describe(decl.type, name)
            variables.extend(self.deriver.leaves(member, Direction.IN))
        stubs, stub_vars = self.stub_finder.find(func_cursor)
        variables.extend(stub_vars)
        return Inventory(variables, stubs)

    @staticmethod
    def _pointer_address(param) -> Variable | None:
        canonical = param.type.get_canonical()
        if canonical.kind != TypeKind.POINTER or canonical.get_pointee().get_canonical().kind in (
                TypeKind.FUNCTIONPROTO, TypeKind.FUNCTIONNOPROTO):
            return None
        return Variable(f"@{param.spelling}", CType("pointer", 1, False), "pointer", "1",
                        Direction.IN, True, param.spelling)

    @staticmethod
    def _statics(func_cursor: Cursor) -> list:
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

    # --- directions from usage ---

    def _globals(self, func_cursor: Cursor) -> list:
        result = []
        for usr in self._order:
            decl, read, written = self._usage[usr]
            if decl.kind != CursorKind.VAR_DECL:
                continue                        # param usage lives in the same table
            if self.const_globals == "fold" and decl.type.is_const_qualified():
                continue
            result.append((decl, self._direction_of(self._usage[usr])))
        return result

    def _direction_of(self, entry: list) -> Direction:
        _, read, written = entry
        if read and written:
            return Direction.INOUT
        return Direction.OUT if written else Direction.IN

    # --- body scan: read/write usage of globals and params ---

    def _scan(self, cur: Cursor, writing: bool) -> None:
        if cur.kind == CursorKind.DECL_REF_EXPR:
            self._record_ref(cur, writing)
            return
        if cur.kind == CursorKind.COMPOUND_ASSIGNMENT_OPERATOR:
            lhs, rhs = self._two_children(cur)
            if lhs is not None:
                self._scan(lhs, writing=True)
                self._scan(lhs, writing=False)  # compound also reads the LHS
                self._scan(rhs, writing=False)
                return
        if cur.kind == CursorKind.BINARY_OPERATOR and self._is_assign(cur):
            lhs, rhs = self._two_children(cur)
            if lhs is not None:
                self._scan(lhs, writing=True)
                self._scan(rhs, writing=False)
                return
        if cur.kind == CursorKind.UNARY_OPERATOR and self._first_token(cur) == "&":
            for child in cur.get_children():    # address escapes: read+write
                self._scan(child, writing=True)
                self._scan(child, writing=False)
            return
        for child in cur.get_children():
            self._scan(child, writing)

    def _record_ref(self, cur: Cursor, writing: bool) -> None:
        ref = cur.referenced
        if ref is None:
            return
        is_global = (ref.kind == CursorKind.VAR_DECL and ref.semantic_parent is not None
                     and ref.semantic_parent.kind == CursorKind.TRANSLATION_UNIT)
        if not is_global and ref.kind != CursorKind.PARM_DECL:
            return                              # locals are not injectable entities
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

    # --- clang helpers (binary_operator with token fallback, K8) ---

    def _is_assign(self, cur: Cursor) -> bool:
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
