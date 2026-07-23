"""Core data model for UTGen.

Every pipeline stage communicates through these types; nothing here depends on
clang. Spec: docs/superpowers/specs/2026-07-21-utgen-design.md §4.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Direction(StrEnum):
    """XML `direction` attribute of a leaf variable."""
    IN = "in"
    OUT = "out"
    INOUT = "inout"


class Op(StrEnum):
    """Comparison/test operator of a condition atom."""
    EQ = "=="
    NE = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    TRUTHY = "truthy"
    BITSET = "bitset"


@dataclass(frozen=True)
class CType:
    """A resolved C scalar/enum type with its numeric range."""
    name: str
    bits: int
    signed: bool
    enum_values: tuple = ()

    @property
    def lo(self) -> int:
        return -(1 << (self.bits - 1)) if self.signed else 0

    @property
    def hi(self) -> int:
        return (1 << (self.bits - 1)) - 1 if self.signed else (1 << self.bits) - 1

    @property
    def enum_max(self) -> int | None:
        return max(v for _, v in self.enum_values) if self.enum_values else None


@dataclass(frozen=True)
class Variable:
    """One injectable/observable XML <argument> leaf (e.g. `test.isvalid`)."""
    name: str
    ctype: CType
    fmt: str
    xml_len: str
    direction: Direction
    is_pointer: bool = False


@dataclass(frozen=True)
class Atom:
    """Leaf condition of a decision; `var is None` means uncontrollable."""
    var: Variable | None
    op: Op
    rhs: int | None = None
    rhs_var: Variable | None = None
    text: str = ""

    @property
    def controllable(self) -> bool:
        return self.var is not None


@dataclass(frozen=True)
class And:
    """Short-circuit `&&` over child nodes."""
    children: tuple


@dataclass(frozen=True)
class Or:
    """Short-circuit `||` over child nodes."""
    children: tuple


@dataclass(frozen=True)
class Not:
    """Logical `!` of one child node."""
    child: object


Node = Atom | And | Or | Not


def atoms_of(node: object) -> list:
    """Flatten a boolean tree into its atoms, left-to-right, duplicates kept."""
    if isinstance(node, Atom):
        return [node]
    if isinstance(node, Not):
        return atoms_of(node.child)
    out = []
    for child in node.children:
        out.extend(atoms_of(child))
    return out


@dataclass(frozen=True)
class Guard:
    """An outer decision outcome required to reach a nested point (FILO stack entry)."""
    expr: object
    outcome: bool


@dataclass
class Decision:
    """A full boolean controlling expression (`if` condition or `case` membership)."""
    id: int
    expr: object
    kind: str                       # "if" | "case" | "loop"
    guards: tuple
    location: str


@dataclass
class StubInfo:
    """A called function replaced by a stub in the test harness."""
    name: str
    ret_ctype: CType | None
    ret_struct: str | None
    params: list


@dataclass
class Inventory:
    """All leaf variables and stubs of one function."""
    variables: list
    stubs: list

    def inputs(self) -> list:
        out = []
        for v in self.variables:
            if v.direction in (Direction.IN, Direction.INOUT):
                out.append(v)
        return out

    def outputs(self) -> list:
        out = []
        for v in self.variables:
            if v.direction in (Direction.OUT, Direction.INOUT):
                out.append(v)
        return out

    def find(self, name: str) -> Variable | None:
        for v in self.variables:
            if v.name == name:
                return v
        return None


@dataclass
class TestRow:
    """One generated test case: a value per in-variable plus bookkeeping."""
    values: dict
    kind: str                       # "MC/DC" | "Switch" | "Boundary" | "Fault Injection" | "Nominal"
    description: str
    intent: dict = field(default_factory=dict)      # decision id -> required outcome
    expected: dict = field(default_factory=dict)    # out-var name -> expected value


@dataclass
class Constraint:
    """`var OP value` requirement handed to the solver."""
    var: Variable
    op: Op
    value: int
    prefer: int | None = None


# --- type graph / module database entities (spec §8) ---

@dataclass(frozen=True)
class MemberDef:
    """One member inside a struct/union definition."""
    name: str
    type_name: str
    kind: str                       # scalar|struct|union|pointer|funcptr|enum
    len_bits: int = 0
    dims: tuple = ()
    signed: bool = False
    bitfield: bool = False
    const: bool = False


@dataclass(frozen=True)
class StructDef:
    """A struct or union definition; anonymous levels are flattened by the builder."""
    name: str
    kind: str                       # struct|union
    members: tuple
    file: str = ""


@dataclass(frozen=True)
class EnumDef:
    """An enum definition with its ordered (name, value) pairs."""
    name: str
    values: tuple
    file: str = ""


@dataclass(frozen=True)
class MacroDef:
    """A recorded macro; `value` is the folded int of object-like constants."""
    name: str
    kind: str                       # object|function
    raw: str
    value: int | None = None
    params: str = ""
    file: str = ""


@dataclass(frozen=True)
class GlobalDef:
    """A file-scope variable (incl. extern from headers) with its qualifiers."""
    name: str
    type_name: str
    dims: tuple = ()
    extern: bool = False
    static: bool = False
    const: bool = False
    volatile: bool = False
    file: str = ""


# --- expression IR (serialized inside <Body>, executed by the interpreter) ---

@dataclass(frozen=True)
class Ref:
    """Read of a leaf/local by dotted path, e.g. `test.isvalid`."""
    path: str
    scope: str = ""


@dataclass(frozen=True)
class Const:
    """Integer (or enum-name) literal."""
    value: int | str


@dataclass(frozen=True)
class Unary:
    """Unary operator: `!`, `-`, `~`, `*` (deref), `&` (address-of)."""
    op: str
    x: object


@dataclass(frozen=True)
class Binop:
    """Binary operator over two expressions (arith/bit/compare/logical)."""
    op: str
    l: object
    r: object


@dataclass(frozen=True)
class CallE:
    """Call of a stubbed function inside an expression."""
    name: str
    args: tuple = ()


@dataclass(frozen=True)
class Ternary:
    """`c ? t : f` conditional expression."""
    c: object
    t: object
    f: object


@dataclass(frozen=True)
class Cast:
    """`(type)x` cast; wrap-around applied by the interpreter."""
    type_name: str
    x: object


@dataclass(frozen=True)
class Index:
    """`base[idx]` array access."""
    base: object
    idx: object


@dataclass(frozen=True)
class MemberE:
    """`base.name` / `base->name` member access."""
    base: object
    name: str


@dataclass(frozen=True)
class Phi:
    """Value selected by a symbolic branch condition at a join."""
    condition: object
    when_true: object
    when_false: object


@dataclass(frozen=True)
class PathConstraint:
    """One decision outcome required by a path."""
    decision_id: int | None
    expr: object
    outcome: bool


@dataclass
class DataflowState:
    """Symbolic values and path facts at one program point."""
    values: dict
    path: tuple = ()
    reachable: bool = True
    unknown: tuple = ()
    condition: object = None
    aliases: dict = field(default_factory=dict)


@dataclass
class DataflowPoint:
    """States reaching one decision or observable program point."""
    decision_id: int | None
    states: tuple
    location: str = ""


@dataclass
class DataflowResult:
    """Path-sensitive symbolic analysis for one function."""
    points: list
    returns: list
    writes: list

    def point(self, decision_id: int) -> DataflowPoint:
        """Return the first recorded point for a decision id."""
        for point in self.points:
            if point.decision_id == decision_id:
                return point
        raise KeyError(decision_id)


# --- statement IR ---

@dataclass(frozen=True)
class Decl:
    """Local variable declaration with optional initializer."""
    name: str
    init: object = None
    scope: str = ""
    is_pointer: bool = False


@dataclass(frozen=True)
class Assign:
    """Assignment; `op` is "=" or a compound form like "+="."""
    lhs: object
    value: object
    op: str = "="


@dataclass(frozen=True)
class If:
    """Branch linked to its Decision by id."""
    decision_id: int
    then: tuple = ()
    els: tuple = ()
    condition: object = None


@dataclass(frozen=True)
class SwitchCase:
    """One case (or default) arm, linked to its case Decision by id."""
    decision_id: int
    body: tuple = ()
    labels: tuple = ()
    default: bool = False


@dataclass(frozen=True)
class Switch:
    """Switch over `subject`; fallthrough handled by the interpreter."""
    subject: object
    cases: tuple = ()


@dataclass(frozen=True)
class While:
    """while/for loop: guard-only, condition kept as a boolean Node (A14)."""
    cond: object
    body: tuple = ()


@dataclass(frozen=True)
class DoWhile:
    """do-while loop: body executes at least once, condition unguards it (A15)."""
    cond: object
    body: tuple = ()


@dataclass(frozen=True)
class ExprStmt:
    """Expression used as a statement (e.g. a void call)."""
    expr: object


@dataclass(frozen=True)
class Return:
    """Function return with optional value expression."""
    value: object = None


@dataclass(frozen=True)
class Break:
    """`break` out of the enclosing switch/loop."""


@dataclass(frozen=True)
class Unknown:
    """Unrepresentable construct; poisons touched outputs, never crashes (K5/L8)."""
    text: str


@dataclass
class FunctionIR:
    """Everything generation needs about one function — clang-free."""
    name: str
    line: int
    return_type: str
    static: bool
    params: list
    stubs: list
    leaves: list                    # Variable items
    decisions: list                 # Decision items
    body: tuple = ()                # Stmt items
    dataflow: DataflowResult | None = None


@dataclass
class ModuleAnalysis:
    """Complete analysis of one target .c — the content of db_<module>.xml."""
    file: str
    sha256: str
    include_dirs: list
    headers: list                   # (name, path, sha256) tuples
    macros: list
    enums: list
    structs: list
    variables: list
    functions: list

    def macro(self, name: str) -> MacroDef | None:
        for m in self.macros:
            if m.name == name:
                return m
        return None

    def variable(self, name: str) -> GlobalDef | None:
        for v in self.variables:
            if v.name == name:
                return v
        return None

    def function(self, name: str) -> FunctionIR | None:
        for f in self.functions:
            if f.name == name:
                return f
        return None
