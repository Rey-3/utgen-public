"""Concrete row expansion for decisions hidden behind local state and loops."""
from __future__ import annotations

import re
from dataclasses import fields, is_dataclass

from utgen.interp import Interpreter
from utgen.model import (And, Atom, Const, Direction, FunctionIR, Index,
                         Inventory, MemberE, Not, Or, Ref, TestRow, Unary,
                         atoms_of)


class CoverageGenerator:
    """Mutate injectable inputs until the statement IR reaches missing outcomes."""

    def __init__(self, max_rounds=4, max_rows=256) -> None:
        self.max_rounds = max_rounds
        self.max_rows = max_rows
        self._function = None
        self._results = {}

    def rows(self, function: FunctionIR, existing: list[TestRow], decision_ids=None) -> list[TestRow]:
        inputs = [leaf for leaf in function.leaves if leaf.direction in (Direction.IN, Direction.INOUT)]
        if not inputs or not function.body:
            return []
        interpreter = Interpreter(Inventory(function.leaves, function.stubs))
        if function is not self._function:
            self._function, self._results = function, {}
        targets = self._targets(function, decision_ids)
        relevant, needs_combinations = self._decision_inputs(function, inputs, decision_ids)
        index_values = self._index_values(function.body)
        queue = [row.values.copy() for row in existing]
        if not queue:
            queue.append(self._defaults(inputs))
        seen, results, rows = set(), [], []
        for values in queue:
            seen.add(self._key(values))
            results.append(self._run(interpreter, function, values))
        covered = self._covered(results, function) & targets
        if targets <= covered:
            return []
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
                        result = self._run(interpreter, function, candidate)
                        gained = (self._covered(results + [result], function) & targets) - covered
                        if not gained:
                            if needs_combinations and leaf.name in relevant and neutral < self.max_rows * 32:
                                queue.append(candidate)
                                neutral += 1
                            continue
                        covered.update(gained)
                        results.append(result)
                        rows.append(TestRow(candidate, "Coverage", self._description(gained)))
                        queue.append(candidate)
                        if len(rows) >= self.max_rows or targets <= covered:
                            return rows
            if not queue:
                break
        return rows

    def _run(self, interpreter, function, values):
        key = self._key(values)
        if key not in self._results:
            self._results[key] = interpreter.run(function, values)
        return self._results[key]

    @staticmethod
    def _targets(function: FunctionIR, decision_ids=None) -> set[tuple]:
        targets = set()
        decision_ids = None if decision_ids is None else set(decision_ids)
        for decision in function.decisions:
            if decision_ids is not None and decision.id not in decision_ids:
                continue
            targets.add((decision.id, False))
            targets.add((decision.id, True))
            for index, atom in enumerate(atoms_of(decision.expr)):
                targets.add((decision.id, atom.text, False))
                targets.add((decision.id, atom.text, True))
                if atom.var is not None:
                    targets.add((decision.id, "mcdc", index))
        return targets

    @staticmethod
    def _covered(results, function: FunctionIR) -> set[tuple]:
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
                        covered.add((decision.id, atom.text, outcome))
            for index in range(len(atoms_of(decision.expr))):
                if CoverageGenerator._independent(decision.expr, index, values):
                    covered.add((decision.id, "mcdc", index))
        return covered

    @staticmethod
    def _independent(expr, index, patterns) -> bool:
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
        values = {}
        for leaf in inputs:
            values[leaf.name] = 1 if leaf.address_of else min(max(0, leaf.ctype.lo), leaf.ctype.hi)
        return values

    @staticmethod
    def _values(leaf, function: FunctionIR, base=None, extra=()) -> tuple:
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
        if decision_ids is None:
            return {leaf.name for leaf in inputs}, False
        selected = set(decision_ids)
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
        symbolic = set(names)
        normalized = {re.sub(r"\[\d+\]", "[*]", path) for path in paths}
        for leaf in inputs:
            if re.sub(r"\[\d+\]", "[*]", leaf.name) in normalized:
                names.add(leaf.name)
        return names, bool(names - symbolic)

    @staticmethod
    def _expression_paths(value, paths) -> None:
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

    @staticmethod
    def _key(values: dict) -> tuple:
        return tuple(sorted(values.items()))

    @staticmethod
    def _description(gained: set[tuple]) -> str:
        decision = next((item for item in gained if len(item) == 2), None)
        return "concrete condition combination" if decision is None else f"concrete decision {decision[0]} = {decision[1]}"
