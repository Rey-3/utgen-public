"""Concrete row expansion for decisions hidden behind local state and loops."""
from __future__ import annotations

from utgen.interp import Interpreter
from utgen.model import Direction, FunctionIR, Inventory, TestRow, atoms_of


class CoverageGenerator:
    """Mutate injectable inputs until the statement IR reaches missing outcomes."""

    def __init__(self, max_rounds=4, max_rows=256) -> None:
        self.max_rounds = max_rounds
        self.max_rows = max_rows

    def rows(self, function: FunctionIR, existing: list[TestRow]) -> list[TestRow]:
        inputs = [leaf for leaf in function.leaves if leaf.direction in (Direction.IN, Direction.INOUT)]
        if not inputs or not function.body:
            return []
        interpreter = Interpreter(Inventory(function.leaves, function.stubs))
        targets = self._targets(function)
        queue = [row.values.copy() for row in existing]
        if not queue:
            queue.append(self._defaults(inputs))
        seen, covered, rows = set(), set(), []
        for values in queue:
            seen.add(self._key(values))
            covered.update(self._covered(interpreter.run(function, values), function))
        if targets <= covered:
            return []
        for _ in range(self.max_rounds):
            frontier = list(queue)
            queue = []
            for base in frontier:
                for leaf in inputs:
                    for value in self._values(leaf, function):
                        if value == base.get(leaf.name):
                            continue
                        candidate = base | {leaf.name: value}
                        key = self._key(candidate)
                        if key in seen:
                            continue
                        seen.add(key)
                        gained = self._covered(interpreter.run(function, candidate), function) - covered
                        if not gained:
                            continue
                        covered.update(gained)
                        rows.append(TestRow(candidate, "Coverage", self._description(gained)))
                        queue.append(candidate)
                        if len(rows) >= self.max_rows or targets <= covered:
                            return rows
            if not queue:
                break
        return rows

    @staticmethod
    def _targets(function: FunctionIR) -> set[tuple]:
        targets = set()
        for decision in function.decisions:
            targets.add((decision.id, False))
            targets.add((decision.id, True))
            for atom in atoms_of(decision.expr):
                targets.add((decision.id, atom.text, False))
                targets.add((decision.id, atom.text, True))
        return targets

    @staticmethod
    def _covered(result, function: FunctionIR) -> set[tuple]:
        covered = set()
        for decision_id, outcomes in result.observed.items():
            for outcome in outcomes:
                covered.add((decision_id, outcome))
        for decision in function.decisions:
            patterns = result.conditions.get(decision.id, ())
            for pattern in patterns:
                for atom, outcome in zip(atoms_of(decision.expr), pattern):
                    covered.add((decision.id, atom.text, outcome))
        return covered

    @staticmethod
    def _defaults(inputs) -> dict:
        values = {}
        for leaf in inputs:
            values[leaf.name] = min(max(0, leaf.ctype.lo), leaf.ctype.hi)
        return values

    @staticmethod
    def _values(leaf, function: FunctionIR) -> tuple:
        values = {leaf.ctype.lo, leaf.ctype.hi, 0, 1, 2, 3}
        for decision in function.decisions:
            for atom in atoms_of(decision.expr):
                if atom.var is not leaf or not isinstance(atom.rhs, int):
                    continue
                values.update((atom.rhs - 1, atom.rhs, atom.rhs + 1))
        return tuple(value for value in sorted(values) if leaf.ctype.lo <= value <= leaf.ctype.hi)

    @staticmethod
    def _key(values: dict) -> tuple:
        return tuple(sorted(values.items()))

    @staticmethod
    def _description(gained: set[tuple]) -> str:
        decision = next((item for item in gained if len(item) == 2), None)
        return "concrete condition combination" if decision is None else f"concrete decision {decision[0]} = {decision[1]}"
