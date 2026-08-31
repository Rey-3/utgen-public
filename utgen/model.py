"""
UTGen Core Data Models and Intermediate Representations

Defines the shared data structures used throughout the UTGen pipeline.

This module contains:
- C type and variable representations
- Test inventory and stub metadata
- Boolean decision and MC/DC representations
- Solver constraints and generated test rows
- Expression and statement intermediate representations (IR)
- Symbolic dataflow states and results
- Function-level and module-level analysis models

These models act as the common data exchange layer between parsing,
type analysis, decision extraction, IR construction, dataflow analysis,
test generation, interpretation, and XML emission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from itertools import product
import re


_ARRAY_EXTENT = re.compile(r"\[(\d+)\]")


def expand_array_names(name: str) -> list[str]:
    """Expand declared array extents in left-to-right product order."""
    sizes = _ARRAY_EXTENT.findall(name)
    if not sizes:
        return []
    template = _ARRAY_EXTENT.sub("[{}]", name)
    names = []
    for indices in product(*(range(int(size)) for size in sizes)):
        names.append(template.format(*indices))
    return names


class Direction(StrEnum):
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
    TRUTHY = "truthy"  # boolean condition
    BITSET = "bitset"  # All specified mask bits must be set. 
    BITANY = "bitany"  # At least one specified mask bit must be set.


@dataclass(frozen=True)
class CType:
    """ C primitive / enum type """
    name: str # C type name
    bits: int # Number of bits
    signed: bool # unsigned or signed 
    enum_values: tuple = () # enum name and their numeric values 

    @property
    def lo(self) -> int:
        # minimum  value of c type
        return -(1 << (self.bits - 1)) if self.signed else 0

    @property
    def hi(self) -> int:
        # maximum value of c type
        return (1 << (self.bits - 1)) - 1 if self.signed else (1 << self.bits) - 1

    @property
    def enum_max(self) -> int | None:
        # maximum value of the enum
        return max(v for _, v in self.enum_values) if self.enum_values else None


@dataclass(frozen=True)
class Variable:
    """stores the metadata about a C variable"""

    """
    1. variable name 
    
    It contain symbol 
    - struct member: "." 
    - array: "[]"
    - function: "()"
    - Tessy local static variable format: :: #1
    - address: @

    2. Ctype
    - line 51

    3. fmt
    - value_*.xml format label

    4. xml_len
    - value_*.xml len label

    5. direction
    - variable direction

    6. is_pointer
    - indicates that the variable data is accessed through a pointer

    7. address_of
    - stores the name of the pointer whose address/NULL state this variable control 
    """
    name: str 
    ctype: CType 
    fmt: str 
    xml_len: str
    direction: Direction
    is_pointer: bool = False
    address_of: str = ""


@dataclass(frozen=True)
class Atom:
    """Leaf condition of a decision; `var is None` means uncontrollable.
    
    1. var
    - comparison of the variables on the left side 
    - speed > 30; var = speed 
    - var can be none; if(is_valid())
    
    2. op
    - relational operations

    3. rhs
    - use when the right side is a constant
    - speed > 30; rhs = 30 

    4. rhs_var
    - use when the right side is not a constant
    - a > b:
        var = a
        rhs = None
        rhs_var = b

    5. text
    - preserve the original C expression
    """
    var: Variable | None
    op: Op
    rhs: int | None = None
    rhs_var: Variable | None = None
    text: str = ""

    @property
    def controllable(self) -> bool:
    # indicates whether this condition can be controlled through a known variable
        return self.var is not None


@dataclass(frozen=True)
class And:
    """
    Represents a logical AND expression containing multiple child conditions

    children:
        - Stores all child conditions connected by the AND operator

    Example:
    Input:
    - if (a > 10 && b == 1)

    Output:
    And(
        children=(
            Atom(var=a, op=Op.GT, rhs=10),
            Atom(var=b, op=Op.EQ, rhs=1)
        )
    )
    
    """
    children: tuple


@dataclass(frozen=True)
class Or:
    """
    Represents a logical OR expression containing multiple child conditions

    children:
        - Stores the child conditions connected by the OR operator

    Example:
    Input:
        if (a > 10 || b == 1)

    Output:
        Or(
            children=(
                Atom(var=a, op=Op.GT, rhs=10),
                Atom(var=b, op=Op.EQ, rhs=1)
            )
        )
    """
    children: tuple


@dataclass(frozen=True)
class Not:
    """
    Represents a logical NOT expression applied to one child condition

    child:
        - Stores the condition that is negated by the NOT operator

    Example:
    Input:
        if (!(a > 10))

    Output:
        Not(
            child=Atom(var=a, op=Op.GT, rhs=10)
        )
    """
    child: object

# Type alias for any supported logical expression node
Node = Atom | And | Or | Not


_ATOM_NODES = {}


@cache
def _cached_atoms(node_id: int) -> tuple:
    node = _ATOM_NODES[node_id]
    if isinstance(node, Atom):
        return node,
    if isinstance(node, Not):
        return _atoms_of(node.child)
    out = []
    for child in node.children:
        out.extend(_atoms_of(child))
    return tuple(out)


def _atoms_of(node: object) -> tuple:
    _ATOM_NODES[id(node)] = node
    return _cached_atoms(id(node))


def _clear_atoms() -> None:
    _ATOM_NODES.clear()
    _cached_atoms.cache_clear()


_atoms_of.cache_clear = _clear_atoms
_atoms_of.cache_info = _cached_atoms.cache_info


def atoms_of(node: object) -> list:
    """Flatten a boolean tree into its atoms, left-to-right, duplicates kept."""
    return list(_atoms_of(node))


@dataclass(frozen=True)
class Guard:
    """
    Represents a condition that must be satisfied before reaching a decision.

    expr:
        - Stores the previous condition expression.

    outcome:
        - Stores the required result of the condition, either True or False.

    Example:
    Input:
        if (a > 10)
        {
            if (b == 1)
            {
            }
        }

    Guard for (b == 1):
        Guard(
            expr=Atom(var=a, op=Op.GT, rhs=10),
            outcome=True
        )
    """
    expr: object
    outcome: bool


@dataclass
class Decision:
    """
    Represents a decision point in the C source code

    id:
        - Unique ID of the decision

    expr:
        - Stores the complete condition expression of the decision

    kind:
        - Stores the type of decision; "if", "loop" or "case"

    guards:
        - Stores the conditions that must be satisfied before reaching this decision

    location:
        - Stores the source code location of the decision

    Example:
    Input:
        if (a > 10 && b == 1)

    Output:
        Decision(
            id=1,
            expr=And(
                children=(
                    Atom(var=a, op=Op.GT, rhs=10),
                    Atom(var=b, op=Op.EQ, rhs=1)
                )
            ),
            kind="if",
            guards=(),
            location="example.c:10"
        )
    """
    id: int
    expr: object
    kind: str                       # "if" | "case" | "loop"
    guards: tuple = ()
    location: str = ""


@dataclass
class StubInfo:
    """
    Stores information about an external function that needs to be stubbed

    name:
        - Stores the name of the stub function

    ret_ctype:
        - Stores the primitive, enum, or pointer return C type
        - None if the function returns a struct/union or the return type is unknown

    ret_struct:
        - Stores the struct/union name when the function returns a struct/union
        - None for normal return types

    params:
        - Stores the function parameters as (type, name) pairs

    Example:
    Input:
        int GetSpeed(int channel);

    Output:
        StubInfo(
            name="GetSpeed",
            ret_ctype=CType(name="int", bits=32, signed=True),
            ret_struct=None,
            params=[
                ("int", "channel")
            ]
        )
    """
    name: str
    ret_ctype: CType | None
    ret_struct: str | None
    params: list


@dataclass
class Inventory:
    """
    Stores the variables and stub functions related to a function

    variables:
        - Stores all test-related Variable objects for the function

    stubs:
        - Stores all StubInfo objects required by the function

    inputs():
        - Returns variables with IN or INOUT direction

    outputs():
        - Returns variables with OUT or INOUT direction

    find(name):
        - Finds and returns a Variable by its name
        - Returns None if the variable cannot be found

    Example:
        Inventory(
            variables=[
                Variable(name="a", direction=Direction.IN, ...),
                Variable(name="b", direction=Direction.OUT, ...)
            ],
            stubs=[
                StubInfo(name="GetSpeed", ...)
            ]
        )
    """
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
    """
    Represents one generated test case

    values:
        - Stores the input values used by the test case

    kind:
        - Stores the type of test case:
            "MC/DC"
            "Boundary"
            "Coverage" 
            "Fault Injection"

    description:
        - Stores a human-readable description of the purpose of the test case

    intent:
        - Stores the expected decision outcomes for the test case
        - Format: {decision_id: outcome}

    expected:
        - Stores the expected output values after executing the function

    Example:
        TestRow(
            values={
                "a": 11
            },
            kind="MC/DC",
            description="decision 1 = True",
            intent={
                1: True
            },
            expected={
                "result": 1
            }
        )
    """
    values: dict
    kind: str                      
    description: str
    intent: dict = field(default_factory=dict)     
    expected: dict = field(default_factory=dict)   
    # Iteration provenance is optional so existing generator positional
    # constructors continue to mean exactly the same thing.
    meta: object = None
    # Structured generator objectives are kept separately from the legacy integer
    # ``intent`` map.  The field is appended after ``meta`` so every existing
    # positional constructor remains source-compatible.  Iteration state may later
    # copy this tuple into RowMeta once a persistent row identity has been allocated.
    obligation_keys: tuple[str, ...] = ()


@dataclass
class Constraint:
    """
    Represents a restriction applied to a Variable for the Solver

    var:
        - Stores the Variable being constrained

    op:
        - Stores the comparison operator of the constraint

    value:
        - Stores the value used in the comparison

    prefer:
        - Stores the preferred value for the Solver to choose
        - It is not a required value
        - None means no preferred value

    Example:
    Input:
        if (a > 10)

    Constraint:
        Constraint(
            var=a,
            op=Op.GT,
            value=10,
            prefer=11
        )

    Meaning:
        a must be greater than 10
        If possible, prefer a = 11
    """
    var: Variable
    op: Op
    value: int
    prefer: int | None = None


@dataclass(frozen=True)
class MemberDef:
    """
    Stores type information about a C member

    name:
        - Stores the member name

    type_name:
        - Stores the C type name of the member

    kind:
        - Stores the category of the member, such as
          "scalar", "pointer", "struct", "union", or "enum"

    len_bits:
        - Stores the size of the member in bits

    dims:
        - Stores the array dimensions.
        - Empty tuple means the member is not an array

    signed:
        - Indicates whether the member uses a signed numeric type

    bitfield:
        - Indicates whether the member is a C bit-field

    const:
        - Indicates whether the member is const-qualified
    """
    name: str
    type_name: str
    kind: str                       
    len_bits: int = 0
    dims: tuple = ()
    signed: bool = False
    bitfield: bool = False
    const: bool = False


@dataclass(frozen=True)
class StructDef:
    """
    Stores the definition of a C struct or union

    name:
        - Stores the name of the struct or union

    kind:
        - Stores whether the definition is a "struct" or "union"

    members:
        - Stores all members of the struct or union as MemberDef objects

    file:
        - Stores the source/header file where the struct or union is defined

    Example:
    Input:
        typedef struct
        {
            int speed;
            uint8_t status;
        } Data;

    Output:
        StructDef(
            name="Data",
            kind="struct",
            members=(
                MemberDef(name="speed", ...),
                MemberDef(name="status", ...)
            ),
            file="data.h"
        )
    """
    name: str
    kind: str                       
    members: tuple
    file: str = ""


@dataclass(frozen=True)
class EnumDef:
    """
    Stores the definition of a C enum

    name:
        - Stores the name of the enum

    values:
        - Stores all enum members as (name, value) pairs

    file:
        - Stores the source/header file where the enum is defined

    Example:
    Input:
        typedef enum
        {
            OFF = 0,
            ON = 1,
            ERROR = 2
        } Status;

    Output:
        EnumDef(
            name="Status",
            values=(
                ("OFF", 0),
                ("ON", 1),
                ("ERROR", 2)
            ),
            file="status.h"
        )
    """
    name: str
    values: tuple
    file: str = ""


@dataclass(frozen=True)
class MacroDef:
    """
    Stores the definition of a C macro

    name:
        - Stores the name of the macro

    kind:
        - Stores the macro type
        - "object" for object-like macros
        - "function" for function-like macros

    raw:
        - Stores the original replacement text of the macro

    value:
        - Stores the evaluated integer value if the object-like macro can be resolved to an integer
        - None if no integer value is available

    params:
        - Stores the parameter list for a function-like macro
        - Empty string for an object-like macro

    file:
        - Stores the source/header file where the macro is defined
    """
    name: str
    kind: str                      
    raw: str
    value: int | None = None
    params: str = ""
    file: str = ""


@dataclass(frozen=True)
class GlobalDef:
    """
    Stores the definition of a C global variable

    name:
        - Stores the name of the global variable

    type_name:
        - Stores the C type name of the global variable

    dims:
        - Stores the array dimensions
        - Empty tuple means the variable is not an array

    extern:
        - Indicates whether the global variable is declared with extern

    static:
        - Indicates whether the global variable is declared with static

    const:
        - Indicates whether the global variable is const-qualified

    volatile:
        - Indicates whether the global variable is volatile-qualified

    file:
        - Stores the source/header file where the global variable is defined
    """
    name: str
    type_name: str
    dims: tuple = ()
    extern: bool = False
    static: bool = False
    const: bool = False
    volatile: bool = False
    file: str = ""


@dataclass(frozen=True)
class Ref:
    """
    Represents a reference to a variable in the intermediate representation (IR)

    path:
        - Stores the name of the referenced variable
        - For example, the C variable `speed` is represented as path="speed"
        - More complex accesses such as struct members and arrays are combined
          with other IR nodes such as MemberE and Index

    scope:
        - Stores an internal scope identifier used to distinguish different
          local variables that have the same variable name

        - In C, a variable declared inside an inner block can have the same
          name as a variable declared in an outer block. Although both
          variables have the same name, they are different variables with
          different lifetimes and storage

        - UTGen assigns a scope identifier such as "scope1" or "scope2"
          when it needs to distinguish these same-named local variables

        - The scope is combined with the variable path internally
          For example:

              Ref(path="value", scope="scope1")
                  -> internal path: "value@scope1"

              Ref(path="value", scope="scope2")
                  -> internal path: "value@scope2"

        - This prevents the dataflow analysis from treating two different
          local variables with the same name as the same variable

        - If the variable name is unique and no additional distinction is
          required, scope is an empty string ""

        - The scope identifier is generated internally by UTGen and does
          not exist in the original C source code

    Example:
        Input:

            void foo(void)
            {
                int value = 10;

                {
                    int value = 20;
                }
            }

        Conceptual representation:

            Ref(
                path="value",
                scope="scope1"
            )

            Ref(
                path="value",
                scope="scope2"
            )

        The two references both use the name "value", but their different
        scope identifiers allow UTGen to recognize them as two separate
        local variables
    """

    path: str
    scope: str = ""
    path: str
    scope: str = ""


@dataclass(frozen=True)
class Const:
    """
    Represents a constant value in the IR.

    value:
        - Stores the actual constant value.
        - Can be an integer or string representation.

    type_name:
        - Stores the C type name of the constant.
        - Empty string means no specific type information is stored.

    Example:
    Input:
        int a = 10;

    Output:
        Const(
            value=10,
            type_name="int"
        )
    """
    value: int | str
    type_name: str = ""


@dataclass(frozen=True)
class Unary:
    """
    Represents a unary expression in the IR

    op:
        - Stores the unary operator, such as "-", "!", "*", "&", "++", "--", or "~"

    x:
        - Stores the expression that the operator is applied to

    Example:
        -a

        Unary(
            op="-",
            x=Ref("a")
        )
    """
    op: str
    x: object


@dataclass(frozen=True)
class Binop:
    """
    Represents a binary expression in the IR

    op:
        - Stores the binary operator, such as "+", "-", "*", ">", or "==".

    l:
        - Stores the left operand expression

    r:
        - Stores the right operand expression

    Example:
        a + b

        Binop(
            op="+",
            l=Ref("a"),
            r=Ref("b")
        )
    """
    op: str
    l: object
    r: object


@dataclass(frozen=True)
class CallE:
    """
    Represents a function call expression in the IR

    name:
        - Stores the name of the called function

    args:
        - Stores the argument expressions passed to the function

    Example:
        Add(a, 10)

        CallE(
            name="Add",
            args=(Ref("a"), Const(10))
        )
    """
    name: str
    args: tuple = ()


@dataclass(frozen=True)
class Ternary:
    """
    Represents a ternary conditional expression in the IR

    c:
        - Stores the condition expression

    t:
        - Stores the expression used when the condition is True

    f:
        - Stores the expression used when the condition is False

    decision_id:
        - Stores the Decision ID associated with the condition

    Example:
        a > 10 ? 1 : 0

        Ternary(
            c=Binop(">", Ref("a"), Const(10)),
            t=Const(1),
            f=Const(0),
            decision_id=1
        )
    """
    c: object
    t: object
    f: object
    decision_id: int = 0


@dataclass(frozen=True)
class Cast:
    """
    Represents a type cast expression in the IR.

    type_name:
        - Stores the target C type to cast the expression to.

    x:
        - Stores the expression being cast.

    Example:
        (int)value

        Cast(
            type_name="int",
            x=Ref("value")
        )
    """
    type_name: str
    x: object


@dataclass(frozen=True)
class Index:
    """
    Represents an array or pointer index expression in the IR

    base:
        - Stores the array or pointer expression being accessed

    idx:
        - Stores the index expression used to access the element

    Example:
        arr[3]

        Index(
            base=Ref("arr"),
            idx=Const(3)
        )
    """
    base: object
    idx: object


@dataclass(frozen=True)
class MemberE:
    """
    Represents a struct or union member access expression in the IR

    base:
        - Stores the object or expression that contains the member

    name:
        - Stores the name of the member being accessed

    Example:
        data.speed

        MemberE(
            base=Ref("data"),
            name="speed"
        )
    """
    base: object
    name: str


@dataclass(frozen=True)
class Phi:
    """
    Represents a value selected from two possible branches

    condition:
        - Stores the condition used to select the value

    when_true:
        - Stores the value when the condition is True

    when_false:
        - Stores the value when the condition is False

    Example:
        if (a > 10)
            x = 1;
        else
            x = 2;

        Phi(
            condition=a > 10,
            when_true=1,
            when_false=2
        )
    """
    condition: object
    when_true: object
    when_false: object


@dataclass(frozen=True)
class PathConstraint:
    """
    Represents a branch condition required to follow an execution path

    decision_id:
        - Stores the ID of the associated decision

    expr:
        - Stores the condition expression of the branch

    outcome:
        - Stores the required result of the condition, True or False

    Example:
        if (a > 10)

        PathConstraint(
            decision_id=1,
            expr=Binop(">", Ref("a"), Const(10)),
            outcome=True
        )
    """
    decision_id: int | None
    expr: object
    outcome: bool


@dataclass
class DataflowState:
    """
    Represents the current state of one execution path during dataflow analysis

    values:
        - Stores the current symbolic values of variables

    path:
        - Stores the PathConstraints followed to reach this state

    reachable:
        - Indicates whether the state is reachable

    unknown:
        - Stores variables whose values are unknown

    condition:
        - Stores the current branch condition being evaluated

    aliases:
        - Stores pointer alias relationships

   Example:
        int x = 1;

        if (a > 10)
        {
            x = 5;
        }

        DataflowState(
            values={
                "a": Ref("a"),
                "x": Const(5)
            },
            path=(
                PathConstraint(
                    decision_id=1,
                    expr=Binop(">", Ref("a"), Const(10)),
                    outcome=True
                ),
            ),
            reachable=True,
            unknown=(),
            condition=None,
            aliases={}
        )
    """
    values: dict
    path: tuple = ()
    reachable: bool = True
    unknown: tuple = ()
    condition: object = None
    aliases: dict = field(default_factory=dict)


@dataclass
class DataflowPoint:
    """
    Represents a point in the program during dataflow analysis

    decision_id:
        - Stores the ID of the decision associated with this point

    states:
        - Stores the possible DataflowStates that reach this point

    location:
        - Stores the source code location of this point

    Example:
        if (a > 10)
        {
            x = 1;
        }

        DataflowPoint(
            decision_id=1,
            states=(
                DataflowState(
                    values={
                        "a": Ref("a"),
                        "x": Const(0)
                    },
                    path=(),
                    condition=Binop(">", Ref("a"), Const(10))
                ),
            ),
            location="main.c:10"
        )
    """
    decision_id: int | None
    states: tuple
    location: str = ""


@dataclass
class DataflowResult:
    """
    Represents the final result of dataflow analysis for a function

    points:
        - Stores the DataflowPoints found during analysis

    returns:
        - Stores the possible return values together with their states

    writes:
        - Stores variable writes detected during analysis

    Example:
        int foo(int a)
        {
            int x = 0;

            if (a > 10)
                x = 5;

            return x;
        }

        DataflowResult(
            points=[
                DataflowPoint(
                    decision_id=1,
                    states=(...)
                )
            ],
            returns=[
                (state1, Const(5)),
                (state2, Const(0))
            ],
            writes=[
                ("x", Const(0)),
                ("x", Const(5))
            ]
        )
    """
    points: list
    returns: list
    writes: list

    def point(self, decision_id: int) -> DataflowPoint:
        """ Returns the DataflowPoint associated with the given decision ID"""
        for point in self.points:
            if point.decision_id == decision_id:
                return point
        raise KeyError(decision_id)


@dataclass(frozen=True)
class Decl:
    """
    Represents a variable declaration in the IR

    name:
        - Stores the declared variable name

    init:
        - Stores the initializer expression

    scope:
        - Stores the scope ID used to distinguish same-named local variables

    is_pointer:
        - Indicates whether the declared variable is a pointer

    static_name:
        - Stores the unique internal name for a static local variable

    Example:
        int x = 10;

        Decl(
            name="x",
            init=Const(10),
            scope="",
            is_pointer=False,
            static_name=""
        )
    """
    name: str
    init: object = None
    scope: str = ""
    is_pointer: bool = False
    static_name: str = ""


@dataclass(frozen=True)
class Assign:
    """
    Represents an assignment statement in the IR

    lhs:
        - Stores the target being assigned to

    value:
        - Stores the value or expression being assigned

    op:
        - Stores the assignment operator, such as "=", "+=", or "-="

    decision_id:
        - Stores the Decision ID associated with the assigned logical expression

    Example:
        x = 10;

        Assign(
            lhs=Ref("x"),
            value=Const(10),
            op="=",
            decision_id=0
        )
    """
    lhs: object
    value: object
    op: str = "="
    decision_id: int = 0


@dataclass(frozen=True)
class If:
    """
    Represents an if/else statement in the IR

    decision_id:
        - Stores the ID of the decision associated with the condition

    then:
        - Stores the statements executed when the condition is True

    els:
        - Stores the statements executed when the condition is False

    condition:
        - Stores the condition expression of the if statement

    Example:
        if (a > 10) 
        {
            x = 1;
        }
        else
        {
            x = 2;
        }

        If(
            decision_id=1,
            condition=Binop(">", Ref("a"), Const(10)),
            then=(
                Assign(Ref("x"), Const(1)),
            ),
            els=(
                Assign(Ref("x"), Const(2)),
            )
        )
    """
    decision_id: int
    then: tuple = ()
    els: tuple = ()
    condition: object = None

    # def __post_init__(self):
    #     if not isinstance(self.decision_id, int) and isinstance(self.condition, int):
    #         condition, decision_id = self.decision_id, self.condition
    #         object.__setattr__(self, "decision_id", decision_id)
    #         object.__setattr__(self, "condition", condition)


@dataclass(frozen=True)
class SwitchCase:
    """
    Represents a case or default branch of a switch statement in the IR

    decision_id:
        - Stores the Decision ID associated with this case

    body:
        - Stores the statements executed when this case is selected

    labels:
        - Stores the case values associated with this branch

    default:
        - Indicates whether this branch is the default case

    Example:
        case 1:
            x = 10;
            break;

        SwitchCase(
            decision_id=1,
            body=(
                Assign(Ref("x"), Const(10)),
                Break()
            ),
            labels=(1,),
            default=False
        )
    """
    decision_id: int
    body: tuple = ()
    labels: tuple = ()
    default: bool = False


@dataclass(frozen=True)
class Switch:
    """
    Represents a complete switch statement in the IR

    subject:
        - Stores the expression evaluated by the switch statement

    cases:
        - Stores all SwitchCase branches contained in the switch

    Example:
        switch (mode)
        {
            case 1:
                x = 10;
                break;

            default:
                x = 0;
                break;
        }

        Switch(
            subject=Ref("mode"),
            cases=(
                SwitchCase(
                    decision_id=1,
                    body=(
                        Assign(Ref("x"), Const(10)),
                        Break()
                    ),
                    labels=(1,),
                    default=False
                ),
                SwitchCase(
                    decision_id=2,
                    body=(
                        Assign(Ref("x"), Const(0)),
                        Break()
                    ),
                    labels=(),
                    default=True
                )
            )
        )
    """
    subject: object
    cases: tuple = ()


@dataclass(frozen=True)
class While:
    """
    Represents a while loop in the IR

    cond:
        - Stores the boolean condition used for static analysis
          and decision reasoning

    body:
        - Stores the statements executed while the condition is True

    runtime_cond:
        - Stores the executable expression form of the loop condition
        - It is used by the interpreter to evaluate the condition at runtime
        - If it is not available, `cond` is used instead

    Example:
        while (a < 10)
        {
            a += 1;
        }

        While(
            cond=Atom(...),
            body=(
                Assign(
                    lhs=Ref("a"),
                    value=Const(1),
                    op="+="
                ),
            ),
            runtime_cond=Binop(
                "<",
                Ref("a"),
                Const(10)
            )
        )
    """
    cond: object
    body: tuple = ()
    runtime_cond: object | None = field(default=None, repr=False)


@dataclass(frozen=True)
class For:
    """
    Represents a for loop in the IR

    init:
        - Stores the initialization statements executed before the loop starts

    cond:
        - Stores the boolean condition used for static analysis

    increment:
        - Stores the statements executed after each loop iteration

    body:
        - Stores the statements executed while the condition is True

    runtime_cond:
        - Stores the executable expression form of the loop condition
        - It is used by the interpreter to evaluate the condition at runtime

    Example:
        for (int i = 0; i < 10; i++)
        {
            x += i;
        }

        For(
            init=(
                Decl(
                    name="i",
                    init=Const(0)
                ),
            ),
            cond=Atom(...),
            increment=(
                ExprStmt(
                    Unary("++", Ref("i"))
                ),
            ),
            body=(
                Assign(
                    lhs=Ref("x"),
                    value=Ref("i"),
                    op="+="
                ),
            ),
            runtime_cond=Binop(
                "<",
                Ref("i"),
                Const(10)
            )
        )
    """
    init: tuple = ()
    cond: object = None
    increment: tuple = ()
    body: tuple = ()
    runtime_cond: object | None = field(default=None, repr=False)


@dataclass(frozen=True)
class DoWhile:
    """
    Represents a do-while loop in the IR

    cond:
        - Stores the boolean condition used for static analysis

    body:
        - Stores the statements executed by the loop
        - The body is executed at least once before the condition is checked

    runtime_cond:
        - Stores the executable expression form of the loop condition
        - It is used by the interpreter to evaluate the condition at runtime

    Example:
        do
        {
            a += 1;
        }
        while (a < 10);

        DoWhile(
            cond=Atom(...),
            body=(
                Assign(
                    lhs=Ref("a"),
                    value=Const(1),
                    op="+="
                ),
            ),
            runtime_cond=Binop(
                "<",
                Ref("a"),
                Const(10)
            )
        )
    """
    cond: object
    body: tuple = ()
    runtime_cond: object | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ExprStmt:
    """
    Represents an expression used as a standalone statement in the IR

    expr:
        - Stores the expression that is executed as a statement
        - The expression may be evaluated for its side effects rather than
          for storing its resulting value

    Example:
        foo(a);

        ExprStmt(
            expr=CallE(
                name="foo",
                args=(Ref("a"),)
            )
        )
    """
    expr: object


@dataclass(frozen=True)
class Return:
    """
    Represents a return statement in the IR

    value:
        - Stores the expression returned by the function
        - It is None when the return statement has no value

    Example:
        return x;

        Return(
            value=Ref("x")
        )
    """
    value: object = None


@dataclass(frozen=True)
class Break:
    """
    Represents a break statement in the IR.

    It contains no fields because a break statement does not have
    a value or condition. It signals that the current loop or
    switch flow should stop

    Example:
        break;

        Break()
    """


@dataclass(frozen=True)
class Unknown:
    """
    Represents a statement that cannot be accurately modeled by the IR

    text:
        - Stores the original source code text of the unsupported
          or unrecognized statement.

    Example:
        some_unsupported_statement;

        Unknown(
            text="some_unsupported_statement;"
        )
    """
    text: str


@dataclass
class FunctionIR:
    """
    Represents the complete intermediate representation of a C function

    name:
        - Stores the function name

    line:
        - Stores the source line where the function is defined

    return_type:
        - Stores the C return type of the function

    static:
        - Indicates whether the function is declared as static

    params:
        - Stores the function parameters as type-name pairs

    stubs:
        - Stores external functions that require stub information

    leaves:
        - Stores the testable leaf variables associated with the function

    decisions:
        - Stores the decisions found in the function for branch and
          condition analysis

    body:
        - Stores the IR statements that make up the function body

    dataflow:
        - Stores the DataflowResult generated from the function body

    constants:
        - Stores known constant values such as macros and enum values

    local_functions:
        - Stores the functions available within the same module for
          resolving local function calls

    Example:
        int AddOne(int a)
        {
            int x = a + 1;

            if (x > 10)
                x = 10;

            return x;
        }

        FunctionIR(
            name="AddOne",
            line=1,
            return_type="int",
            static=False,
            params=[
                ("int", "a")
            ],
            stubs=[],
            leaves=[...],
            decisions=[
                Decision(
                    id=1,
                    expr=...,
                    kind="if"
                )
            ],
            body=(
                Decl(
                    name="x",
                    init=Binop("+", Ref("a"), Const(1))
                ),
                If(
                    decision_id=1,
                    then=(
                        Assign(Ref("x"), Const(10)),
                    ),
                    condition=Binop(">", Ref("x"), Const(10))
                ),
                Return(
                    value=Ref("x")
                )
            ),
            dataflow=...,
            constants={...},
            local_functions={...}
        )
    """
    name: str
    line: int
    return_type: str
    static: bool
    params: list
    stubs: list
    leaves: list                    
    decisions: list                 
    body: tuple = ()                
    dataflow: DataflowResult | None = None
    constants: dict = field(default_factory=dict)
    local_functions: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass
class ModuleAnalysis:
    """
    Represents the complete analysis result of a C source module

    file:
        - Stores the path of the analyzed source file

    sha256:
        - Stores the SHA-256 hash of the source file

    include_dirs:
        - Stores the include directories used when parsing the source file

    headers:
        - Stores the header files associated with the module

    macros:
        - Stores the MacroDef objects found in the module

    enums:
        - Stores the EnumDef objects found in the module

    structs:
        - Stores the StructDef objects found in the module

    variables:
        - Stores the GlobalDef objects representing global variables

    functions:
        - Stores the FunctionIR objects representing functions in the module

    Example:
        #define MAX 100

        int g_count;

        int Add(int a)
        {
            return a + 1;
        }

        ModuleAnalysis(
            file="example.c",
            sha256="...",
            include_dirs=[...],
            headers=[...],
            macros=[
                MacroDef(
                    name="MAX",
                    ...
                )
            ],
            enums=[],
            structs=[],
            variables=[
                GlobalDef(
                    name="g_count",
                    type_name="int"
                )
            ],
            functions=[
                FunctionIR(
                    name="Add",
                    ...
                )
            ]
        )
    """
    file: str
    sha256: str
    include_dirs: list
    headers: list                 
    macros: list
    enums: list
    structs: list
    variables: list
    functions: list

    # Search for the object in the corresponding list based on the name. If found, return the result; otherwise, return None
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
