from __future__ import annotations

import os

from clang.cindex import Cursor, CursorKind, TypeKind

from utgen.model import (Assign, Binop, CallE, Const, CType, Decl, Direction,
                         ExprStmt, If, Ref, StubInfo, Variable)
from utgen.typegraph import LeafDeriver, TypeGraph, clean_name

# Common library calls are intentionally left as real calls instead of being
# exposed as controllable TESSY stubs.
DEFAULT_NO_STUB = ("memcpy", "memset", "memcmp", "abs")


def prepare_snapshot_callbacks(function):
    """Expose existing event callbacks at a stub between a snapshot and its check."""
    # ponytail: exact bit-snapshot/public-setter patterns; other event models need explicit hooks.
    stubs = {stub.name: stub for stub in function.stubs}
    inputs = {leaf.name for leaf in function.leaves}

    def visit(body):
        snapshots, hook, order = {}, None, -1
        for index, statement in enumerate(body):
            if isinstance(statement, Decl) and isinstance(statement.init, Ref):
                snapshots[statement.name] = (statement.init.path, index)
            elif (isinstance(statement, Assign) and statement.op == "="
                  and isinstance(statement.lhs, Ref) and isinstance(statement.value, Ref)):
                snapshots[statement.lhs.path] = (statement.value.path, index)
            if (isinstance(statement, ExprStmt) and isinstance(statement.expr, CallE)
                    and statement.expr.name in stubs):
                hook, order = stubs[statement.expr.name], index
            if not isinstance(statement, If):
                continue
            condition = statement.condition
            if isinstance(condition, Binop) and condition.op == "==" and hook is not None:
                left, right = condition.l, condition.r
                if (isinstance(left, Binop) and left.op == "&" and isinstance(left.l, Ref)
                        and isinstance(right, Binop) and right.op == "&" and isinstance(right.l, Ref)
                        and isinstance(left.r, Const) and left.r == right.r):
                    for snapshot, current in ((left.l.path, right.l.path), (right.l.path, left.l.path)):
                        source, copied_at = snapshots.get(snapshot, (None, index))
                        if source != current or order <= copied_at:
                            continue
                        callbacks = [callback for callback in function.local_functions.values()
                                     if not callback.static and not callback.params and callback.return_type == "void"
                                     and any(isinstance(write, Assign) and write.op == "|="
                                             and write.lhs == Ref(current) and write.value == left.r
                                             for write in callback.body)]
                        if len(callbacks) != 1:
                            continue
                        callback = callbacks[0]
                        control = f"tessy_{hook.name}__{callback.name}"
                        hook.callbacks[control] = callback.name
                        if control not in inputs:
                            inputs.add(control)
                            function.leaves.append(Variable(control, CType("unsigned char", 1, False),
                                                            "unsigned char", "1", Direction.IN))
            visit(statement.then)
            visit(statement.els)

    visit(function.body)


