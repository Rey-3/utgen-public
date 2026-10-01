
from __future__ import annotations

from collections import deque
import re
import time
import warnings
from dataclasses import fields, is_dataclass

from utgen.interp import Interpreter
from utgen.model import (And, Atom, Binop, CallE, Cast, Const, Direction, FunctionIR, Index,
                         Inventory, MemberE, Not, Or, Ref, TestRow, Unary,
                         Ternary, Unknown, atom_text, atoms_of, expand_array_names)


class CoverageGenerator:
    """Generate extra concrete test rows to close remaining coverage gaps.

    The generator starts from already-created rows, executes them through the
    interpreter, then mutates input values to discover branch/condition outcomes
    and MC/DC pairs that symbolic generation did not reach.
    """

    def __init__(self, max_seconds: float = 15.0) -> None:
        self.max_seconds = max_seconds
        self._function = None
        self._inputs = ()
        self._results = {}

    def rows(self, function: FunctionIR, existing: list[TestRow], decision_ids=None) -> list[TestRow]:
        """Repair uncovered targets with a finite, decision-scoped concrete search.

        All existing rows count as coverage evidence, but only rows intended for or
        observed at the selected decision become mutation seeds. Search begins with
        the selected decision's inputs and expands to enclosing guard inputs only
        when reachability still blocks the target.
        """
        inputs = [leaf for leaf in function.leaves if leaf.direction in (Direction.IN, Direction.INOUT)]
        if not inputs or not function.body:
            return []

        interpreter = Interpreter(Inventory(function.leaves, function.stubs))
        self._set_function(function)
        selected = ({decision.id for decision in function.decisions}
                    if decision_ids is None else set(decision_ids))
        targets = self._targets(function, selected)
        index_values = self._index_values(function.body)

        # Existing rows are evidence, not automatically mutation seeds. Running them
        # once preserves any coverage they already provide while avoiding the old
        # "every historical row x every input" frontier explosion.
        state = _CoverageState(function)
        helper_patterns = {}
        evidence, evidence_keys = [], set()
        for row in existing:
            values = row.values.copy()
            key = self._key(values)
            if key in evidence_keys:
                continue
            evidence_keys.add(key)
            result = self._run(interpreter, function, values, key)
            evidence.append((row, values, result))
            state.add(result)

        if targets <= state.covered:
            return []

        initial_executions = len(self._results)

        # Prefer symbolic rows created for this target. Rows that reached the target
        # incidentally are also good seeds. Fall back to one neutral complete row.
        guards = self._guard_requirements(function, selected)
        seed_decisions = selected | {decision_id for decision_id, _ in guards}
        seeds, seed_keys, preferred_seeds = [], set(), set()
        for row, values, result in evidence:
            guard_seed = bool(guards) and row.intent.get(guards[-1][0]) == (not guards[-1][1])
            if not (selected.intersection(row.intent) or selected.intersection(result.observed) or guard_seed):
                continue
            key = self._key(values)
            if selected.intersection(row.intent) or guard_seed:
                preferred_seeds.add(key)
            if key in seed_keys:
                continue
            seed_keys.add(key)
            seeds.append(values)
        if not seeds:
            seeds.append(self._defaults(inputs))

        helpers = set()
        controls = {}
        operands = set()
        relevant, combination_inputs = self._decision_inputs(function, inputs, selected, helpers, controls, operands)
        expanded = set(selected)
        direct = set(combination_inputs)
        for decision in function.decisions:
            if decision.id in seed_decisions:
                for atom in atoms_of(decision.expr):
                    direct.update(variable.name for variable in (atom.var, atom.rhs_var) if variable is not None)
                    if decision.id in selected:
                        operands.update(variable.name for variable in (atom.var, atom.rhs_var) if variable is not None)
        operand_outputs = {name for leaf in inputs if leaf.direction == Direction.INOUT and leaf.name in operands
                           for name in expand_array_names(leaf.name) or (leaf.name,)}
        prefer_controls = any(atom.var is not None for decision in function.decisions if decision.id in selected
                              for atom in atoms_of(decision.expr))
        generated = []
        emitted = {self._key(row.values) for row in existing}

        # ponytail: bound candidate search per decision; dependency setup is not search time.
        deadline = time.monotonic() + self.max_seconds
        while True:
            search_inputs = [leaf for leaf in inputs if leaf.name in relevant]
            search_inputs.sort(key=lambda leaf: (leaf.name not in operands,
                                                 prefer_controls and leaf.name not in controls,
                                                 controls.get(leaf.name, float("inf")) if prefer_controls else 0,
                                                 leaf.name not in direct, not leaf.name.startswith("tessy_")))
            search_results = []

            if search_inputs:
                # Freeze each input's representative domain to its seed.
                # This prevents variable-to-variable probes from walking across the
                # integer range as other inputs are mutated.
                queue, deferred, focused_seeds = deque(), deque(), set()
                for index, values in sorted(enumerate(seeds), key=lambda item: self._key(item[1]) not in preferred_seeds):
                    key = self._search_key(None, values, search_inputs)
                    if key not in focused_seeds:
                        focused_seeds.add(key)
                        frontier = queue if self._key(values) in preferred_seeds or search_inputs[0].name in operands else deferred
                        frontier.append((index, values.copy(), 0))
                seen = {
                    self._search_key(index, values, search_inputs)
                    for index, values in enumerate(seeds)
                }
                domains = {}
                initial_pass = True

                while queue or deferred:
                    if not queue:
                        queue, deferred = deferred, queue
                        initial_pass = False
                    seed_index, base, leaf_index = queue.popleft()
                    if seed_index not in helper_patterns:
                        helper_patterns[seed_index] = self._helper_patterns(
                            self._run(interpreter, function, seeds[seed_index]), helpers,
                        )
                    for leaf in search_inputs[leaf_index:]:
                        # Visit one input per seed before deeper combinations take over.
                        if leaf_index + 1 < len(search_inputs):
                            # Try direct operands in every seed before its indirect frontier.
                            frontier = queue if (not initial_pass or self._key(seeds[seed_index]) in preferred_seeds
                                                 or search_inputs[leaf_index + 1].name in operands) else deferred
                            frontier.append((seed_index, base, leaf_index + 1))
                        domain_key = seed_index, leaf.name
                        if domain_key not in domains:
                            domains[domain_key] = self._values(
                                leaf, function, seeds[seed_index], index_values, expanded,
                            )
                        for value in domains[domain_key]:
                            if value == base.get(leaf.name):
                                continue
                            candidate = base | {leaf.name: value}
                            search_key = self._search_key(seed_index, candidate, search_inputs)
                            if search_key in seen:
                                continue
                            seen.add(search_key)

                            key = self._key(candidate)
                            if time.monotonic() >= deadline:
                                warnings.warn(
                                    f"{function.name}: coverage search for decisions {sorted(selected)} "
                                    f"stopped at the {self.max_seconds:g}s budget after "
                                    f"{len(self._results) - initial_executions} new executions; "
                                    f"{len(targets - state.covered)} targets remain uncovered.",
                                    RuntimeWarning,
                                    stacklevel=2,
                                )
                                return generated
                            result = self._run(interpreter, function, candidate, key)
                            search_results.append(result)
                            gained = state.gained(result) & targets
                            novel = state.novel(result, expanded)
                            base_result = self._run(interpreter, function, base) if helpers or guards or operand_outputs else None
                            control_progress = {
                                ("guard", decision_id, required)
                                for decision_id, required in guards
                                if required in result.observed.get(decision_id, set())
                                and required not in base_result.observed.get(decision_id, set())
                            } - helper_patterns[seed_index]
                            # An upstream control can change a counter before its condition flips.
                            # ponytail: final INOUT values approximate progress until decision snapshots are available.
                            control_progress |= {
                                ("operand", name, result.outputs[name])
                                for name in operand_outputs
                                if leaf.name not in operands and name in result.outputs and name in base_result.outputs
                                and result.outputs[name] != base_result.outputs[name]
                            } - helper_patterns[seed_index]
                            helper_progress = self._helper_patterns(result, helpers) - helper_patterns[seed_index]
                            if helper_progress and selected.intersection(base_result.observed) and not selected.intersection(result.observed):
                                helper_progress = set()
                            useful = bool(gained or novel or control_progress or helper_progress)
                            if useful:
                                helper_patterns[seed_index].update(helper_progress)
                                helper_patterns[seed_index].update(control_progress)
                                state.add(result)
                                if (gained or novel) and key not in emitted:
                                    emitted.add(key)
                                    generated.append(TestRow(
                                        candidate,
                                        "Coverage",
                                        self._description(gained),
                                    ))
                                if targets <= state.covered:
                                    return generated

                            # Direct symbolic inputs already have combined values from
                            # the solver/MC/DC seed. Only indirect/path-derived inputs
                            # need neutral intermediate states to build combinations.
                            if useful:
                                # New coverage supplies a better seed for arithmetic boundaries.
                                new_index = seed_index
                                if gained or novel:
                                    new_index = len(seeds)
                                    seeds.append(candidate)
                                    preferred_seeds.add(key)
                                next_leaf = 0 if gained or novel or control_progress else (leaf_index + 1) % len(search_inputs)
                                # Helper-only progress must not starve the remaining inputs.
                                if gained or novel or control_progress or not operand_outputs:
                                    queue.appendleft((new_index, candidate, next_leaf))
                                else:
                                    deferred.append((new_index, candidate, next_leaf))
                                if next_leaf:
                                    deferred.append((new_index, candidate, 0))
                            elif leaf.name in combination_inputs:
                                deferred.append((seed_index, candidate, 0))
                        break

            observed_results = [result for _, _, result in evidence] + search_results
            # If the target was never reached, expand only its enclosing guards.
            guard_id = None
            for candidate_id, required in guards:
                if candidate_id in expanded:
                    continue
                if not any(required in result.observed.get(candidate_id, set())
                           for result in observed_results):
                    guard_id = candidate_id
                    break
            if guard_id is None:
                guard_id = next(
                    (candidate_id for candidate_id, _ in guards if candidate_id not in expanded),
                    None,
                )
            if guard_id is None:
                return generated

            expanded.add(guard_id)
            guard_inputs, guard_combinations = self._decision_inputs(function, inputs, (guard_id,), helpers, controls)
            relevant.update(guard_inputs)
            combination_inputs.update(guard_combinations)

    def _run(self, interpreter, function, values, key=None):
        """Execute one normalized input set, caching identical executions."""
        self._set_function(function)
        key = self._key(values) if key is None else key
        if key not in self._results:
            self._results[key] = interpreter.run(function, values)
        return self._results[key]

    @staticmethod
    def _helper_patterns(result, helpers=None):
        out = set()
        for key, patterns in getattr(result, "helper_conditions", {}).items():
            if helpers is not None and key[0] not in helpers:
                continue
            for pattern in patterns:
                out.add((key, pattern))
        return out

    def _set_function(self, function):
        # Cached execution results are valid only for the exact FunctionIR object.
        # Switching functions also rebuilds the ordered input list used by _key().
        if function is self._function:
            return
        self._function, self._results = function, {}
        self._inputs = tuple(leaf for leaf in function.leaves if leaf.direction in (Direction.IN, Direction.INOUT))

    @staticmethod
    def _targets(function: FunctionIR, decision_ids=None) -> set[tuple]:
        """Encode all coverage obligations into comparable tuple keys.

        A decision contributes branch True/False targets, each atom contributes
        condition True/False targets, and controllable atoms also contribute one
        MC/DC independence target.
        """
        targets = set()
        decision_ids = None if decision_ids is None else set(decision_ids)
        for decision in function.decisions:
            if decision_ids is not None and decision.id not in decision_ids:
                continue
            targets.add((decision.id, False))
            targets.add((decision.id, True))
            for index, atom in enumerate(atoms_of(decision.expr)):
                targets.add((decision.id, atom_text(atom), False))
                targets.add((decision.id, atom_text(atom), True))
                if atom.var is not None:
                    targets.add((decision.id, "mcdc", index))
        return targets

    @staticmethod
    def _outcome(expr, pattern, positions):
        if isinstance(expr, Atom):
            return pattern[positions[id(expr)]]
        if isinstance(expr, Not):
            value = CoverageGenerator._outcome(expr.child, pattern, positions)
            return None if value is None else not value
        if isinstance(expr, And):
            for child in expr.children:
                value = CoverageGenerator._outcome(child, pattern, positions)
                if value is None:
                    return None
                if not value:
                    return False
            return True
        if isinstance(expr, Or):
            for child in expr.children:
                value = CoverageGenerator._outcome(child, pattern, positions)
                if value is None:
                    return None
                if value:
                    return True
            return False
        return None

    @staticmethod
    def _defaults(inputs) -> dict:
        # Use zero when it fits the C type. Address-marker leaves use 1 so pointer
        # inputs begin in a non-NULL state unless a test explicitly changes them.
        values = {}
        for leaf in inputs:
            values[leaf.name] = 1 if leaf.address_of else min(max(0, leaf.ctype.lo), leaf.ctype.hi)
        return values

    @staticmethod
    def _values(leaf, function: FunctionIR, base=None, extra=(), decision_ids=None) -> tuple:
        """Build finite representative mutations for the active dependency set."""
        values = {leaf.ctype.lo, leaf.ctype.hi, 0, 1, 2, 3, *extra}
        if base is not None and isinstance(base.get(leaf.name), int):
            values.add(base[leaf.name])

        selected = None if decision_ids is None else set(decision_ids)
        for decision in function.decisions:
            if selected is not None and decision.id not in selected:
                continue
            for atom in atoms_of(decision.expr):
                left_matches = atom.var is not None and atom.var.name == leaf.name
                right_matches = atom.rhs_var is not None and atom.rhs_var.name == leaf.name
                if left_matches and isinstance(atom.rhs, int):
                    values.update((atom.rhs - 1, atom.rhs, atom.rhs + 1))
                other = atom.rhs_var if left_matches else atom.var if right_matches else None
                if other is not None and base is not None and isinstance(base.get(other.name), int):
                    value = base[other.name]
                    values.update((value - 1, value, value + 1))
        if base is not None and function.dataflow is not None:
            linear_values = {}
            for name, value in base.items():
                linear_values[re.sub(r"\[\d+\]", "[*]", name)] = value
            name = re.sub(r"\[\d+\]", "[*]", leaf.name)
            visited = set()

            def probe(expression):
                if id(expression) in visited:
                    return
                visited.add(id(expression))
                if isinstance(expression, Binop) and expression.op in ("==", "!=", "<", "<=", ">", ">="):
                    left = CoverageGenerator._linear_value(expression.l, name, linear_values)
                    right = CoverageGenerator._linear_value(expression.r, name, linear_values)
                    if left is not None and right is not None and left[0] != right[0]:
                        boundary = (right[1] - left[1]) // (left[0] - right[0])
                        values.update((boundary - 1, boundary, boundary + 1))
                if is_dataclass(expression):
                    for item in fields(expression):
                        probe(getattr(expression, item.name))
                elif isinstance(expression, (tuple, list)):
                    for item in expression:
                        probe(item)

            for decision in function.decisions:
                if selected is not None and decision.id not in selected:
                    continue
                try:
                    point = function.dataflow.point(decision.id)
                except KeyError:
                    continue
                for state in point.states:
                    probe(state.condition)
        return tuple(value for value in sorted(values) if leaf.ctype.lo <= value <= leaf.ctype.hi)

    @staticmethod
    def _linear_value(expression, name, values):
        """Resolve an additive expression as coefficient * input + offset."""
        if isinstance(expression, Const) and isinstance(expression.value, int):
            return 0, expression.value
        if isinstance(expression, Cast):
            return CoverageGenerator._linear_value(expression.x, name, values)
        path = CoverageGenerator._expression_path(expression)
        if path:
            path = re.sub(r"\[\d+\]", "[*]", path)
            if path == name:
                return 1, 0
            value = values.get(path)
            if isinstance(value, int):
                return 0, value
        if isinstance(expression, Binop) and expression.op in ("+", "-"):
            left = CoverageGenerator._linear_value(expression.l, name, values)
            right = CoverageGenerator._linear_value(expression.r, name, values)
            if left is not None and right is not None:
                sign = 1 if expression.op == "+" else -1
                return left[0] + sign * right[0], left[1] + sign * right[1]
        return None

    @staticmethod
    def _search_key(seed_index, values, search_inputs) -> tuple:
        """Key effective inputs, including concrete array overrides, by seed."""
        key = []
        for leaf in search_inputs:
            default = values.get(leaf.name, 1 if leaf.address_of else 0)
            for name in expand_array_names(leaf.name) or (leaf.name,):
                value = values.get(name, default)
                key.append((name, 0 if value == "NULL" else value))
        return seed_index, tuple(key)

    @staticmethod
    def _guard_requirements(function, decision_ids) -> list[tuple[int, bool]]:
        """Return enclosing guard decision IDs and required outcomes in source order."""
        selected = set(decision_ids)
        out, seen = [], set()
        for decision in function.decisions:
            if decision.id not in selected:
                continue
            for guard in decision.guards:
                for candidate in function.decisions:
                    if candidate.expr is guard.expr or candidate.expr == guard.expr:
                        key = candidate.id, guard.outcome
                        if key not in seen:
                            seen.add(key)
                            out.append(key)
                        break
        return out

    @staticmethod
    def _decision_inputs(function, inputs, decision_ids, helpers=None, controls=None, operands=None) -> tuple[set[str], set[str]]:
        """Find inputs that can influence the selected decisions.

        Direct symbolic variables come from decision atoms. IR expression paths are
        scanned as well so array/member/dereferenced inputs that were not represented
        by Atom.var can still participate in multi-step candidate combinations.
        Optional output mappings rank controls by branch distance and identify
        direct operand inputs without changing the dependency set.
        """
        selected = ({decision.id for decision in function.decisions}
                    if decision_ids is None else set(decision_ids))
        names = set()
        for decision in function.decisions:
            if decision.id not in selected:
                continue
            for atom in atoms_of(decision.expr):
                if atom.var is not None:
                    names.add(atom.var.name)
                if atom.rhs_var is not None:
                    names.add(atom.rhs_var.name)
        raw_paths, control_paths, conditions = set(), {}, {}

        def visit(value):
            if is_dataclass(value):
                decision_id = getattr(value, "decision_id", None)
                if decision_id in selected:
                    condition = value.c if isinstance(value, Ternary) else getattr(value, "condition", getattr(value, "value", None))
                    conditions[decision_id] = condition
                for item in fields(value):
                    visit(getattr(value, item.name))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    visit(item)

        visit(function.body)
        operand_paths = set()
        for decision_id in selected:
            CoverageGenerator._expression_paths(conditions.get(decision_id), operand_paths)
        operand_normalized = {re.sub(r"\[\d+\]", "[*]", path) for path in operand_paths}
        dataflow_paths, memo = set(), {"helpers": helpers if helpers is not None else set()}
        return_paths = {"return_inputs": True}
        # Helper history must not outrank the current path's actual controls.
        argument_paths = {"arguments_only": True}
        for decision_id in selected:
            try:
                if function.dataflow is None:
                    raise KeyError(decision_id)
                point = function.dataflow.point(decision_id)
            except KeyError:
                CoverageGenerator._expression_paths(conditions.get(decision_id), raw_paths)
            else:
                for state in point.states:
                    bindings = getattr(state, "values", {})
                    CoverageGenerator._dataflow_paths(
                        function, conditions.get(decision_id), raw_paths, bindings, memo=return_paths,
                    )
                    if controls is not None:
                        paths = set()
                        CoverageGenerator._dataflow_paths(
                            function, conditions.get(decision_id), paths, bindings, memo=argument_paths,
                        )
                        for path in paths:
                            control_paths.setdefault(path, float("inf"))
                        # The closest branch controls an operand's most recent reset/increment.
                        for distance, constraint in enumerate(reversed(getattr(state, "path", ()))):
                            CoverageGenerator._dataflow_paths(
                                function, constraint.expr, dataflow_paths, bindings, resolved=True, memo=memo,
                            )
                            paths = set()
                            CoverageGenerator._dataflow_paths(
                                function, constraint.expr, paths, bindings, resolved=True, memo=argument_paths,
                            )
                            for path in paths:
                                control_paths[path] = min(distance, control_paths.get(path, float("inf")))
                    CoverageGenerator._dataflow_paths(
                        function, state.condition, dataflow_paths, bindings, resolved=True, memo=memo,
                    )

        # Raw indirect paths can require neutral multi-input combinations. Dataflow
        # provenance only broadens which upstream inputs are tried; it does not make
        # those inputs an exhaustive Cartesian frontier unless they become useful.
        symbolic = set(names)
        raw_normalized = {
            re.sub(r"\[\d+\]", "[*]", path) for path in raw_paths
        }
        normalized = raw_normalized | {
            re.sub(r"\[\d+\]", "[*]", path) for path in dataflow_paths
        }
        control_normalized = {}
        for path, distance in control_paths.items():
            name = re.sub(r"\[\d+\]", "[*]", path)
            control_normalized[name] = min(distance, control_normalized.get(name, float("inf")))
        combination_inputs = set()

        def matches(leaf_name, paths):
            return leaf_name in paths or any(
                leaf_name.startswith(path + ".") or leaf_name.startswith(path + "[")
                for path in paths
            )

        for leaf in inputs:
            leaf_name = re.sub(r"\[\d+\]", "[*]", leaf.name)
            if matches(leaf_name, normalized):
                names.add(leaf.name)
            if leaf.name not in symbolic and matches(leaf_name, raw_normalized):
                combination_inputs.add(leaf.name)
            if controls is not None and matches(leaf_name, control_normalized):
                distance = min(distance for path, distance in control_normalized.items() if matches(leaf_name, (path,)))
                controls[leaf.name] = min(distance, controls.get(leaf.name, float("inf")))
            if operands is not None and matches(leaf_name, operand_normalized):
                operands.add(leaf.name)
        return names, combination_inputs

    @staticmethod
    def _dataflow_paths(function, value, paths, bindings=None, seen=(), resolved=False, memo=None) -> None:
        """Reuse shared snapshot paths within this dependency collection."""
        if memo is None:
            memo = {}
        identity = value if isinstance(value, (Ref, Unknown)) else id(value)
        key = (id(function), identity, id(bindings), frozenset(seen), resolved)
        if key not in memo:
            # Retain both objects so temporary references cannot reuse their IDs.
            memo[key] = (value, bindings, set())
            CoverageGenerator._collect_dataflow_paths(function, value, memo[key][2], bindings, seen, resolved, memo)
        paths.update(memo[key][2])

    @staticmethod
    def _collect_dataflow_paths(function, value, paths, bindings, seen, resolved, memo) -> None:
        """Collect input paths while preserving already-substituted value snapshots."""
        if isinstance(value, CallE):
            helper = function.local_functions.get(value.name)
            if helper is not None and ("call", helper.name) not in seen:
                if "helpers" in memo:
                    memo["helpers"].add(helper.name)
                helper_seen = seen + (("call", helper.name),)
                used = set()
                returns = getattr(helper.dataflow, "returns", ())
                has_writes = any(leaf.direction != Direction.IN and leaf.name != f"{helper.name}()"
                                 for leaf in helper.leaves)
                if memo.get("return_inputs") and returns and not has_writes:
                    for state, returned in returns:
                        CoverageGenerator._dataflow_paths(helper, returned, used, state.values, helper_seen, resolved=True, memo=memo)
                        for constraint in state.path:
                            CoverageGenerator._dataflow_paths(helper, constraint.expr, used, state.values, helper_seen, resolved=True, memo=memo)
                else:
                    # ponytail: conservatively retain side-effect reads until write snapshots are available.
                    CoverageGenerator._dataflow_paths(helper, helper.body, used, seen=helper_seen, memo=memo)
                used = {re.sub(r"\[\d+\]", "[*]", path) for path in used}
                mapped = False
                for leaf in helper.leaves:
                    if leaf.direction not in (Direction.IN, Direction.INOUT):
                        continue
                    name = re.sub(r"\[\d+\]", "[*]", leaf.name)
                    if used and not any(name == path or name.startswith((path + ".", path + "[")) for path in used):
                        continue
                    matched_param = False
                    for index, (_, name) in enumerate(helper.params):
                        if not (
                            leaf.name == name
                            or leaf.name.startswith(name + ".")
                            or leaf.name.startswith(name + "[")
                        ):
                            continue
                        matched_param = True
                        if index < len(value.args):
                            argument = value.args[index]
                            base = CoverageGenerator._expression_path(argument)
                            if leaf.name == name:
                                CoverageGenerator._dataflow_paths(
                                    function, argument, paths, bindings, helper_seen, resolved=resolved, memo=memo,
                                )
                                mapped = True
                            elif base:
                                path = base + leaf.name[len(name):]
                                CoverageGenerator._dataflow_paths(
                                    function, CoverageGenerator._path_reference(argument, path),
                                    paths, bindings, helper_seen, resolved=resolved, memo=memo,
                                )
                                mapped = True
                        break
                    if not matched_param and not memo.get("arguments_only"):
                        # Caller bindings include later calls, not this helper's entry state.
                        paths.add(leaf.name)
                        # ponytail: conservatively retain earlier global-copy sources;
                        # exact call-time snapshots would require a larger IR change.
                        path = re.sub(r"\[\d+\]", "[*]", leaf.name)
                        write_key = ("writes", id(function))
                        if write_key not in memo:
                            memo[write_key] = {}
                            for written, origin in getattr(function.dataflow, "writes", ()):
                                while isinstance(origin, Cast):
                                    origin = origin.x
                                if isinstance(origin, (Ref, CallE, Binop)):
                                    name = re.sub(r"\[\d+\]", "[*]", written)
                                    memo[write_key].setdefault(name, []).append(origin)
                        prefixes = (path, *(path[:match.start()] for match in re.finditer(r"[.\[]", path)))
                        for name in prefixes:
                            for origin in memo[write_key].get(name, ()):
                                base = CoverageGenerator._expression_path(origin)
                                expression = CoverageGenerator._path_reference(origin, base + path[len(name):]) if path != name else origin
                                CoverageGenerator._dataflow_paths(function, expression, paths, bindings, helper_seen, resolved=True, memo=memo)
                        mapped = True
                if mapped:
                    return

        path = CoverageGenerator._expression_path(value)
        if path:
            # Resolved references are snapshots; Unknown locals still need provenance.
            if resolved:
                paths.add(path)
                if not isinstance(CoverageGenerator._path_reference(value, path), Unknown):
                    return
            if "[*]" in path and bindings:
                matched = False
                for key, bound in bindings.items():
                    if key not in seen and key != path and re.sub(r"\[\d+\]", "[*]", key) == path:
                        CoverageGenerator._dataflow_paths(function, bound, paths, bindings, seen + (key,), resolved=True, memo=memo)
                        matched = True
                if matched:
                    return
            # Resolve locals and aggregate copies from the existing dataflow bindings.
            # Unknown values still carry dependency names; they are not treated as known values.
            key, suffix = path, ""
            while bindings and key:
                if key in bindings and key not in seen:
                    bound = bindings[key]
                    base = CoverageGenerator._expression_path(bound)
                    if base != key:
                        expression = CoverageGenerator._path_reference(bound, base + suffix) if base and suffix else bound
                        # Bindings already describe the value at assignment time.
                        CoverageGenerator._dataflow_paths(function, expression, paths, bindings, seen + (key,), resolved=True, memo=memo)
                        return
                split = max(key.rfind("."), key.rfind("["))
                if split < 0:
                    break
                key, suffix = key[:split], key[split:] + suffix
            paths.add(path)
            return
        if is_dataclass(value):
            for item in fields(value):
                CoverageGenerator._dataflow_paths(
                    function, getattr(value, item.name), paths, bindings, seen, resolved=resolved, memo=memo,
                )
        elif isinstance(value, (tuple, list)):
            for item in value:
                CoverageGenerator._dataflow_paths(function, item, paths, bindings, seen, resolved=resolved, memo=memo)

    @staticmethod
    def _path_reference(value, path):
        """Preserve unknown provenance when rebuilding a field or argument path."""
        while isinstance(value, (MemberE, Index, Unary)):
            value = value.x if isinstance(value, Unary) else value.base
        return Unknown(path) if isinstance(value, Unknown) else Ref(path)

    @staticmethod
    def _expression_paths(value, paths) -> None:
        # Keep only the complete lvalue. Recording parent paths would make every
        # sibling field under the same struct look like a decision dependency.
        path = CoverageGenerator._expression_path(value)
        if path:
            paths.add(path)
            return
        if is_dataclass(value):
            for item in fields(value):
                CoverageGenerator._expression_paths(getattr(value, item.name), paths)
        elif isinstance(value, (tuple, list)):
            for item in value:
                CoverageGenerator._expression_paths(item, paths)

    @staticmethod
    def _expression_path(value) -> str:
        """Normalize an IR reference into a comparable leaf path.

        Concrete array indices become ``[*]`` so an expression such as a[2] can be
        matched back to the corresponding inventory leaf regardless of its index.
        Address/dereference wrappers keep the same underlying storage path.
        """
        if isinstance(value, Ref):
            return f"{value.path}@{value.scope}" if value.scope else value.path
        if isinstance(value, Unknown):
            return value.text
        if isinstance(value, CallE):
            return f"tessy_{value.name}"
        if isinstance(value, MemberE):
            base = CoverageGenerator._expression_path(value.base)
            return f"{base}.{value.name}" if base else ""
        if isinstance(value, Index):
            base = CoverageGenerator._expression_path(value.base)
            return f"{base}[*]" if base else ""
        if isinstance(value, Unary) and value.op in ("&", "*"):
            return CoverageGenerator._expression_path(value.x)
        return ""

    @staticmethod
    def _index_values(value) -> set[int]:
        """Collect literal array indices used by the function body as useful probes."""
        values = set()

        def visit(item):
            if isinstance(item, Index) and isinstance(item.idx, Const) and isinstance(item.idx.value, int):
                values.add(item.idx.value)
            if is_dataclass(item):
                for child in fields(item):
                    visit(getattr(item, child.name))
            elif isinstance(item, (tuple, list)):
                for child in item:
                    visit(child)

        visit(value)
        return values

    def _key(self, values: dict) -> tuple:
        # Normalize only actual function inputs into a stable tuple. This makes cache
        # and duplicate detection insensitive to dictionary order or unrelated values.
        # NULL is normalized to 0 because the interpreter uses the same representation.
        return self._search_key(None, values, self._inputs)[1]

    @staticmethod
    def _description(gained: set[tuple]) -> str:
        decision = next((item for item in gained if len(item) == 2), None)
        return "concrete condition combination" if decision is None else f"concrete decision {decision[0]} = {decision[1]}"

