"""Concrete execution of UTGen's clang-free statement IR."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, is_dataclass

from utgen.model import (And, Assign, Atom, Binop, Break, CallE, Cast, Const,
                          Decl, DoWhile, ExprStmt, For, If, Index, Inventory,
                          MemberE, Not, Or, Ref, Return, Switch, Ternary,
                          TestRow, Unary, While, atoms_of)


_BREAK = object()
_UNKNOWN = object()
_INDEX_PATH = re.compile(r"\[\d+\]")
_LEGACY_ATOM = re.compile(r"\s*(\w+)\s*(<=|>=|==|!=|<|>)\s*(?:\([^)]*\)\s*)?(\w+|\d+)\s*")
_ARRAY_INDEX = re.compile(r"\[(\d+)\]")


@dataclass
class ExecResult:
    """Observed decision outcomes, outputs, and unknown output names."""
    outcomes: dict[int, bool] = field(default_factory=dict)
    observed: dict[int, set[bool]] = field(default_factory=dict)
    conditions: dict[int, set[tuple[bool | None, ...]]] = field(default_factory=dict)
    loops: dict[int, set[bool]] = field(default_factory=dict)
    outputs: dict[str, int] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)


class Interpreter:
    """Execute a function IR using injectable inventory values."""

    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory
        self._variable_names, self._normalized_names, self._address_names = set(), {}, {}
        self._path_cache = {}
        for variable in inventory.variables:
            self._variable_names.add(variable.name)
            self._normalized_names[variable.name] = _INDEX_PATH.sub("[*]", variable.name)
            if variable.address_of:
                self._address_names.setdefault(variable.address_of, []).append(variable.name)

    def run(self, function, values: dict) -> ExecResult:
        """Execute ``function`` and return its observable result."""
        self.env = {}
        self.aliases = {}
        self.pointers = set()
        self.constants = getattr(function, "constants", {}) or {}
        self.local_functions = function.local_functions
        self._local_stack = []
        self.decision_expr = {decision.id: decision.expr for decision in function.decisions}
        self.recording = True
        self.outcomes = {}
        self.observed = {}
        self.conditions = {}
        self.loops = {}
        self.unknown = set()
        self.result = ExecResult()
        for variable in self.inventory.inputs():
            value = values.get(variable.name, 1 if variable.address_of else 0)
            self.env[variable.name] = 0 if value == "NULL" else value
        for name, value in values.items():
            self.env[name] = 0 if value == "NULL" else value
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
        self.result.observed = self.observed
        self.result.conditions = self.conditions
        self.result.loops = self.loops
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
            if statement.static_name:
                self._bind_static(statement)
            elif statement.is_pointer:
                path = self._path(Ref(statement.name, statement.scope))
                self.pointers.add(path)
                if not self._set_alias(path, statement.init):
                    self.env[path] = self._eval(statement.init)
            elif isinstance(statement.init, CallE) and self._struct_stub(statement.init.name):
                self.aliases[statement.name] = f"tessy_{statement.init.name}"
            else:
                self.env[statement.name] = self._eval(statement.init)
        elif isinstance(statement, Assign):
            path = self._path(statement.lhs)
            if isinstance(statement.value, CallE) and self._struct_stub(statement.value.name):
                self.aliases[path] = f"tessy_{statement.value.name}"
                return None
            if path in self.pointers and self._set_alias(path, statement.value):
                return None
            if path in self.pointers:
                self.aliases.pop(path, None)
            if statement.decision_id:
                value, leaves = self._trace(statement.value)
                if value is not _UNKNOWN:
                    self._record(statement.decision_id, bool(value), leaves)
            else:
                value = self._eval(statement.value)
            if value is _UNKNOWN:
                self.unknown.add(path)
            else:
                value = self._assign(statement.op, self.env.get(path, 0), value)
                self.env[path] = self._wrap(path, value)
        elif isinstance(statement, If):
            value, leaves = self._trace(statement.condition)
            outcome = bool(value)
            self._record(statement.decision_id, outcome, leaves)
            return self._block(statement.then if outcome else statement.els)
        elif isinstance(statement, Switch):
            return self._switch(statement)
        elif isinstance(statement, DoWhile):
            return self._loop(statement.body, self._loop_condition(statement), True, loop_id=id(statement))
        elif isinstance(statement, While):
            return self._loop(statement.body, self._loop_condition(statement), False, loop_id=id(statement))
        elif isinstance(statement, For):
            self._block(statement.init)
            signal = self._loop(statement.body, self._loop_condition(statement), False, statement.increment, id(statement))
            if signal is not None:
                return signal
        elif isinstance(statement, Break):
            return _BREAK
        elif isinstance(statement, ExprStmt):
            self._eval(statement.expr)
        elif isinstance(statement, Return):
            return "return", self._eval(statement.value)
        return None

    def _set_alias(self, path, expression):
        if isinstance(expression, Unary) and expression.op == "&":
            target = self._path(expression.x)
            if target:
                self.aliases[path] = target
                return True
        if isinstance(expression, Ref) and expression.path in self.aliases:
            self.aliases[path] = self.aliases[expression.path]
            return True
        self.aliases.pop(path, None)
        return False

    @staticmethod
    def _loop_condition(statement):
        return statement.runtime_cond if statement.runtime_cond is not None else statement.cond

    def _bind_static(self, declaration):
        local = self._path(Ref(declaration.name, declaration.scope))
        for variable in self.inventory.inputs():
            name = variable.name
            if name == declaration.static_name or name.startswith((declaration.static_name + ".", declaration.static_name + "[")):
                path = self._normalized_path(local + name[len(declaration.static_name):])
                self.env[path] = self._read(name)

    def _eval(self, expression):
        if expression is None:
            return 0
        if isinstance(expression, Const):
            value = expression.value

            # Enum names and object-like macro names may be persisted as
            # strings in the XML database. Resolve them before evaluating
            # arithmetic or bitwise expressions.
            if isinstance(value, str) and value in self.constants:
                return self.constants[value]

            return value
        if isinstance(expression, Ref):
            path = self._path(expression)
            return self._read(path)
        if isinstance(expression, Index):
            return self._read(self._path(expression))
        if isinstance(expression, MemberE):
            path = self._path(expression)
            return self._read(path)
        if isinstance(expression, CallE):
            # remove for not stable 
            # helper = self.local_functions.get(expression.name)
            # if helper is not None and not helper.static:
            #     return self._call_local(expression, helper)
            return self.env.get(f"tessy_{expression.name}", 0)
        if isinstance(expression, Ternary):
            condition, leaves = self._trace(expression.c)
            if expression.decision_id:
                self._record(expression.decision_id, bool(condition), leaves)
            return _UNKNOWN if condition is _UNKNOWN else self._eval(expression.t if condition else expression.f)
        if isinstance(expression, Cast):
            return self._eval(expression.x)
        if isinstance(expression, Atom):
            if expression.var is None:
                return self._legacy_atom(expression)
            left = self._read(expression.var.name)
            right = self._read(expression.rhs_var.name) if expression.rhs_var else expression.rhs
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
            if expression.op == "*":
                return self._read(self._path(expression.x), False)
            value = self._eval(expression.x)
            if value is _UNKNOWN:
                return _UNKNOWN
            if (
                expression.op in ("++", "--", "-", "~")
                and not isinstance(value, int)
            ):
                return _UNKNOWN
            if expression.op in ("++", "--"):
                value += 1 if expression.op == "++" else -1
                self.env[self._path(expression.x)] = self._wrap(self._path(expression.x), value)
                return value
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
            if (
                expression.op
                in ("+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>", "<", "<=", ">", ">=")
                and (
                    not isinstance(left, int)
                    or not isinstance(right, int)
                )
            ):
                return _UNKNOWN
            return self._binop(expression.op, left, right)
        return 0

    def _trace(self, expression):
        if isinstance(expression, Binop) and expression.op in ("&&", "||"):
            left, left_leaves = self._trace(expression.l)
            if expression.op == "&&" and not left:
                return 0, left_leaves
            if expression.op == "||" and left:
                return 1, left_leaves
            right, right_leaves = self._trace(expression.r)
            return self._truth(right), left_leaves + right_leaves
        if isinstance(expression, And):
            leaves = ()
            for child in expression.children:
                value, child_leaves = self._trace(child)
                leaves += child_leaves
                if not value:
                    return 0, leaves
            return 1, leaves
        if isinstance(expression, Or):
            leaves = ()
            for child in expression.children:
                value, child_leaves = self._trace(child)
                leaves += child_leaves
                if value:
                    return 1, leaves
            return 0, leaves
        if isinstance(expression, Not):
            value, leaves = self._trace(expression.child)
            return int(not value), leaves
        value = self._eval(expression)
        if isinstance(expression, (Atom, Ref)) or (isinstance(expression, Binop) and expression.op in ("==", "!=", "<", "<=", ">", ">=")):
            return value, (bool(value),)
        return value, ()

    def _call_local(self, expression, helper):
        if helper.name in self._local_stack or len(expression.args) != len(helper.params):
            return _UNKNOWN
        actuals = []
        for (type_name, _), actual in zip(helper.params, expression.args):
            if "*" in type_name:
                target = self._path(actual.x) if isinstance(actual, Unary) and actual.op == "&" else self._path(actual)
                if not target:
                    return _UNKNOWN
                actuals.append(("pointer", target))
                continue
            source = self._path(actual) if isinstance(actual, (Ref, MemberE, Index)) else ""
            members = self._aggregate(source)
            if members:
                actuals.append(("aggregate", members))
                continue
            value = self._eval(actual)
            if value is _UNKNOWN:
                return _UNKNOWN
            actuals.append(("scalar", value))
        roots = {name for _, name in helper.params} | self._decl_roots(helper.body)
        snapshot = self._save_frame(roots)
        recording = self.recording
        self._local_stack.append(helper.name)
        try:
            for (kind, value), (_, formal) in zip(actuals, helper.params):
                if kind == "pointer":
                    self.aliases[formal] = value
                    self.pointers.add(formal)
                elif kind == "aggregate":
                    for suffix, member in value.items():
                        self.env[formal + suffix] = member
                else:
                    self.env[formal] = value
            self.recording = False
            signal = self._block(helper.body)
            return _UNKNOWN if not signal or signal[0] != "return" else signal[1]
        finally:
            self.recording = recording
            self._local_stack.pop()
            self._restore_frame(snapshot)

    def _aggregate(self, source):
        if not source:
            return {}
        prefixes = source + ".", source + "["
        return {name[len(source):]: value for name, value in self.env.items()
                if name.startswith(prefixes)}

    @staticmethod
    def _decl_roots(statements):
        roots = set()

        def visit(value):
            if isinstance(value, Decl):
                roots.add(value.name)
            if is_dataclass(value):
                for item in fields(value):
                    visit(getattr(value, item.name))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    visit(item)

        visit(statements)
        return roots

    def _save_frame(self, roots):
        owned = lambda name: any(name == root or name.startswith((root + ".", root + "[")) for root in roots)
        return (roots, {name: value for name, value in self.env.items() if owned(name)},
                {name: value for name, value in self.aliases.items() if owned(name)},
                {name for name in self.pointers if owned(name)},
                {name for name in self.unknown if owned(name)})

    def _restore_frame(self, snapshot):
        roots, env, aliases, pointers, unknown = snapshot
        owned = lambda name: any(name == root or name.startswith((root + ".", root + "[")) for root in roots)
        for mapping in (self.env, self.aliases):
            for name in tuple(mapping):
                if owned(name):
                    mapping.pop(name)
        self.pointers.difference_update(name for name in tuple(self.pointers) if owned(name))
        self.unknown.difference_update(name for name in tuple(self.unknown) if owned(name))
        self.env.update(env)
        self.aliases.update(aliases)
        self.pointers.update(pointers)
        self.unknown.update(unknown)

    def _loop(self, body, condition, do_first=False, increment=(), loop_id=None):
        ran = False
        outcomes = self.loops.setdefault(loop_id, set())
        for _ in range(1024):
            if not do_first or ran:
                outcome = bool(self._eval(condition))
                outcomes.add(outcome)
                if not outcome:
                    return None
            ran = True
            signal = self._block(body)
            if signal is _BREAK:
                return None
            if signal is not None:
                return signal
            if increment:
                signal = self._block(increment)
                if signal is not None:
                    return signal
            do_first = False
        self.unknown.update(self._writes(body))
        return None

    def _record(self, decision_id, outcome, leaves=()):
        if not self.recording or decision_id not in self.decision_expr:
            return
        self.outcomes[decision_id] = outcome
        self.observed.setdefault(decision_id, set()).add(outcome)
        if leaves:
            self.conditions.setdefault(decision_id, set()).add(self._pattern(decision_id, leaves))

    def _pattern(self, decision_id, leaves):
        expr = self.decision_expr.get(decision_id)
        atoms = atoms_of(expr) if expr is not None else ()
        if not atoms:
            return tuple(leaves)
        positions = {id(atom): index for index, atom in enumerate(atoms)}
        pattern = [None] * len(atoms)
        values = iter(leaves)
        missing = object()

        def trace(node):
            if isinstance(node, Atom):
                # The runtime condition and the extracted decision expression
                # are not always structurally identical. When fewer observed
                # leaf outcomes are available, keep the remaining positions
                # as None rather than raising StopIteration.
                value = next(values, missing)
                if value is missing:
                    return None
                pattern[positions[id(node)]] = value
                return value

            if isinstance(node, Not):
                value = trace(node.child)
                return None if value is None else not value

            if isinstance(node, And):
                for child in node.children:
                    value = trace(child)
                    if value is None:
                        return None
                    if not value:
                        return False
                return True

            if isinstance(node, Or):
                for child in node.children:
                    value = trace(child)
                    if value is None:
                        return None
                    if value:
                        return True
                return False

            return None

        trace(expr)
        return tuple(pattern)

    def _read(self, path, address=True):
        if path in self.unknown:
            return _UNKNOWN
        if address:
            for name in self._address_names.get(path, ()):
                if name in self.env:
                    return self.env[name]
        if path in self.env:
            return self.env[path]
        pattern = self._normalized_path(path)
        if pattern in self.unknown:
            return _UNKNOWN
        if pattern in self.env:
            return self.env[pattern]
        for key in self.unknown & self._variable_names:
            if self._normalized_names[key] == pattern:
                return _UNKNOWN
        for key in self._variable_names:
            if key in self.env and self._normalized_names[key] == pattern:
                return self.env[key]
        return 0

    def _normalized_path(self, path):
        if path not in self._path_cache:
            self._path_cache[path] = _INDEX_PATH.sub("[*]", path)
        return self._path_cache[path]

    def _legacy_atom(self, atom):
        match = _LEGACY_ATOM.fullmatch(atom.text)
        if match is None:
            return 0
        left_name, operator, right_name = match.groups()
        left = self._read(left_name)
        right = int(right_name) if right_name.isdigit() else self._read(right_name)
        if not right and right_name not in self.env:
            right = self._legacy_constant(right_name)
        return self._binop(operator, left, right)

    def _legacy_constant(self, name):
        return self.constants.get(name, self._array_bound())

    def _array_bound(self):
        bound = 0
        for variable in self.inventory.inputs():
            for value in _ARRAY_INDEX.findall(variable.name):
                bound = max(bound, int(value))
        return bound

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
            self._record(case.decision_id, matched)
            if matched and selected is None:
                selected = index
        selected = default if selected is None else selected
        if selected is None:
            return None
        if statement.cases[selected].default:
            self._record(statement.cases[selected].decision_id, True)
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
        if op == "+": return left + right
        if op == "-": return left - right
        if op == "*": return left * right
        if op == "/": return Interpreter._divide(left, right)
        if op == "%": return Interpreter._mod(left, right)
        if op == "&": return left & right
        if op == "|": return left | right
        if op == "^": return left ^ right
        if op == "<<": return left << right
        if op == ">>": return left >> right
        if op == "==": return int(left == right)
        if op == "!=": return int(left != right)
        if op == ">": return int(left > right)
        if op == ">=": return int(left >= right)
        if op == "<": return int(left < right)
        return int(left <= right) if op == "<=" else 0

    @staticmethod
    def _assign(op, old, value):
        return value if op == "=" else Interpreter._binop(op[:-1], old, value)

    @staticmethod
    def _atom(op, left, right):
        if op == "truthy": return int(bool(left))
        if op == "bitset": return int(bool(left & right))
        if op == "==": return int(left == right)
        if op == "!=": return int(left != right)
        if op == ">": return int(left > right)
        if op == ">=": return int(left >= right)
        if op == "<": return int(left < right)
        return int(left <= right) if op == "<=" else 0

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
