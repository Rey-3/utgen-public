"""Type graph (structs/unions/enums) and leaf derivation — "who is who" (L2).

Built once per module from clang types. The DB (Task 9) serializes these same
StructDef/EnumDef records, so leaves can be re-derived later without clang.
"""
from __future__ import annotations

from clang.cindex import Cursor, CursorKind, TypeKind

from utgen.model import CType, Direction, EnumDef, MemberDef, StructDef, Variable

_SIGNED = {TypeKind.SCHAR, TypeKind.SHORT, TypeKind.INT,
           TypeKind.LONG, TypeKind.LONGLONG}
_UNSIGNED = {TypeKind.UCHAR, TypeKind.USHORT, TypeKind.UINT,
             TypeKind.ULONG, TypeKind.ULONGLONG, TypeKind.BOOL}
_PLAIN_CHAR = {TypeKind.CHAR_S, TypeKind.CHAR_U}
_INTS = _SIGNED | _UNSIGNED | _PLAIN_CHAR
_FUNC_KINDS = {TypeKind.FUNCTIONPROTO, TypeKind.FUNCTIONNOPROTO}


def clean_name(spelling: str) -> str:
    """Type name without const/volatile qualifiers and struct/union/enum tags (B19)."""
    out = spelling
    for word in ("const ", "volatile ", "struct ", "union ", "enum "):
        out = out.replace(word, "")
    return out.strip()


def dims_suffix(dims: tuple) -> str:
    """(4, 5) -> "[4][5]" — declared dims stay on their own path segment (L2)."""
    out = ""
    for d in dims:
        out += f"[{d}]"
    return out


class TypeGraph:
    """Registered struct/union/enum definitions, keyed by clean type name."""

    def __init__(self, char_signed: bool = False):
        self.char_signed = char_signed          # plain `char` signedness (K8)
        self.structs: dict[str, StructDef] = {}
        self.enums: dict[str, EnumDef] = {}
        self._by_usr: dict[str, str] = {}       # record decl usr -> graph name
        self._anon_count = 0

    # --- registration / classification from clang types ---

    def describe(self, ctype, name: str, bitfield_width: int | None = None) -> MemberDef:
        """Classify a clang type as a MemberDef, registering referenced definitions."""
        dims = []
        elem = ctype
        canon = elem.get_canonical()
        while canon.kind == TypeKind.CONSTANTARRAY:
            dims.append(canon.element_count)
            elem = canon.element_type
            canon = elem.get_canonical()
        const = ctype.is_const_qualified() or elem.is_const_qualified()

        if bitfield_width is not None:
            return MemberDef(name, clean_name(elem.spelling), "scalar",
                             bitfield_width, tuple(dims),
                             self._is_signed(canon), True, const)
        if canon.kind == TypeKind.POINTER:
            pointee = canon.get_pointee()
            pointee_canon = pointee.get_canonical()
            if pointee_canon.kind in _FUNC_KINDS:
                return MemberDef(name, clean_name(elem.spelling), "funcptr", 0, tuple(dims))
            if pointee_canon.kind in (TypeKind.RECORD, TypeKind.ENUM):
                return MemberDef(name, self.register(pointee), "pointer",
                                 pointee_canon.get_size() * 8, tuple(dims))
            bits = pointee_canon.get_size() * 8 if pointee_canon.kind in _INTS else 8
            return MemberDef(name, clean_name(pointee.spelling), "pointer",
                             bits, tuple(dims), self._is_signed(pointee_canon))
        if canon.kind == TypeKind.RECORD:
            decl = canon.get_declaration()
            kind = "union" if decl.kind == CursorKind.UNION_DECL else "struct"
            return MemberDef(name, self.register(elem), kind, 0, tuple(dims))
        if canon.kind == TypeKind.ENUM:
            return MemberDef(name, self.register(elem), "enum",
                             canon.get_size() * 8, tuple(dims))
        if canon.kind in _INTS:
            return MemberDef(name, clean_name(elem.spelling), "scalar",
                             canon.get_size() * 8, tuple(dims),
                             self._is_signed(canon), False, const)
        return MemberDef(name, clean_name(elem.spelling), "unsupported", 0, tuple(dims))

    def register(self, ctype) -> str:
        """Ensure ctype's record/enum definition is in the graph; return its name."""
        canon = ctype.get_canonical()
        if canon.kind == TypeKind.RECORD:
            return self._register_record(ctype, canon)
        if canon.kind == TypeKind.ENUM:
            return self._register_enum(ctype, canon)
        return clean_name(ctype.spelling)

    def _register_record(self, ctype, canon) -> str:
        decl = canon.get_declaration()
        usr = decl.get_usr()
        if usr in self._by_usr:
            return self._by_usr[usr]
        name = self._name_for(ctype, canon)
        self._by_usr[usr] = name
        kind = "union" if decl.kind == CursorKind.UNION_DECL else "struct"
        # Placeholder first so self-referential structs cannot recurse forever.
        self.structs[name] = StructDef(name, kind, (), _file_of(decl))
        members = []
        for field in canon.get_fields():
            width = field.get_bitfield_width() if field.is_bitfield() else None
            if field.is_bitfield() and not field.spelling:
                continue                              # unnamed padding bitfield
            # Anonymous members carry clang's "(anonymous at ...)" spelling —
            # normalize to "" so the deriver skips the path level (D8).
            field_name = field.spelling
            if "(anonymous" in field_name or "(unnamed" in field_name:
                field_name = ""
            members.append(self.describe(field.type, field_name, width))
        self.structs[name] = StructDef(name, kind, tuple(members), _file_of(decl))
        return name

    def _register_enum(self, ctype, canon) -> str:
        decl = canon.get_declaration()
        usr = decl.get_usr()
        if usr in self._by_usr:
            return self._by_usr[usr]
        name = self._name_for(ctype, canon)
        self._by_usr[usr] = name
        values = []
        for child in decl.get_children():
            if child.kind == CursorKind.ENUM_CONSTANT_DECL:
                values.append((child.spelling, child.enum_value))
        self.enums[name] = EnumDef(name, tuple(values), _file_of(decl))
        return name

    def _name_for(self, ctype, canon) -> str:
        name = clean_name(ctype.spelling)
        if "(anonymous" in name or "(unnamed" in name or not name:
            self._anon_count += 1
            return f"<anon{self._anon_count}>"
        return name

    def _is_signed(self, canon) -> bool:
        if canon.kind in _PLAIN_CHAR:
            return self.char_signed
        return canon.kind in _SIGNED


