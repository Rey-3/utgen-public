"""Path-sensitive symbolic data-flow over the clang-free function IR."""
from __future__ import annotations

from dataclasses import dataclass, replace

from utgen.model import (And, Assign, Atom, Binop, Break, CallE, Cast, Const,
                         DataflowPoint, DataflowResult, DataflowState, Decl,
                         Direction, DoWhile, ExprStmt, For, If, Index, MemberE,
                         Not, Op, Or, PathConstraint, Phi, Ref, Return, Switch,
                         Ternary, Unary, Unknown, While)


@dataclass
class _Flow:
    active: tuple
    breaks: tuple = ()


class DataflowEngine:
    """Propagate symbolic values while retaining every reachable path."""

    def __init__(self, max_paths: int = 256):
        self.max_paths = max_paths
        self._points = []
        self._returns = []
        self._writes = []
        self._leaves = set()
        self._decisions = {}

    def analyze(self, function_ir):
        """Analyze one function and attach its result to the function IR."""
        self._points, self._returns, self._writes = [], [], []
        self._leaves = set()
        self._decisions = {decision.id: decision.expr for decision in function_ir.decisions}
        values = {}
        aliases = {}
        for leaf in function_ir.leaves:
            if leaf.direction in (Direction.IN, Direction.INOUT):
                values[leaf.name] = Ref(leaf.name)
            if leaf.is_pointer and "." not in leaf.name and "[" not in leaf.name and not leaf.name.startswith("tessy_") and not leaf.name.endswith("()"):
                aliases[leaf.name] = (leaf.name,)
            self._leaves.add(leaf.name)
        initial = DataflowState(values, aliases=aliases)
        self._block((initial,), function_ir.body)
        result = DataflowResult(self._points, self._returns, self._writes)
        function_ir.dataflow = result
        return result

    def _block(self, states, statements):
        active, breaks = tuple(states), ()
        for statement in statements:
            if not active:
                break
            flow = self._statement(active, statement)
            active = flow.active
            breaks += flow.breaks
        return _Flow(self._limit(active), breaks)

    def _statement(self, states, statement):
        if isinstance(statement, Decl):
            result = []
            for state in states:
                if statement.static_name:
                    state = self._bind_static(state, statement)
                elif statement.init is None:
                    state = self._poison(state, (self._path(Ref(statement.name, statement.scope)),))
                else:
                    state = self._assign_lvalue(state, Ref(statement.name, statement.scope), statement.init, "=")
                result.append(self._declare_pointer(state, statement))
            return _Flow(tuple(result))
        if isinstance(statement, Assign):
            result = []
            for state in states:
                result.append(self._assign_lvalue(state, statement.lhs, statement.value, statement.op))
            return _Flow(tuple(result))
        if isinstance(statement, If):
            return self._if(states, statement)
        if isinstance(statement, For):
            return self._for(states, statement)
        if isinstance(statement, (While, DoWhile)):
            return self._loop(states, statement)
        if isinstance(statement, Switch):
            return self._switch(states, statement)
        if isinstance(statement, Return):
            for state in states:
                value = self._substitute(statement.value, state) if statement.value is not None else None
                self._returns.append((state, value))
            return _Flow(())
        if isinstance(statement, Break):
            return _Flow((), tuple(states))
        if isinstance(statement, ExprStmt):
            return _Flow(tuple(self._call_effect(state, statement.expr) for state in states))
        if isinstance(statement, Unknown):
            return _Flow(tuple(self._poison(state, ()) for state in states))
        return _Flow(states)

    def _bind_static(self, state, declaration):
        values = dict(state.values)
        local = self._path(Ref(declaration.name, declaration.scope))
        for name in self._leaves:
            if name == declaration.static_name or name.startswith((declaration.static_name + ".", declaration.static_name + "[")):
                values[local + name[len(declaration.static_name):]] = Ref(name)
        return replace(state, values=values)

    def _if(self, states, statement):
        point_states = []
        then_states, else_states = [], []
        for state in states:
            expression = statement.condition if statement.condition is not None else self._decisions.get(statement.decision_id)
            condition = self._condition(expression, state)
            point_states.append(replace(state, condition=condition))
            true_state = self._branch(state, statement.decision_id, condition, True)
            false_state = self._branch(state, statement.decision_id, condition, False)
            if true_state is not None:
                then_states.append(true_state)
            if false_state is not None:
                else_states.append(false_state)
        self._points.append(DataflowPoint(statement.decision_id, tuple(point_states)))
        then_flow = self._block(tuple(then_states), statement.then)
        else_flow = self._block(tuple(else_states), statement.els)
        active = then_flow.active + else_flow.active
        return _Flow(self._limit(active), then_flow.breaks + else_flow.breaks)

    def _for(self, states, statement):
        initialized = self._block(states, statement.init)
        loop = While(statement.cond, statement.body + statement.increment, statement.runtime_cond)
        return self._loop(initialized.active, loop, self._written(statement.init))

    def _loop(self, states, statement, extra_written=()):
        if isinstance(statement, DoWhile):
            return self._do_loop(states, statement)
        exits, written = [], self._written(statement.body) | set(extra_written)
        for state in states:
            condition = self._condition(statement.cond, state)
            true_state = self._branch(state, None, condition, True)
            false_state = self._branch(state, None, condition, False)
            if false_state is not None:
                exits.append(false_state)
            if true_state is None:
                continue
            body = self._block((true_state,), statement.body)
            exits.extend(body.active)
            exits.extend(body.breaks)
        result = []
        for state in exits:
            result.append(self._invalidate(state, written))
        return _Flow(self._limit(tuple(result)))

    def _do_loop(self, states, statement):
        body = self._block(states, statement.body)
        exits = list(body.breaks)
        for state in body.active:
            condition = self._condition(statement.cond, state)
            false_state = self._branch(state, None, condition, False)
            true_state = self._branch(state, None, condition, True)
            if false_state is not None:
                exits.append(false_state)
            if true_state is not None:
                exits.append(true_state)
        written = self._written(statement.body)
        result = []
        for state in exits:
            result.append(self._invalidate(state, written))
        return _Flow(self._limit(tuple(result)))

    def _switch(self, states, statement):
        all_labels = []
        for case in statement.cases:
            for label in case.labels:
                all_labels.append(label)
        remaining = tuple(states)
        exits = []
        for case in statement.cases:
            case_states, next_states = [], []
            for state in remaining:
                subject = self._substitute(statement.subject, state)
                condition = self._case_condition(subject, case, all_labels)
                self._points.append(DataflowPoint(case.decision_id, (replace(state, condition=condition),)))
                true_state = self._branch(state, case.decision_id, condition, True)
                false_state = self._branch(state, case.decision_id, condition, False)
                if true_state is not None:
                    case_states.append(true_state)
                if false_state is not None:
                    next_states.append(false_state)
            flow = self._block(tuple(case_states), case.body)
            exits.extend(flow.active)
            exits.extend(flow.breaks)
            remaining = tuple(next_states)
        exits.extend(remaining)
        return _Flow(self._limit(tuple(exits)))

    def _case_condition(self, subject, case, all_labels):
        if case.default:
            terms = []
            for label in all_labels:
                terms.append(Unary("!", Binop("==", subject, Const(label))))
            return self._fold(And(tuple(terms))) if terms else Const(1)
        terms = []
        for label in case.labels:
            terms.append(Binop("==", subject, Const(label)))
        if len(terms) == 1:
            return self._fold(terms[0])
        return self._fold(Or(tuple(terms)))

    def _branch(self, state, decision_id, condition, outcome):
        actual = self._const_bool(condition)
        if actual is not None and actual != outcome:
            return None
        constraint = PathConstraint(decision_id, condition, outcome)
        return replace(state, path=state.path + (constraint,), condition=None)

    def _condition(self, expression, state, seen=()):
        if expression is None:
            return None
        if isinstance(expression, (Atom, And, Not, Or)):
            result = self._node_expr(expression, state, seen)
        else:
            result = self._substitute(expression, state, seen)
        result = self._fold(result)
        return None if self._has_unknown(result) else result

    def _node_expr(self, node, state, seen=()):
        if isinstance(node, Atom):
            if node.var is None:
                return Unknown(node.text)
            left = self._substitute(Ref(node.var.name), state, seen)
            if node.op == Op.TRUTHY:
                return Binop("!=", left, Const(0))
            if node.op == Op.BITSET:
                return Binop("!=", Binop("&", left, Const(node.rhs)), Const(0))
            right = Ref(node.rhs_var.name) if node.rhs_var is not None else Const(node.rhs)
            return Binop(str(node.op), left, self._substitute(right, state, seen))
        if isinstance(node, Not):
            return Unary("!", self._node_expr(node.child, state, seen))
        if isinstance(node, And):
            terms = []
            for child in node.children:
                terms.append(self._node_expr(child, state, seen))
            return Binop("&&", terms[0], terms[1]) if len(terms) == 2 else And(tuple(terms))
        if isinstance(node, Or):
            terms = []
            for child in node.children:
                terms.append(self._node_expr(child, state, seen))
            return Binop("||", terms[0], terms[1]) if len(terms) == 2 else Or(tuple(terms))
        return Unknown(str(node))

    def _assign_lvalue(self, state, lhs, expression, operator):
        targets = self._lvalue_targets(lhs, state)
        if targets is None:
            return self._poison(state, tuple(state.values))
        if not targets:
            return self._poison(state, ())
        value = self._substitute(expression, state)
        result = state
        for path in targets:
            result = self._assign_path(result, path, value, operator)
        alias_targets = self._pointer_targets(expression, state)
        key = self._path(lhs)
        if operator == "=" and key:
            aliases = dict(result.aliases)
            aliases[key] = alias_targets if alias_targets is not None else None
            result = replace(result, aliases=aliases)
        return result

    def _assign_path(self, state, path, expression, operator):
        value = expression
        if operator != "=":
            old = state.values.get(path, Ref(path))
            value = Binop(operator[:-1], self._substitute(old, state), expression)
        value = self._fold(value)
        values, unknown = dict(state.values), set(state.unknown)
        values[path], unknown = value, unknown - {path}
        self._writes.append((path, value))
        return replace(state, values=values, unknown=tuple(sorted(unknown)))

    def _declare_pointer(self, state, declaration):
        if not declaration.is_pointer:
            return state
        path = self._path(Ref(declaration.name, declaration.scope))
        aliases = dict(state.aliases)
        aliases.setdefault(path, None)
        return replace(state, aliases=aliases)

    def _lvalue_targets(self, expression, state):
        if isinstance(expression, Unary) and expression.op == "*":
            return self._pointer_targets(expression.x, state)
        if isinstance(expression, MemberE):
            targets = self._member_targets(expression, state)
            if targets is not None:
                return targets
        if isinstance(expression, Index):
            targets = self._index_targets(expression, state)
            if targets is not None:
                return targets
        path = self._path(expression)
        return (path,) if path else None

    def _pointer_targets(self, expression, state, seen=()):
        if isinstance(expression, Unary):
            if expression.op == "&":
                if isinstance(expression.x, (MemberE, Index)):
                    targets = self._member_targets(expression.x, state, seen) if isinstance(expression.x, MemberE) else self._index_targets(expression.x, state, seen)
                    if targets is not None:
                        return targets
                if isinstance(expression.x, Unary) and expression.x.op == "*":
                    return self._pointer_targets(expression.x.x, state, seen)
                path = self._path(expression.x)
                return (path,) if path else None
            if expression.op == "*":
                targets = self._pointer_targets(expression.x, state, seen)
                if targets is None:
                    return None
                result = []
                for target in targets:
                    nested = state.aliases.get(target, (target,))
                    if nested is None:
                        return None
                    result.extend(nested)
                return tuple(result)
            return None
        if isinstance(expression, Ref):
            key = self._path(expression)
            return state.aliases[key] if key in state.aliases else None
        if isinstance(expression, MemberE):
            targets = self._member_targets(expression, state, seen)
            return targets
        if isinstance(expression, Index):
            return self._index_targets(expression, state, seen)
        if isinstance(expression, Const) and expression.value == 0:
            return ()
        return None

    def _member_targets(self, expression, state, seen=()):
        targets = self._pointer_targets(expression.base, state, seen)
        if targets is None:
            return None
        result = []
        for target in targets:
            result.append(f"{target}.{expression.name}")
        return tuple(result)

    def _index_targets(self, expression, state, seen=()):
        targets = self._pointer_targets(expression.base, state, seen)
        if targets is None:
            return None
        index = self._const_int(self._substitute(expression.idx, state, seen))
        result = []
        for target in targets:
            result.append(self._add_index(target, index))
        return tuple(result)

    def _add_index(self, target, index):
        if index is None:
            return f"{target}[*]"
        if target.endswith("]") and "[" in target:
            base, _, old = target.rpartition("[")
            try:
                return f"{base}[{int(old[:-1]) + index}]"
            except ValueError:
                pass
        return f"{target}[{index}]"

    def _load_targets(self, targets, state, seen=()):
        if targets is None:
            return Unknown("unknown pointer target")
        if len(targets) != 1:
            return Unknown("ambiguous pointer target")
        return self._substitute(Ref(targets[0]), state, seen)

    def _substitute(self, expression, state, seen=()):
        stack = [("eval", expression, seen)]
        values = []
        while stack:
            frame = stack.pop()
            kind = frame[0]
            if kind == "build_call":
                name, count = frame[1], frame[2]
                args = tuple(values[-count:]) if count else ()
                if count:
                    del values[-count:]
                values.append(CallE(name, args))
                continue
            if kind == "build_unary":
                values.append(self._fold(Unary(frame[1], values.pop())))
                continue
            if kind == "build_member":
                values.append(MemberE(values.pop(), frame[1]))
                continue
            if kind == "build_index":
                index, base = values.pop(), values.pop()
                values.append(Index(base, index))
                continue
            if kind == "build_binop":
                right, left = values.pop(), values.pop()
                values.append(self._fold(Binop(frame[1], left, right)))
                continue
            if kind == "build_cast":
                values.append(Cast(frame[1], values.pop()))
                continue
            if kind == "build_ternary":
                false, true = values.pop(), values.pop()
                values.append(Ternary(frame[1], true, false))
                continue
            if kind == "build_phi":
                false, true = values.pop(), values.pop()
                values.append(Phi(frame[1], true, false))
                continue

            current, current_seen = frame[1], frame[2]
            if current is None or isinstance(current, (Const, Unknown)):
                values.append(current)
                continue
            if isinstance(current, Ref):
                key = self._path(current)
                if key in state.unknown:
                    values.append(Unknown(key))
                    continue
                value = state.values.get(key)
                if value is None or key in current_seen:
                    values.append(current)
                else:
                    stack.append(("eval", value, current_seen + (key,)))
                continue
            if isinstance(current, Unary) and current.op == "*":
                values.append(self._load_targets(self._pointer_targets(current.x, state, current_seen), state, current_seen))
                continue
            if isinstance(current, MemberE):
                targets = self._member_targets(current, state, current_seen)
                if targets is not None:
                    values.append(self._load_targets(targets, state, current_seen))
                    continue
            if isinstance(current, Index):
                targets = self._index_targets(current, state, current_seen)
                if targets is not None:
                    values.append(self._load_targets(targets, state, current_seen))
                    continue
            path = self._path(current)
            if path and (path in self._leaves or path in state.values):
                stack.append(("eval", Ref(path), current_seen))
                continue
            if isinstance(current, CallE):
                stack.append(("build_call", current.name, len(current.args)))
                for arg in reversed(current.args):
                    stack.append(("eval", arg, current_seen))
                continue
            if isinstance(current, Unary):
                stack.append(("build_unary", current.op))
                stack.append(("eval", current.x, current_seen))
                continue
            if isinstance(current, MemberE):
                stack.append(("build_member", current.name))
                stack.append(("eval", current.base, current_seen))
                continue
            if isinstance(current, Index):
                stack.append(("build_index",))
                stack.append(("eval", current.idx, current_seen))
                stack.append(("eval", current.base, current_seen))
                continue
            if isinstance(current, Binop):
                stack.append(("build_binop", current.op))
                stack.append(("eval", current.r, current_seen))
                stack.append(("eval", current.l, current_seen))
                continue
            if isinstance(current, Cast):
                stack.append(("build_cast", current.type_name))
                stack.append(("eval", current.x, current_seen))
                continue
            if isinstance(current, Ternary):
                condition = self._condition(current.c, state, current_seen)
                outcome = self._const_bool(condition)
                if outcome is not None:
                    stack.append(("eval", current.t if outcome else current.f, current_seen))
                else:
                    stack.append(("build_ternary", condition))
                    stack.append(("eval", current.f, current_seen))
                    stack.append(("eval", current.t, current_seen))
                continue
            if isinstance(current, Phi):
                condition = self._condition(current.condition, state, current_seen)
                outcome = self._const_bool(condition)
                if outcome is True:
                    stack.append(("eval", current.when_true, current_seen))
                elif outcome is False:
                    stack.append(("eval", current.when_false, current_seen))
                else:
                    stack.append(("build_phi", condition))
                    stack.append(("eval", current.when_false, current_seen))
                    stack.append(("eval", current.when_true, current_seen))
                continue
            if isinstance(current, (And, Or, Not, Atom)):
                values.append(self._condition(current, state, current_seen))
                continue
            values.append(current)
        return values.pop()

    def _path(self, expression):
        if isinstance(expression, Ref):
            return f"{expression.path}@{expression.scope}" if expression.scope else expression.path
        if isinstance(expression, CallE):
            return f"tessy_{expression.name}"
        if isinstance(expression, MemberE):
            base = self._path(expression.base)
            return f"{base}.{expression.name}" if base else ""
        if isinstance(expression, Index):
            base = self._path(expression.base)
            index = self._const_int(expression.idx)
            suffix = f"[{index}]" if index is not None else "[*]"
            return f"{base}{suffix}" if base else ""
        return ""

    def _fold(self, expression):
        if isinstance(expression, Unary):
            value = self._const_int(expression.x)
            if value is not None:
                result = {"!": int(not value), "-": -value, "~": ~value}.get(expression.op)
                if result is not None:
                    return Const(result)
            return expression
        if isinstance(expression, Binop):
            left, right = self._const_int(expression.l), self._const_int(expression.r)
            if left is not None and right is not None:
                result = self._eval(expression.op, left, right)
                if result is not None:
                    return Const(result)
            return expression
        if isinstance(expression, (And, Or)):
            terms = []
            for child in expression.children:
                terms.append(self._fold(child))
            return And(tuple(terms)) if isinstance(expression, And) else Or(tuple(terms))
        return expression

    def _eval(self, operator, left, right):
        if operator == "+": return left + right
        if operator == "-": return left - right
        if operator == "*": return left * right
        if operator == "/" and right: return int(left / right)
        if operator == "%" and right: return left % right
        if operator == "&": return left & right
        if operator == "|": return left | right
        if operator == "^": return left ^ right
        if operator == "<<" and right >= 0: return left << right
        if operator == ">>" and right >= 0: return left >> right
        if operator in ("==", "!=", "<", "<=", ">", ">="):
            return int({"==": left == right, "!=": left != right, "<": left < right,
                        "<=": left <= right, ">": left > right, ">=": left >= right}[operator])
        if operator == "&&": return int(bool(left) and bool(right))
        if operator == "||": return int(bool(left) or bool(right))
        return None

    def _const_int(self, expression):
        return expression.value if isinstance(expression, Const) and isinstance(expression.value, int) else None

    def _const_bool(self, expression):
        value = self._const_int(expression)
        return None if value is None else bool(value)

    def _has_unknown(self, expression):
        if isinstance(expression, Unknown):
            return True
        if isinstance(expression, Unary):
            return self._has_unknown(expression.x)
        if isinstance(expression, Binop):
            return self._has_unknown(expression.l) or self._has_unknown(expression.r)
        if isinstance(expression, Ternary):
            return (self._has_unknown(expression.c) or self._has_unknown(expression.t)
                    or self._has_unknown(expression.f))
        if isinstance(expression, Cast):
            return self._has_unknown(expression.x)
        if isinstance(expression, Phi):
            return (self._has_unknown(expression.condition) or self._has_unknown(expression.when_true)
                    or self._has_unknown(expression.when_false))
        if isinstance(expression, (And, Or)):
            return any(self._has_unknown(child) for child in expression.children)
        return False

    def _call_effect(self, state, expression):
        if isinstance(expression, CallE):
            names = list(state.values)
            pointer_arg = False
            for index, argument in enumerate(expression.args):
                targets = self._pointer_argument_targets(argument, state)
                if targets is None:
                    pointer_arg = True
                    names.append(f"tessy_{expression.name}__Arg{index}")
                    continue
                if not targets:
                    continue
                pointer_arg = True
                names.extend(targets)
                names.append(f"tessy_{expression.name}__Arg{index}")
            return self._poison(state, tuple(names)) if pointer_arg else self._poison(state, tuple(state.values))
        if isinstance(expression, Unary) and expression.op in ("++", "--"):
            path = self._path(expression.x)
            if path in state.aliases:
                aliases = dict(state.aliases)
                aliases[path] = None
                return self._poison(replace(state, aliases=aliases), (path,))
        return state

    def _pointer_argument_targets(self, argument, state):
        if isinstance(argument, (Unary, MemberE, Index)):
            return self._pointer_targets(argument, state)
        if isinstance(argument, Ref) and self._path(argument) in state.aliases:
            return self._pointer_targets(argument, state)
        return ()

    def _poison(self, state, names):
        unknown = set(state.unknown)
        for name in names:
            if name:
                unknown.add(name)
        return replace(state, unknown=tuple(sorted(unknown)))

    def _invalidate(self, state, names):
        values, unknown = dict(state.values), set(state.unknown)
        aliases = dict(state.aliases)
        if "*" in names:
            unknown.update(values)
            values.clear()
            for key in aliases:
                aliases[key] = None
            return replace(state, values=values, aliases=aliases, unknown=tuple(sorted(unknown)))
        for name in names:
            values.pop(name, None)
            aliases.pop(name, None)
            unknown.add(name)
        return replace(state, values=values, aliases=aliases, unknown=tuple(sorted(unknown)))

    def _written(self, statements):
        names = set()
        for statement in statements:
            if isinstance(statement, Decl):
                names.add(self._path(Ref(statement.name, statement.scope)))
            elif isinstance(statement, Assign):
                path = self._path(statement.lhs)
                names.add(path or "*")
            elif isinstance(statement, If):
                names.update(self._written(statement.then))
                names.update(self._written(statement.els))
            elif isinstance(statement, For):
                names.update(self._written(statement.init))
                names.update(self._written(statement.body))
                names.update(self._written(statement.increment))
            elif isinstance(statement, (While, DoWhile)):
                names.update(self._written(statement.body))
            elif isinstance(statement, Switch):
                for case in statement.cases:
                    names.update(self._written(case.body))
        return names

    def _limit(self, states):
        if len(states) <= self.max_paths:
            return tuple(states)
        return (self._merge(states),)

    def _merge(self, states):
        values, aliases, unknown = {}, {}, set()
        keys = set()
        for state in states:
            keys.update(state.values)
            unknown.update(state.unknown)
        for key in keys:
            candidate = states[0].values.get(key)
            if all(candidate == state.values.get(key) for state in states):
                values[key] = candidate
            else:
                unknown.add(key)
        alias_keys = set()
        for state in states:
            alias_keys.update(state.aliases)
        for key in alias_keys:
            candidate = states[0].aliases.get(key)
            aliases[key] = candidate if all(candidate == state.aliases.get(key) for state in states) else None
        prefix = list(states[0].path)
        for state in states[1:]:
            length = min(len(prefix), len(state.path))
            while len(prefix) > length or prefix and prefix[-1] != state.path[len(prefix) - 1]:
                prefix.pop()
        return DataflowState(values, aliases=aliases, path=tuple(prefix), unknown=tuple(sorted(unknown)))
