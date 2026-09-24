
from __future__ import annotations

import re
from dataclasses import fields, is_dataclass

from utgen.interp import Interpreter
from utgen.model import (And, Atom, Const, Direction, FunctionIR, Index,
                         Inventory, MemberE, Not, Or, Ref, TestRow, Unary,
                         atom_text, atoms_of)


class CoverageGenerator:
    """Generate extra concrete test rows to close remaining coverage gaps.

    The generator starts from already-created rows, executes them through the
    interpreter, then mutates input values to discover branch/condition outcomes
    and MC/DC pairs that symbolic generation did not reach.
    """

    def __init__(self, max_rounds=4, max_rows=256) -> None:
        self.max_rounds = max_rounds
        self.max_rows = max_rows
        self._function = None
        self._inputs = ()
        self._results = {}

    def rows(self, function: FunctionIR, existing: list[TestRow], decision_ids=None) -> list[TestRow]:
        """Search concrete input combinations for still-uncovered targets.

        Existing rows are used as the search seeds. A candidate is retained when
        it adds coverage, produces a new condition pattern, or is useful as an
        intermediate state for a later multi-input combination.
        """
        inputs = [leaf for leaf in function.leaves if leaf.direction in (Direction.IN, Direction.INOUT)]
        if not inputs or not function.body:
            return []
        interpreter = Interpreter(Inventory(function.leaves, function.stubs))
        self._set_function(function)
        # Coverage is tracked as explicit obligations: decision outcomes, each
        # condition's True/False outcomes, and MC/DC independence for each atom.
        targets = self._targets(function, decision_ids)

        # Some decision expressions reference values through IR paths rather than
        # directly through Atom.var. Those inputs may need several mutations before
        # the final combination becomes useful, so they are handled separately.
        relevant, needs_combinations = self._decision_inputs(function, inputs, decision_ids)
        index_values = self._index_values(function.body)
        # Reuse solver/MC/DC rows as the initial frontier. Concrete execution can
        # then repair gaps caused by masking, indirect expressions, or IR behaviour.
        queue, seen = [], set()
        for row in existing:
            values = row.values.copy()
            key = self._key(values)
            if key in seen:
                continue
            seen.add(key)
            queue.append(values)
        if not queue:
            queue.append(self._defaults(inputs))
            seen.add(self._key(queue[0]))
        # Build the baseline coverage state from all seed rows before generating
        # anything new. This prevents returning rows for coverage already achieved.
        rows, state = [], self._state(function)
        for values in queue:
            key = self._key(values)
            state.add(self._run(interpreter, function, values, key))
        covered = state.covered & targets
        if targets <= covered:
            return []
        # At least one round per condition may be required because one round changes
        # only one input from a base row. More atoms can therefore require deeper
        # combinations even when max_rounds is configured lower.
        rounds = max(self.max_rounds, max((len(atoms_of(decision.expr)) for decision in function.decisions), default=0))
        neutral = 0
        for _ in range(rounds):
            frontier = list(queue)
            queue = []
            for base in frontier:
                for leaf in inputs:
                    for value in self._values(leaf, function, base, index_values):
                        if value == base.get(leaf.name):
                            continue
                        candidate = base | {leaf.name: value}
                        key = self._key(candidate)
                        if key in seen:
                            continue
                        seen.add(key)
                        result = self._run(interpreter, function, candidate, key)
                        # Probe without committing first. A candidate that immediately
                        # satisfies a target is useful; otherwise a new condition
                        # pattern may still become an MC/DC partner in a later round.
                        gained = (state.gained(result) & targets) - covered
                        if not gained and not state.novel(result, decision_ids):
                            # Keep a bounded number of neutral mutations only when the
                            # selected decision depends on indirect/path-based inputs.
                            # One such mutation may be useless alone but necessary before
                            # changing a second input in the next frontier expansion.
                            if needs_combinations and leaf.name in relevant and neutral < self.max_rows * 32:
                                queue.append(candidate)
                                neutral += 1
                            continue
                        state.add(result)
                        covered.update(gained)
                        rows.append(TestRow(
                            candidate,
                            "Coverage",
                            self._description(gained),
                        ))
                        queue.append(candidate)
                        if len(rows) >= self.max_rows or targets <= covered:
                            return rows
            if not queue:
                break
        return rows

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
    def _state(function):
        return _CoverageState(function)

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
    def _covered(results, function: FunctionIR) -> set[tuple]:
        """Convert interpreter observations into the same keys used by _targets()."""
        if not isinstance(results, (list, tuple)):
            results = [results]
        covered = set()
        patterns = {}
        for result in results:
            for decision_id, outcomes in result.observed.items():
                for outcome in outcomes:
                    covered.add((decision_id, outcome))
            for decision_id, values in result.conditions.items():
                patterns.setdefault(decision_id, set()).update(values)
        for decision in function.decisions:
            values = patterns.get(decision.id, ())
            for pattern in values:
                for atom, outcome in zip(atoms_of(decision.expr), pattern):
                    if outcome is not None:
                        covered.add((decision.id, atom_text(atom), outcome))
            for index in range(len(atoms_of(decision.expr))):
                if CoverageGenerator._independent(decision.expr, index, values):
                    covered.add((decision.id, "mcdc", index))
        return covered

    @staticmethod
    def _independent(expr, index, patterns) -> bool:
        """Check whether one condition independently changes the decision result.

        Two observed patterns form an MC/DC pair when the target condition and the
        overall decision both flip while every other known condition stays equal.
        ``None`` is treated as unknown/masked and therefore does not disqualify a pair.
        """
        atoms = atoms_of(expr)
        positions = {id(atom): position for position, atom in enumerate(atoms)}
        observed = []
        for pattern in patterns:
            outcome = CoverageGenerator._outcome(expr, pattern, positions)
            if outcome is not None and pattern[index] is not None:
                observed.append((outcome, pattern))
        for outcome, left in observed:
            for other_outcome, right in observed:
                if outcome == other_outcome or left[index] == right[index]:
                    continue
                if all(position == index or left[position] is None or right[position] is None
                       or left[position] == right[position] for position in range(len(atoms))):
                    return True
        return False

    @staticmethod
    def _outcome(expr, pattern, positions):
        """Evaluate a boolean IR expression from a recorded condition pattern.

        Returning None preserves short-circuit/masked information instead of
        inventing an outcome for a condition the interpreter did not observe.
        """
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
    def _values(leaf, function: FunctionIR, base=None, extra=()) -> tuple:
        """Build high-value mutations instead of scanning the full C domain.

        Besides type extremes and small nominal values, the search probes one step
        below/at/above decision thresholds. For variable-to-variable comparisons it
        also probes around the other variable's current concrete value.
        """
        values = {leaf.ctype.lo, leaf.ctype.hi, 0, 1, 2, 3, *extra}
        for decision in function.decisions:
            for atom in atoms_of(decision.expr):
                if atom.var is leaf and isinstance(atom.rhs, int):
                    values.update((atom.rhs - 1, atom.rhs, atom.rhs + 1))
                other = atom.rhs_var if atom.var is leaf else atom.var if atom.rhs_var is leaf else None
                if other is not None and base is not None and isinstance(base.get(other.name), int):
                    value = base[other.name]
                    values.update((value - 1, value, value + 1))
        return tuple(value for value in sorted(values) if leaf.ctype.lo <= value <= leaf.ctype.hi)

    @staticmethod
    def _decision_inputs(function, inputs, decision_ids) -> tuple[set[str], bool]:
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
        paths = set()

        def visit(value):
            if is_dataclass(value):
                decision_id = getattr(value, "decision_id", None)
                if decision_id in selected:
                    CoverageGenerator._expression_paths(getattr(value, "condition", None), paths)
                for item in fields(value):
                    visit(getattr(value, item.name))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    visit(item)

        visit(function.body)
        # Preserve the direct symbolic set before adding path-derived leaves. The
        # difference tells rows() whether neutral intermediate combinations are needed.
        symbolic = set(names)
        normalized = {re.sub(r"\[\d+\]", "[*]", path) for path in paths}
        for leaf in inputs:
            if re.sub(r"\[\d+\]", "[*]", leaf.name) in normalized:
                names.add(leaf.name)
        return names, bool(names - symbolic)

    @staticmethod
    def _expression_paths(value, paths) -> None:
        # Recursively collect lvalue-like paths from the IR condition tree.
        path = CoverageGenerator._expression_path(value)
        if path:
            paths.add(path)
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
            return value.path
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
