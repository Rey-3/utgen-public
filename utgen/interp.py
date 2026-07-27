"""Concrete execution of UTGen's clang-free statement IR."""
from __future__ import annotations

from dataclasses import dataclass, field

from utgen.model import (And, Assign, Atom, Binop, Break, CallE, Cast, Const,
                         Decl, DoWhile, ExprStmt, For, If, Index, Inventory,
                         MemberE, Not, Or, Ref, Return, Switch, Ternary,
                         TestRow, Unary, While)


_BREAK = object()
_UNKNOWN = object()


@dataclass
class ExecResult:
    """Observed decision outcomes, outputs, and unknown output names."""
    outcomes: dict[int, bool] = field(default_factory=dict)
    outputs: dict[str, int] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)


class Interpreter:
    """Execute a function IR using injectable inventory values."""

    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory

    def run(self, function, values: dict) -> ExecResult:
        """Execute ``function`` and return its observable result."""
        self.env = {}
        self.aliases = {}
        self.outcomes = {}
        self.unknown = set()
        self.result = ExecResult()
        for variable in self.inventory.inputs():
            value = values.get(variable.name, 0)
            self.env[variable.name] = 0 if value == "NULL" else value
        signal = self._block(function.body)
        if signal and signal[0] == "return":
            output = f"{function.name}()"
            if signal[1] is _UNKNOWN:
                self.unknown.add(output)
            else:
                self.result.outputs[output] = self._wrap(output, signal[1])
        for variable in self.inventory.outputs():
            if variable.name in self.unknown:
                self.unknown.add(variable.name)
            elif variable.name in self.env:
                self.result.outputs[variable.name] = self._wrap(variable.name, self.env[variable.name])
        self.result.outcomes = self.outcomes
        self.result.unknown = self.unknown
        return self.result

    def _block(self, statements):
        for statement in statements:
            signal = self._stmt(statement)
            if signal is not None:
                return signal
        return None

    def _stmt(self, statement):
        if isinstance(statement, Decl):
            if isinstance(statement.init, CallE) and self._struct_stub(statement.init.name):
                self.aliases[statement.name] = f"tessy_{statement.init.name}"
            else:
                self.env[statement.name] = self._eval(statement.init)
        elif isinstance(statement, Assign):
            path, value = self._path(statement.lhs), self._eval(statement.value)
            if value is _UNKNOWN:
                self.unknown.add(path)
            else:
                value = self._assign(statement.op, self.env.get(path, 0), value)
                self.env[path] = self._wrap(path, value)
        elif isinstance(statement, If):
            outcome = bool(self._eval(statement.condition))
            self.outcomes[statement.decision_id] = outcome
            return self._block(statement.then if outcome else statement.els)
        elif isinstance(statement, Switch):
            return self._switch(statement)
        elif isinstance(statement, DoWhile):
            signal = self._block(statement.body)
            return None if signal is _BREAK else signal
        elif isinstance(statement, While):
            if self._eval(statement.cond):
                self.unknown.update(self._writes(statement.body))
        elif isinstance(statement, For):
            self._block(statement.init)
            if self._eval(statement.cond):
                self.unknown.update(self._writes(statement.body))
        elif isinstance(statement, Break):
            return _BREAK
        elif isinstance(statement, ExprStmt):
            self._eval(statement.expr)
        elif isinstance(statement, Return):
            return "return", self._eval(statement.value)
        return None

    def _eval(self, expression):
        if expression is None:
            return 0
        if isinstance(expression, Const):
            return expression.value
        if isinstance(expression, Ref):
            path = self._path(expression)
            return _UNKNOWN if path in self.unknown else self.env.get(path, 0)
        if isinstance(expression, MemberE):
            path = self._path(expression)
            return _UNKNOWN if path in self.unknown else self.env.get(path, 0)
        if isinstance(expression, CallE):
            return self.env.get(f"tessy_{expression.name}", 0)
        if isinstance(expression, Ternary):
            condition = self._eval(expression.c)
            return _UNKNOWN if condition is _UNKNOWN else self._eval(expression.t if condition else expression.f)
        if isinstance(expression, Cast):
            return self._eval(expression.x)
        if isinstance(expression, Atom):
            if expression.var is None:
                return 0
            left = self.env.get(expression.var.name, 0)
            right = self.env.get(expression.rhs_var.name, 0) if expression.rhs_var else expression.rhs
            return self._atom(expression.op, left, right)
        if isinstance(expression, And):
            for child in expression.children:
                if not self._eval(child):
                    return 0
            return 1
        if isinstance(expression, Or):
            for child in expression.children:
                if self._eval(child):
                    return 1
            return 0
        if isinstance(expression, Not):
            return int(not self._eval(expression.child))
        if isinstance(expression, Unary):
            value = self._eval(expression.x)
            if value is _UNKNOWN:
                return _UNKNOWN
            if expression.op == "!":
                return int(not value)
            if expression.op == "-":
                return -value
            if expression.op == "~":
                return ~value
            return value
        if isinstance(expression, Binop):
            left = self._eval(expression.l)
            if left is _UNKNOWN:
                return _UNKNOWN
            if expression.op == "&&":
                return 0 if not left else self._truth(self._eval(expression.r))
            if expression.op == "||":
                return 1 if left else self._truth(self._eval(expression.r))
            right = self._eval(expression.r)
            if right is _UNKNOWN or expression.op in ("/", "%") and not right:
                return _UNKNOWN
            return self._binop(expression.op, left, right)
        return 0

    def _path(self, expression):
        if isinstance(expression, Ref):
            return self.aliases.get(expression.path, expression.path)
        if isinstance(expression, MemberE):
            return f"{self._path(expression.base)}.{expression.name}"
        if isinstance(expression, Index):
            return f"{self._path(expression.base)}[{self._eval(expression.idx)}]"
        if isinstance(expression, Unary) and expression.op == "*":
            return self._path(expression.x)
        return ""

    def _switch(self, statement):
        subject, selected, default = self._eval(statement.subject), None, None
        for index, case in enumerate(statement.cases):
            matched = subject in case.labels
            if case.default:
                default = index
            self.outcomes[case.decision_id] = matched
            if matched and selected is None:
                selected = index
        selected = default if selected is None else selected
        if selected is None:
            return None
        if statement.cases[selected].default:
            self.outcomes[statement.cases[selected].decision_id] = True
        for case in statement.cases[selected:]:
            signal = self._block(case.body)
            if signal is _BREAK:
                return None
            if signal is not None:
                return signal
        return None

    @staticmethod
    def _writes(statements):
        names = set()
        for statement in statements:
            if isinstance(statement, Assign):
                if isinstance(statement.lhs, Ref):
                    names.add(statement.lhs.path)
            elif hasattr(statement, "body"):
                names.update(Interpreter._writes(statement.body))
        return names

    def _struct_stub(self, name):
        for stub in self.inventory.stubs:
            if stub.name == name:
                return stub.ret_struct is not None
        return False

    def _wrap(self, path, value):
        variable = self.inventory.find(path)
        if variable is None or not isinstance(value, int):
            return value
        value &= (1 << variable.ctype.bits) - 1
        return value - (1 << variable.ctype.bits) if variable.ctype.signed and value >= 1 << (variable.ctype.bits - 1) else value

    @staticmethod
    def _truth(value):
        return _UNKNOWN if value is _UNKNOWN else int(bool(value))

    @staticmethod
    def _binop(op, left, right):
        operations = {"+": lambda: left + right, "-": lambda: left - right, "*": lambda: left * right,
                      "/": lambda: Interpreter._divide(left, right), "%": lambda: Interpreter._mod(left, right),
                      "&": lambda: left & right, "|": lambda: left | right, "^": lambda: left ^ right,
                      "<<": lambda: left << right, ">>": lambda: left >> right, "==": lambda: int(left == right),
                      "!=": lambda: int(left != right), ">": lambda: int(left > right),
                      ">=": lambda: int(left >= right), "<": lambda: int(left < right), "<=": lambda: int(left <= right)}
        return operations[op]() if op in operations else 0

    @staticmethod
    def _assign(op, old, value):
        return value if op == "=" else Interpreter._binop(op[:-1], old, value)

    @staticmethod
    def _atom(op, left, right):
        operations = {"truthy": lambda: int(bool(left)), "bitset": lambda: int(bool(left & right)),
                      "==": lambda: int(left == right), "!=": lambda: int(left != right),
                      ">": lambda: int(left > right), ">=": lambda: int(left >= right),
                      "<": lambda: int(left < right), "<=": lambda: int(left <= right)}
        return operations[op]() if op in operations else 0

    @staticmethod
    def _divide(left, right):
        return int(left / right) if right else 0

    @staticmethod
    def _mod(left, right):
        return left - Interpreter._divide(left, right) * right if right else 0

class Verifier:
    """Verify row intents and attach concrete expected outputs."""

    def __init__(self, interpreter: Interpreter) -> None:
        self.interpreter = interpreter

    def check(self, function, rows: list[TestRow]) -> list[str]:
        """Fill expected outputs and return intent-mismatch notes."""
        notes = []
        for row in rows:
            result = self.interpreter.run(function, row.values)
            for decision_id, wanted in row.intent.items():
                if result.outcomes.get(decision_id) != wanted:
                    notes.append(f"decision {decision_id}: intent mismatch")
                    break
            else:
                row.expected.update(result.outputs)
        return notes