def _file_of(decl: Cursor) -> str:
    loc = decl.location.file
    return str(loc) if loc else ""


class LeafDeriver:
    """Derives XML leaf Variables from the graph: `lol.abc[4].ded[5]` (L2/D8/D11)."""

    def __init__(self, graph: TypeGraph):
        self.graph = graph

    def leaves(self, member: MemberDef, direction: Direction) -> list:
        out: list[Variable] = []
        self._emit(out, "", member, direction, fmt=None, pointer=False, visited=frozenset())
        return out

    def _emit(self, out, prefix, member, direction, fmt, pointer, visited):
        if member.kind in ("funcptr", "unsupported"):
            return                              # recorded in graph, never injectable (D11/K9)
        name = self._join(prefix, member.name) + dims_suffix(member.dims)
        pointer = pointer or member.kind == "pointer"
        if member.type_name in self.graph.structs and member.kind in ("struct", "union", "pointer"):
            if member.type_name in visited:
                return                          # self-referential guard
            struct = self.graph.structs[member.type_name]
            fmt = fmt or member.type_name + dims_suffix(member.dims)
            for child in struct.members:
                self._emit(out, name, child, direction, fmt,
                           pointer, visited | {member.type_name})
            return
        if member.type_name in self.graph.enums:
            # Covers both a direct enum member and a pointer-to-enum (D4): the
            # injectable domain is the enum's, never a raw scalar.
            enum = self.graph.enums.get(member.type_name)
            values = enum.values if enum else ()
            ctype = CType(member.type_name, member.len_bits, member.signed, values)
            xml_len = str(ctype.enum_max) if values else str(member.len_bits)
            out.append(Variable(name, ctype, fmt or member.type_name,
                                xml_len, direction, pointer))
            return
        bits = member.len_bits or 8
        ctype = CType(member.type_name, bits, member.signed)
        sign_word = "signed" if member.signed else "unsigned"
        out.append(Variable(name, ctype, fmt or sign_word, str(bits), direction, pointer))

    def _join(self, prefix: str, name: str) -> str:
        if not name:
            return prefix                       # anonymous level skipped in path (D8)
        if not prefix:
            return name
        return f"{prefix}.{name}"
