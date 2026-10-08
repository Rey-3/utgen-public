
from __future__ import annotations

from clang.cindex import Cursor, CursorKind, TypeKind

from utgen.model import CType, Direction, EnumDef, MemberDef, StructDef, Variable

# Group Clang integer kinds here so type parsing uses one consistent rule for
# width/sign handling instead of repeating TypeKind checks throughout the graph.
_SIGNED = {TypeKind.SCHAR, TypeKind.SHORT, TypeKind.INT,
           TypeKind.LONG, TypeKind.LONGLONG}
_UNSIGNED = {TypeKind.UCHAR, TypeKind.USHORT, TypeKind.UINT,
             TypeKind.ULONG, TypeKind.ULONGLONG, TypeKind.BOOL}
_PLAIN_CHAR = {TypeKind.CHAR_S, TypeKind.CHAR_U}
_INTS = _SIGNED | _UNSIGNED | _PLAIN_CHAR
_FUNC_KINDS = {TypeKind.FUNCTIONPROTO, TypeKind.FUNCTIONNOPROTO}


def clean_name(spelling: str) -> str:
    """Normalize a Clang type spelling into the type name stored by UTGen."""

    # Qualifiers and C tag keywords belong to the source spelling, but keeping
    # them here would make the same logical type appear under different names.
    out = spelling
    for word in ("const ", "volatile ", "struct ", "union ", "enum "):
        out = out.replace(word, "")
    return out.strip()


def dims_suffix(dims: tuple) -> str:
    """Convert stored array extents back to the C-like suffix used in leaf names."""

    out = ""
    for d in dims:
        out += f"[{d}]"
    return out


class TypeGraph:
    """Translate Clang types into reusable UTGen struct, enum, and member metadata."""


    def __init__(self, char_signed: bool = False):
        self.char_signed = char_signed
        self.structs: dict[str, StructDef] = {}
        self.enums: dict[str, EnumDef] = {}
        # Clang USRs identify declarations independently of how their type was
        # spelled at each use site. The map also prevents recursive records from
        # being registered forever while their members are being traversed.
        self._by_usr: dict[str, str] = {}
        self._anon_count = 0



    def describe(self, ctype, name: str, bitfield_width: int | None = None) -> MemberDef:
        """Describe one Clang type without yet flattening structs/unions into leaves."""

        # Peel constant arrays first so the base element can be classified once,
        # while preserving every extent for later names such as item[2][4].
        dims = []
        elem = ctype
        canon = elem.get_canonical()
        while canon.kind == TypeKind.CONSTANTARRAY:
            dims.append(canon.element_count)
            elem = canon.element_type
            canon = elem.get_canonical()
        const = ctype.is_const_qualified() or elem.is_const_qualified()
        # Keep typedef names for analysis; resolve integer chains for Excel storage.
        native_type = clean_name(canon.spelling) if canon.kind in (_SIGNED | _UNSIGNED) - {TypeKind.BOOL} else ""

        # A bit-field's usable width is the declared bit width, not the size of
        # its underlying integer storage type. Leaf bounds must therefore use it.
        if bitfield_width is not None:
            return MemberDef(name, clean_name(elem.spelling), "scalar",
                             bitfield_width, tuple(dims),
                             self._is_signed(canon), True, const, native_type)
        if canon.kind == TypeKind.POINTER:
            pointee = canon.get_pointee()
            pointee_canon = pointee.get_canonical()
            # Function pointers are metadata only for this graph; dereferencing
            # them cannot produce controllable scalar test-data leaves.
            if pointee_canon.kind in _FUNC_KINDS:
                return MemberDef(name, clean_name(elem.spelling), "funcptr", 0, tuple(dims))
            # Register record/enum pointees now so LeafDeriver can follow their
            # type_name into the graph and expand the pointed-to data structure.
            if pointee_canon.kind in (TypeKind.RECORD, TypeKind.ENUM):
                return MemberDef(name, self.register(pointee), "pointer",
                                 pointee_canon.get_size() * 8, tuple(dims))
            # For scalar pointers UTGen models the pointed-to value, so the width
            # comes from the pointee type rather than the machine pointer width.
            bits = pointee_canon.get_size() * 8 if pointee_canon.kind in _INTS else 8
            return MemberDef(name, clean_name(pointee.spelling), "pointer",
                             bits, tuple(dims), self._is_signed(pointee_canon))
        if canon.kind == TypeKind.RECORD:
            decl = canon.get_declaration()
            kind = "union" if decl.kind == CursorKind.UNION_DECL else "struct"
            # Store only the registered type reference here. Member expansion is
            # intentionally deferred to LeafDeriver to keep parsing and flattening separate.
            return MemberDef(name, self.register(elem), kind, 0, tuple(dims))
        if canon.kind == TypeKind.ENUM:
            return MemberDef(name, self.register(elem), "enum",
                             canon.get_size() * 8, tuple(dims))
        if canon.kind in _INTS:
            return MemberDef(name, clean_name(elem.spelling), "scalar",
                             canon.get_size() * 8, tuple(dims),
                             self._is_signed(canon), False, const, native_type)
        # Unsupported types remain visible in the graph instead of being guessed
        # as integers; LeafDeriver will deliberately skip them later.
        return MemberDef(name, clean_name(elem.spelling), "unsupported", 0, tuple(dims))

    def register(self, ctype) -> str:
        """Register reusable record/enum definitions and return their graph name."""

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

        # Install a placeholder before visiting fields. A self-referential type
        # such as `struct Node { struct Node *next; }` can then resolve back to
        # this record instead of recursively registering the same declaration.
        self.structs[name] = StructDef(name, kind, (), _file_of(decl))
        members = []
        for field in canon.get_fields():
            width = field.get_bitfield_width() if field.is_bitfield() else None
            # Unnamed bit-fields reserve layout bits only; there is no variable
            # name that UTGen can expose as test input/output.
            if field.is_bitfield() and not field.spelling:
                continue


            field_name = field.spelling
            # Clang can expose anonymous struct/union fields using generated text.
            # Emptying the name lets LeafDeriver flatten their children directly
            # under the parent instead of creating a fake `.anonymous` path.
            if "(anonymous" in field_name or "(unnamed" in field_name:
                field_name = ""
            members.append(self.describe(field.type, field_name, width))
        self.structs[name] = StructDef(name, kind, tuple(members), _file_of(decl))
        return name

    def _register_enum(self, ctype, canon) -> str:
        """Capture enum names and numeric values once for later rendering/solving."""

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
        """Return a stable graph-local name, including synthetic names for anonymous types."""

        name = clean_name(ctype.spelling)
        if "(anonymous" in name or "(unnamed" in name or not name:
            self._anon_count += 1
            return f"<anon{self._anon_count}>"
        return name

    def _is_signed(self, canon) -> bool:
        """Resolve integer signedness, including configurable plain-char behavior."""

        # Whether plain `char` is signed is implementation-defined in C, so it
        # comes from UTGen configuration instead of being hard-coded here.
        if canon.kind in _PLAIN_CHAR:
            return self.char_signed
        return canon.kind in _SIGNED