class StubFinder:
    """Find external function calls that must be represented as TESSY stubs.

    Besides collecting :class:`StubInfo`, this class derives the synthetic
    ``tessy_*`` variables used to control a stub return value and observe the
    arguments passed to that stub during a test.
    """

    def __init__(self, graph: TypeGraph, deriver: LeafDeriver,
                 no_stub=DEFAULT_NO_STUB):
        self.graph = graph
        self.deriver = deriver
        self.no_stub = set(no_stub)

    def find(self, func_cursor: Cursor) -> tuple[list, list]:
        """Return unique external stubs and the TESSY variables they require."""

        stubs: list[StubInfo] = []
        variables: list[Variable] = []

        # A function may be called many times in the AST, but TESSY only needs
        # one stub definition for that function name.
        seen: set[str] = set()
        source_file = self._file_of(func_cursor)
        self._walk(func_cursor, func_cursor.spelling, source_file, seen,
                   stubs, variables)
        return stubs, variables

    def _walk(self, cur: Cursor, self_name: str, source_file: str,
              seen: set, stubs: list, variables: list) -> None:
        """Walk the complete Clang AST so nested calls are discovered too."""

        if cur.kind == CursorKind.CALL_EXPR:
            self._record(cur, self_name, source_file, seen, stubs, variables)
        for child in cur.get_children():
            self._walk(child, self_name, source_file, seen, stubs, variables)

    def _record(self, cur: Cursor, self_name: str, source_file: str,
                seen: set, stubs: list, variables: list) -> None:
        callee = cur.referenced

        # ``referenced`` gives the declaration when Clang can resolve the call.
        # Falling back to the call spelling still lets UTGen record an unresolved
        # external call, although type information will then be unavailable.
        name = callee.spelling if callee is not None else cur.spelling

        # Do not stub recursion, explicitly excluded library calls, duplicate
        # calls, or functions implemented in the same source file. Local
        # definitions belong to the unit under analysis and can be interpreted
        # directly instead of being replaced by a TESSY stub.
        if (not name or name == self_name or name in self.no_stub or name in seen
                or self._is_local_definition(callee, source_file)):
            return

        seen.add(name)
        stubs.append(self._stub_info(name, callee))

        # Synthetic TESSY variables need the resolved declaration because their
        # shapes are derived from the actual return and parameter types.
        if callee is not None:
            variables.extend(self._tessy_leaves(name, callee))

    def _is_local_definition(self, callee: Cursor | None, source_file: str) -> bool:
        """Check whether the called function is defined in the source under test."""

        if callee is None or not source_file:
            return False
        definition = callee.get_definition()
        return definition is not None and self._file_of(definition) == source_file

    def _file_of(self, cursor: Cursor) -> str:
        """Return a normalized path suitable for reliable source-file comparison."""

        location = cursor.location.file
        if location is None:
            return ""

        # Clang paths can differ only by slash style or case on Windows.
        # Normalizing both avoids treating the same file as an external module.
        return os.path.normcase(os.path.normpath(str(location)))

    def _stub_info(self, name: str, callee: Cursor | None) -> StubInfo:
        """Build the signature metadata later written to the stub-analysis XML."""

        if callee is None:
            # Keep the call visible even when Clang cannot resolve its declaration.
            return StubInfo(name, None, None, [])

        ret_ctype, ret_struct = self._return_of(callee)
        params = []
        for param in callee.get_arguments():
            params.append((self._stub_type_name(param.type), param.spelling))
        return StubInfo(name, ret_ctype, ret_struct, params)

    def _return_of(self, callee: Cursor) -> tuple[CType | None, str | None]:
        """Convert a Clang return type into UTGen's scalar or aggregate form."""

        member = self.graph.describe(callee.result_type, "")

        # Struct/union returns are kept separately because CType represents the
        # scalar-like information needed for ranges (bit width, sign, enums).
        if member.kind in ("struct", "union"):
            return None, member.type_name

        if member.kind in ("scalar", "enum", "pointer"):
            enum = self.graph.enums.get(member.type_name)
            values = enum.values if enum else ()
            name = member.type_name

            # TypeGraph describes a pointer mainly through its pointee. For the
            # stub signature, preserve the actual pointer spelling (for example
            # ``uint8_t *``) instead of losing the pointer part of the type.
            if member.kind == "pointer":
                name = clean_name(callee.result_type.spelling)

            return CType(name, member.len_bits, member.signed, values), None

        # ``void`` and unsupported return categories do not need scalar/struct
        # return metadata.
        return None, None

    def _stub_type_name(self, ctype) -> str:
        """Produce the parameter type spelling used in the generated stub XML."""

        member = self.graph.describe(ctype, "")
        if member.kind == "scalar":
            # Remove qualifiers/keywords that TypeGraph does not need while
            # keeping typedef-style scalar names stable for the XML signature.
            return clean_name(ctype.spelling)

        # Pointer and aggregate declarations need their original Clang spelling
        # so information such as ``*`` and the complete declared type is retained.
        return ctype.spelling

    def _tessy_leaves(self, name: str, callee: Cursor) -> list:
        """Derive synthetic variables used to control and observe a TESSY stub."""

        # In the current UTGen representation a void stub has no controllable
        # return value, so no tessy_* value leaves are generated for it.
        if callee.result_type.get_canonical().kind == TypeKind.VOID:
            return []

        out: list[Variable] = []

        # A stub return value is test data supplied *to* the function under test,
        # therefore it is an IN variable from UTGen/TESSY's point of view.
        ret_member = self.graph.describe(callee.result_type, f"tessy_{name}")
        out.extend(self.deriver.leaves(ret_member, Direction.IN))

        for idx, param in enumerate(callee.get_arguments()):
            ptype = param.type

            # For pointer parameters TESSY is interested in the value passed
            # through the pointer, not only the raw pointer address. Derive leaves
            # from the pointee type so structures/arrays can be expanded normally.
            if ptype.get_canonical().kind == TypeKind.POINTER:
                ptype = ptype.get_canonical().get_pointee()

            # Arguments received by the stub are observations produced by the
            # function under test, so they are represented as OUT variables.
            member = self.graph.describe(ptype, f"tessy_{name}__Arg{idx}")
            out.extend(self.deriver.leaves(member, Direction.OUT))

        return out
