
from __future__ import annotations

from utgen.model import Atom, Constraint, FunctionIR, Op, TestRow, atoms_of
from utgen.solver import Solver


RELATIONAL = {Op.EQ, Op.NE, Op.LT, Op.LE, Op.GT, Op.GE}


class BoundaryGenerator:


    def __init__(self, solver: Solver) -> None:
        self.solver = solver

    def rows(self, function: FunctionIR) -> list[TestRow]:

        from utgen.assembler import RowAssembler

        defaults = RowAssembler._defaults(RowAssembler._inputs(function))
        rows = []
        by_values = {}
        for decision in function.decisions:
            guards = RowAssembler._guards(decision)
            for atom in atoms_of(decision.expr):
                if not self._boundary_atom(atom):
                    continue
                for point in self._points(atom):
                    for recipe in guards:
                        constraints = RowAssembler._constraints(recipe)
                        constraints.append(Constraint(atom.var, Op.EQ, point))
                        if atom.rhs_var is not None:
                            midpoint = (atom.rhs_var.ctype.lo + atom.rhs_var.ctype.hi) // 2
                            constraints.append(Constraint(atom.rhs_var, Op.EQ, midpoint))
                        values = self.solver.solve(constraints)
                        if values is None:
                            continue
                        values = defaults | values
                        row = TestRow(
                            values,
                            "Boundary",
                            self._description(atom, point),
                            self._guard_intent(function, decision),
                            target_decision_id=decision.id,
                        )
                        self._append(rows, by_values, row)
                        break
        return rows

    @staticmethod
    def _boundary_atom(atom) -> bool:
        return isinstance(atom, Atom) and atom.var is not None and atom.op in RELATIONAL and (atom.rhs is not None or atom.rhs_var is not None)

    @staticmethod
    def _points(atom: Atom) -> list[int]:
        base = atom.rhs
        if atom.rhs_var is not None:
            base = (atom.rhs_var.ctype.lo + atom.rhs_var.ctype.hi) // 2
        out = []
        for offset in (-1, 0, 1):
            point = base + offset
            if atom.var.ctype.lo <= point <= atom.var.ctype.hi and point not in out:
                out.append(point)
        return out

    @staticmethod
    def _description(atom: Atom, point: int) -> str:
        return f"boundary {atom.text or atom.var.name}: {point}"

    @staticmethod
    def _guard_intent(function: FunctionIR, decision) -> dict:
        out = {}
        for guard in decision.guards:
            for candidate in function.decisions:
                if candidate.expr is guard.expr or candidate.expr == guard.expr:
                    out[candidate.id] = guard.outcome
                    break
        return out

    @staticmethod
    def _append(rows: list, by_values: dict, row: TestRow) -> None:
        key = tuple(row.values.items())
        existing = by_values.get(key)
        if existing is not None:
            existing.description += f"; {row.description}"
            existing.intent.update(row.intent)
            return
        by_values[key] = row
        rows.append(row)