def _file_of(decl: Cursor) -> str:
    loc = decl.location.file
    return str(loc) if loc else ""


class LeafDeriver:
    """Flatten graph members into scalar Variables consumed by test generation/XML output."""


    def __init__(self, graph: TypeGraph):
        self.graph = graph

    def leaves(self, member: MemberDef, direction: Direction) -> list:
        """Expand one parameter/global/return member into its addressable leaf variables."""

        out: list[Variable] = []
        self._emit(out, "", member, direction, fmt=None, pointer=False, visited=frozenset())
        return out

    def _emit(self, out, prefix, member, direction, fmt, pointer, visited):
        # These kinds cannot be converted into concrete scalar values for UTGen.
        if member.kind in ("funcptr", "unsupported"):
            return
        name = self._join(prefix, member.name) + dims_suffix(member.dims)
        # Once traversal passes through a pointer, every resulting child leaf is
        # marked as pointer-derived even when the child itself is a scalar field.
        pointer = pointer or member.kind == "pointer"
        if member.type_name in self.graph.structs and member.kind in ("struct", "union", "pointer"):
            # `visited` is path-local: it stops cycles such as Node->Node while
            # still allowing the same struct type to be expanded in separate branches.
            if member.type_name in visited:
                return
            struct = self.graph.structs[member.type_name]
            # Preserve the outer composite type as XML/TESSY format metadata for
            # all leaves produced from that struct, including its array dimensions.
            fmt = fmt or member.type_name + dims_suffix(member.dims)
            for child in struct.members:
                self._emit(out, name, child, direction, fmt,
                           pointer, visited | {member.type_name})
            return
        if member.type_name in self.graph.enums:


            enum = self.graph.enums.get(member.type_name)
            values = enum.values if enum else ()
            ctype = CType(member.type_name, member.len_bits, member.signed, values)
            # TESSY enum length metadata uses the maximum enumerator value, while
            # ordinary scalar leaves use their bit width below.
            xml_len = str(ctype.enum_max) if values else str(member.len_bits)
            out.append(Variable(name, ctype, fmt or member.type_name,
                                xml_len, direction, pointer))
            return
        # Some fallback scalar/pointer descriptions can arrive without a usable
        # width; 8 bits keeps the leaf representable instead of creating CType(0).
        bits = member.len_bits or 8
        ctype = CType(member.type_name, bits, member.signed)
        # Standalone scalars use signedness as their format. If this leaf came
        # from a composite, `fmt` keeps the owning struct/union format instead.
        sign_word = member.native_type or ("signed" if member.signed else "unsigned")
        out.append(Variable(name, ctype, fmt or sign_word, str(bits), direction, pointer))

    def _join(self, prefix: str, name: str) -> str:
        """Build dotted member paths while allowing anonymous fields to stay flattened."""

        if not name:
            return prefix
        if not prefix:
            return name
        return f"{prefix}.{name}"
