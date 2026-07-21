"""Per-function variable inventory: params, return value, globals (Task 3 scope).

Struct params/globals explode into member leaves (`test.isvalid`). Deep types
(arrays, unions, bitfields, pointers, enums) land in Task 4 via the type graph;
until then unsupported leaf types are skipped, never crash (K5).
"""
from __future__ import annotations

from clang.cindex import Cursor, CursorKind, TypeKind

from utgen.model import CType, Direction, Inventory, Variable

_SIGNED = {TypeKind.SCHAR, TypeKind.SHORT, TypeKind.INT,
           TypeKind.LONG, TypeKind.LONGLONG}
_UNSIGNED = {TypeKind.UCHAR, TypeKind.USHORT, TypeKind.UINT,
             TypeKind.ULONG, TypeKind.ULONGLONG, TypeKind.BOOL}
_PLAIN_CHAR = {TypeKind.CHAR_S, TypeKind.CHAR_U}


def _clean(spelling: str) -> str:
    """Type name without const/volatile qualifiers (B19)."""
    out = spelling
    for word in ("const ", "volatile "):
        out = out.replace(word, "")
    return out.strip()


class InventoryBuilder:
    """Builds the injectable/observable leaf inventory of one function."""

    def __init__(self, char_signed: bool = False, const_globals: str = "inject"):
        self.char_signed = char_signed          # plain `char` signedness (K8)
        self.const_globals = const_globals      # "inject" | "fold" (B19)

    def build(self, func_cursor: Cursor, tu) -> Inventory:
        variables: list[Variable] = []
        for param in func_cursor.get_arguments():
            self._add_leaves(variables, param.spelling, param.type, Direction.IN)
        result_type = func_cursor.result_type
        if result_type.get_canonical().kind != TypeKind.VOID:
            self._add_leaves(variables, f"{func_cursor.spelling}()",
                             result_type, Direction.OUT)
        for decl, direction in self._globals_of(func_cursor):
            self._add_leaves(variables, decl.spelling, decl.type, direction)
        return Inventory(variables, [])

    # --- leaf building ---

    def _add_leaves(self, out: list, base: str, ctype, direction: Direction,
                    fmt: str | None = None) -> None:
        canon = ctype.get_canonical()
        if canon.kind == TypeKind.RECORD:
            # Exploded members carry the OUTERMOST struct type name (existing convention).
            parent_fmt = fmt or _clean(ctype.spelling)
            for field in canon.get_fields():
                self._add_leaves(out, f"{base}.{field.spelling}", field.type,
                                 direction, fmt=parent_fmt)
            return
        resolved = self._resolve(ctype)
        if resolved is None:
            return                              # Task 4 scope (pointer/array/enum/...)
        sign_word = "signed" if resolved.signed else "unsigned"
        out.append(Variable(base, resolved, fmt or sign_word,
                            str(resolved.bits), direction))

    def _resolve(self, ctype) -> CType | None:
        canon = ctype.get_canonical()
        if canon.kind in _PLAIN_CHAR:
            signed = self.char_signed
        elif canon.kind in _SIGNED:
            signed = True
        elif canon.kind in _UNSIGNED:
            signed = False
        else:
            return None
        return CType(_clean(ctype.spelling), canon.get_size() * 8, signed)

    # --- global usage analysis ---

    def _globals_of(self, func_cursor: Cursor) -> list:
        """File-scope variables used by the function, in first-use order.

        read -> IN, written -> OUT, both -> INOUT. Taking a global's address
        counts as read+write (the callee may do either).
        """
        seen: dict[str, list] = {}              # usr -> [decl, read, written]
        order: list[str] = []
        self._scan(func_cursor, seen, order, writing=False)
        result = []
        for usr in order:
            decl, read, written = seen[usr]
            if self.const_globals == "fold" and decl.type.is_const_qualified():
                continue
            if read and written:
                direction = Direction.INOUT
            elif written:
                direction = Direction.OUT
            else:
                direction = Direction.IN
            result.append((decl, direction))
        return result

    def _scan(self, cur: Cursor, seen: dict, order: list, writing: bool) -> None:
        if cur.kind == CursorKind.DECL_REF_EXPR:
            self._record_ref(cur, seen, order, writing)
            return
        if cur.kind == CursorKind.COMPOUND_ASSIGNMENT_OPERATOR:
            lhs, rhs = self._two_children(cur)
            if lhs is not None:
                self._scan(lhs, seen, order, writing=True)
                self._scan(lhs, seen, order, writing=False)   # compound also reads
                self._scan(rhs, seen, order, writing=False)
                return
        if cur.kind == CursorKind.BINARY_OPERATOR and self._is_assign(cur):
            lhs, rhs = self._two_children(cur)
            if lhs is not None:
                self._scan(lhs, seen, order, writing=True)
                self._scan(rhs, seen, order, writing=False)
                return
        if cur.kind == CursorKind.UNARY_OPERATOR and self._first_token(cur) == "&":
            for child in cur.get_children():
                self._scan(child, seen, order, writing=True)
                self._scan(child, seen, order, writing=False)
            return
        for child in cur.get_children():
            self._scan(child, seen, order, writing)

    def _record_ref(self, cur: Cursor, seen: dict, order: list, writing: bool) -> None:
        ref = cur.referenced
        if ref is None or ref.kind != CursorKind.VAR_DECL:
            return
        parent = ref.semantic_parent
        if parent is None or parent.kind != CursorKind.TRANSLATION_UNIT:
            return                              # locals/params are not globals
        usr = ref.get_usr()
        entry = seen.get(usr)
        if entry is None:
            entry = [ref, False, False]
            seen[usr] = entry
            order.append(usr)
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
