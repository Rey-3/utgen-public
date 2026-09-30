
from __future__ import annotations

from collections import deque
import re
import time
import warnings
from dataclasses import fields, is_dataclass

from utgen.interp import Interpreter
from utgen.model import (And, Atom, CallE, Const, Direction, FunctionIR, Index,
                         Inventory, MemberE, Not, Or, Ref, TestRow, Unary,
                         Ternary, Unknown, atom_text, atoms_of)


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

        # ponytail: bound heuristic time per decision; raise after measuring missed coverage.
        deadline = time.monotonic() + self.max_seconds
        initial_executions = len(self._results)

        # Prefer symbolic rows created for this target. Rows that reached the target
        # incidentally are also good seeds. Fall back to one neutral complete row.
        seeds, seed_keys = [], set()
        for row, values, result in evidence:
            if not (selected.intersection(row.intent) or selected.intersection(result.observed)):
                continue
            key = self._key(values)
            if key in seed_keys:
                continue
            seed_keys.add(key)
            seeds.append(values)
        if not seeds:
            seeds.append(self._defaults(inputs))

        relevant, combination_inputs = self._decision_inputs(function, inputs, selected)
        expanded = set(selected)
        guards = self._guard_requirements(function, selected)
        generated = []
        emitted = {self._key(row.values) for row in existing}
        path_expanded = False

        while True:
            search_inputs = [leaf for leaf in inputs if leaf.name in relevant]
            search_results = []

            if search_inputs:
                # Freeze each input's representative domain to its symbolic seed.
                # This prevents variable-to-variable probes from walking across the
                # integer range as other inputs are mutated.
                queue = deque((index, values.copy()) for index, values in enumerate(seeds))
                seen = {
                    self._search_key(index, values, search_inputs)
                    for index, values in enumerate(seeds)
                }
                domains = {
                    (index, leaf.name): self._values(
                        leaf, function, seed, index_values, expanded
                    )
                    for index, seed in enumerate(seeds)
                    for leaf in search_inputs
                }

                while queue:
                    seed_index, base = queue.popleft()
                    for leaf in search_inputs:
                        for value in domains[seed_index, leaf.name]:
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
                            useful = bool(gained or novel)
                            if useful:
                                state.add(result)
                                if key not in emitted:
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
                            if useful or leaf.name in combination_inputs:
                                queue.append((seed_index, candidate))

            # The focused target search is exhausted. A reached target can still
            # depend on earlier branches that sanitize or rewrite its operands.
            # Add those control-path inputs only after direct repair was insufficient.
            observed_results = [result for _, _, result in evidence] + search_results
            target_reached = any(
                selected.intersection(result.observed)
                for result in observed_results
            )
            if target_reached and not path_expanded:
                path_expanded = True
                path_inputs = self._path_inputs(function, inputs, selected)
                extra = path_inputs - relevant
                if extra:
                    relevant.update(extra)
                    continue

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
            guard_inputs, guard_combinations = self._decision_inputs(function, inputs, (guard_id,))
            relevant.update(guard_inputs)
            combination_inputs.update(guard_combinations)

    def _run(self, interpreter, function, values, key=None):
        """Execute one normalized input set, caching identical executions."""
        self._set_function(function)
        key = self._key(values) if key is None else key
        if key not in self._results:
            self._results[key] = interpreter.run(function, values)
        return self._results[key]

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
                if atom.var is leaf and isinstance(atom.rhs, int):
                    values.update((atom.rhs - 1, atom.rhs, atom.rhs + 1))
                other = atom.rhs_var if atom.var is leaf else atom.var if atom.rhs_var is leaf else None
                if other is not None and base is not None and isinstance(base.get(other.name), int):
                    value = base[other.name]
                    values.update((value - 1, value, value + 1))
        return tuple(value for value in sorted(values) if leaf.ctype.lo <= value <= leaf.ctype.hi)

    @staticmethod
    def _search_key(seed_index, values, search_inputs) -> tuple:
        """Deduplicate focused states without merging different symbolic seeds."""
        return seed_index, tuple(
            (leaf.name, 0 if values.get(leaf.name) == "NULL" else values.get(leaf.name))
            for leaf in search_inputs
        )

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
    def _decision_inputs(function, inputs, decision_ids) -> tuple[set[str], set[str]]:
        """Find inputs that can influence the selected decisions.

        Direct symbolic variables come from decision atoms. IR expression paths are
        scanned as well so array/member/dereferenced inputs that were not represented
        by Atom.var can still participate in multi-step candidate combinations.
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
        raw_paths, conditions = set(), {}

        def visit(value):
            if is_dataclass(value):
                decision_id = getattr(value, "decision_id", None)
                if decision_id in selected:
                    condition = value.c if isinstance(value, Ternary) else getattr(value, "condition", None)
                    conditions[decision_id] = condition
                for item in fields(value):
                    visit(getattr(value, item.name))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    visit(item)

        visit(function.body)
        dataflow_paths = set()
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
                        function, conditions.get(decision_id), raw_paths, bindings,
                    )
                    CoverageGenerator._dataflow_paths(
                        function, state.condition, dataflow_paths, bindings,
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
        return names, combination_inputs

    @staticmethod
    def _path_inputs(function, inputs, decision_ids) -> set[str]:
        """Find inputs in path constraints leading to the selected decisions."""
        if function.dataflow is None:
            return set()
        paths = set()
        for decision_id in set(decision_ids):
            try:
                point = function.dataflow.point(decision_id)
            except KeyError:
                continue
            for state in point.states:
                for constraint in state.path:
                    CoverageGenerator._dataflow_paths(
                        function, constraint.expr, paths,
                    )
        normalized = {
            re.sub(r"\[\d+\]", "[*]", path) for path in paths
        }
        out = set()
        for leaf in inputs:
            leaf_name = re.sub(r"\[\d+\]", "[*]", leaf.name)
            if leaf_name in normalized or any(
                leaf_name.startswith(path + ".")
                or leaf_name.startswith(path + "[")
                for path in normalized
            ):
                out.add(leaf.name)
        return out

    @staticmethod
    def _dataflow_paths(function, value, paths, bindings=None, seen=()) -> None:
        """Collect concrete caller inputs from a substituted dataflow expression."""
        if isinstance(value, CallE):
            helper = function.local_functions.get(value.name)
            if helper is not None and ("call", helper.name) not in seen:
                helper_seen = seen + (("call", helper.name),)
                used = set()
                # ponytail: conservative body scan until dataflow preserves input snapshots.
                CoverageGenerator._dataflow_paths(helper, helper.body, used, seen=helper_seen)
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
                            base = CoverageGenerator._expression_path(value.args[index])
                            if base:
                                CoverageGenerator._dataflow_paths(
                                    function, Ref(base + leaf.name[len(name):]), paths, bindings, helper_seen,
                                )
                                mapped = True
                            elif leaf.name == name:
                                CoverageGenerator._dataflow_paths(
                                    function, value.args[index], paths, bindings, helper_seen,
                                )
                                mapped = True
                        break
                    if not matched_param:
                        CoverageGenerator._dataflow_paths(function, Ref(leaf.name), paths, bindings, helper_seen)
                        mapped = True
                if mapped:
                    return

        path = CoverageGenerator._expression_path(value)
        if path:
            # Resolve locals and aggregate copies from the existing dataflow bindings.
            # Unknown values still carry dependency names; they are not treated as known values.
            key, suffix = path, ""
            while bindings and key:
                if key in bindings and key not in seen:
                    bound = bindings[key]
                    base = CoverageGenerator._expression_path(bound)
                    if base != key:
                        expression = Ref(base + suffix) if base and suffix else bound
                        CoverageGenerator._dataflow_paths(function, expression, paths, bindings, seen + (key,))
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
                    function, getattr(value, item.name), paths, bindings, seen,
                )
        elif isinstance(value, (tuple, list)):
            for item in value:
                CoverageGenerator._dataflow_paths(function, item, paths, bindings, seen)

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
        key = []
        for leaf in self._inputs:
            value = values.get(leaf.name, 1 if leaf.address_of else 0)
            key.append((leaf.name, 0 if value == "NULL" else value))
        return tuple(key)

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
