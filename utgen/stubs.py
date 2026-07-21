"""Stub discovery: called functions -> StubInfo records + tessy_ leaf Variables.

Follows the existing pipeline's conventions:
- every called function is stubbed, same-module callees included (E7),
  deduped by name in call order (E4), minus the no_stub list (E6);
- non-void stubs contribute `tessy_<fn>` IN leaves (injected return) and
  `tessy_<fn>__ArgN` OUT capture leaves for every parameter — pointees are
  captured by value, so capture leaves are never pointers (D7);
- void stubs contribute a StubInfo only, no leaves (E3).
"""
from __future__ import annotations

from clang.cindex import Cursor, CursorKind, TypeKind

from utgen.model import CType, Direction, StubInfo, Variable
from utgen.typegraph import LeafDeriver, TypeGraph

DEFAULT_NO_STUB = ("memcpy", "memset", "memcmp", "abs")


class StubFinder:
    """Finds called functions and derives their tessy_ variables."""

    def __init__(self, graph: TypeGraph, deriver: LeafDeriver,
                 no_stub=DEFAULT_NO_STUB):
        self.graph = graph
        self.deriver = deriver
        self.no_stub = set(no_stub)

    def find(self, func_cursor: Cursor) -> tuple[list, list]:
        """Returns (stubs, tessy leaf Variables) for one function body."""
        stubs: list[StubInfo] = []
        variables: list[Variable] = []
        seen: set[str] = set()
        self._walk(func_cursor, func_cursor.spelling, seen, stubs, variables)
        return stubs, variables

    def _walk(self, cur: Cursor, self_name: str, seen: set,
              stubs: list, variables: list) -> None:
        if cur.kind == CursorKind.CALL_EXPR:
            self._record(cur, self_name, seen, stubs, variables)
        for child in cur.get_children():
            self._walk(child, self_name, seen, stubs, variables)

    def _record(self, cur: Cursor, self_name: str, seen: set,
                stubs: list, variables: list) -> None:
        callee = cur.referenced
        name = callee.spelling if callee is not None else cur.spelling
        if not name or name == self_name or name in self.no_stub or name in seen:
            return                              # recursion out of scope; E4 dedupe; E6
        seen.add(name)
        stubs.append(self._stub_info(name, callee))
        if callee is not None:
            variables.extend(self._tessy_leaves(name, callee))

    def _stub_info(self, name: str, callee: Cursor | None) -> StubInfo:
        if callee is None:
            return StubInfo(name, None, None, [])   # unresolved callee: record only (K5)
        ret_ctype, ret_struct = self._return_of(callee)
        params = []
        for param in callee.get_arguments():
            params.append((param.type.spelling, param.spelling))
        return StubInfo(name, ret_ctype, ret_struct, params)

    def _return_of(self, callee: Cursor) -> tuple[CType | None, str | None]:
        member = self.graph.describe(callee.result_type, "")
        if member.kind in ("struct", "union"):
            return None, member.type_name
        if member.kind in ("scalar", "enum", "pointer"):
            enum = self.graph.enums.get(member.type_name)
            values = enum.values if enum else ()
            return CType(member.type_name, member.len_bits, member.signed, values), None
        return None, None                       # void / unsupported

    def _tessy_leaves(self, name: str, callee: Cursor) -> list:
        if callee.result_type.get_canonical().kind == TypeKind.VOID:
            return []                           # E3: void stubs inject/capture nothing
        out: list[Variable] = []
        ret_member = self.graph.describe(callee.result_type, f"tessy_{name}")
        out.extend(self.deriver.leaves(ret_member, Direction.IN))
        for idx, param in enumerate(callee.get_arguments()):
            ptype = param.type
            if ptype.get_canonical().kind == TypeKind.POINTER:
                ptype = ptype.get_canonical().get_pointee()   # captured by value (D7)
            member = self.graph.describe(ptype, f"tessy_{name}__Arg{idx}")
            out.extend(self.deriver.leaves(member, Direction.OUT))
        return out