class _CoverageState:
    """Incrementally track observed coverage and MC/DC-relevant condition patterns."""

    def __init__(self, function):
        self.decisions = {decision.id: decision for decision in function.decisions}
        self.atoms, self.positions, self.patterns, self.pattern_sets = {}, {}, {}, {}
        self.covered, self._outcomes, self._pairs = set(), {}, {}
        for decision in function.decisions:
            atoms = tuple(atoms_of(decision.expr))
            self.atoms[decision.id] = atoms
            self.positions[decision.id] = {id(atom): index for index, atom in enumerate(atoms)}
            self.patterns[decision.id], self.pattern_sets[decision.id] = [], set()

    def add(self, result):
        # Commit both newly covered targets and unseen condition patterns.
        return self._changes(result, True)

    def gained(self, result):
        # Dry-run the same calculation so rows() can judge a candidate first.
        return self._changes(result, False)

    def novel(self, result, decision_ids=None):
        """Return True when execution exposes a condition pattern not seen before.

        A novel pattern is worth retaining even without immediate target coverage,
        because it can later pair with another pattern to establish MC/DC independence.
        """
        selected = None if decision_ids is None else set(decision_ids)
        for decision_id, patterns in result.conditions.items():
            if decision_id in self.pattern_sets and (selected is None or decision_id in selected):
                if any(pattern not in self.pattern_sets[decision_id] for pattern in patterns):
                    return True
        return False

    def _changes(self, result, commit):
        """Calculate newly covered obligations and optionally commit them.

        New condition patterns are compared against all previously committed patterns
        to detect MC/DC pairs. Keeping calculation and commit separate lets the search
        score candidates without corrupting its baseline state.
        """
        gained, additions = set(), {}
        for decision_id, outcomes in result.observed.items():
            for outcome in outcomes:
                if (decision_id, outcome) not in self.covered:
                    gained.add((decision_id, outcome))
        for decision_id, values in result.conditions.items():
            if decision_id not in self.decisions:
                continue
            additions[decision_id] = []
            known = self.pattern_sets[decision_id]
            for pattern in values:
                if pattern not in known and pattern not in additions[decision_id]:
                    additions[decision_id].append(pattern)
        for decision_id, new_patterns in additions.items():
            decision, atoms = self.decisions[decision_id], self.atoms[decision_id]
            patterns = list(self.patterns[decision_id])
            for pattern in new_patterns:
                for atom, outcome in zip(atoms, pattern):
                    if outcome is not None:
                        gained.add((decision_id, atom_text(atom), outcome))
                for index in range(len(atoms)):
                    target = decision_id, "mcdc", index
                    if target in self.covered or target in gained:
                        continue
                    for previous in patterns:
                        if self._independent(decision, index, pattern, previous):
                            gained.add(target)
                            break
                patterns.append(pattern)
        if commit:
            self.covered.update(gained)
            for decision_id, new_patterns in additions.items():
                self.patterns[decision_id].extend(new_patterns)
                self.pattern_sets[decision_id].update(new_patterns)
        return gained

    def _independent(self, decision, index, left, right):
        # Pair checks are repeated heavily while the frontier grows, so cache the
        # symmetric pair by frozenset: (left, right) and (right, left) are equivalent.
        key = decision.id, index, frozenset((left, right))
        if key not in self._pairs:
            left_outcome = self._outcome(decision, left)
            right_outcome = self._outcome(decision, right)
            independent = left_outcome is not None and right_outcome is not None
            independent = independent and left[index] is not None and right[index] is not None
            independent = independent and left_outcome != right_outcome and left[index] != right[index]
            if independent:
                for position in range(len(self.atoms[decision.id])):
                    if position != index and left[position] is not None and right[position] is not None and left[position] != right[position]:
                        independent = False
                        break
            self._pairs[key] = independent
        return self._pairs[key]

    def _outcome(self, decision, pattern):
        # Decision evaluation for a recorded pattern is deterministic and reused by
        # multiple MC/DC pair checks, so cache it per decision and pattern.
        key = decision.id, pattern
        if key not in self._outcomes:
            self._outcomes[key] = CoverageGenerator._outcome(decision.expr, pattern, self.positions[decision.id])
        return self._outcomes[key]
