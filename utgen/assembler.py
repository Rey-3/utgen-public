"""MC/DC row assembly under decision guards."""
from __future__ import annotations

from itertools import product

from utgen.mcdc import McdcGenerator, MaskingRecipes, eval_expr
from utgen.model import Atom, Direction, FunctionIR, Inventory, TestRow, atoms_of
from utgen.solver import AtomConstraints, Solver


class RowAssembler:
    """Combine MC/DC recipes, guards, and solver results into test rows."""

    def __init__(self, solver: Solver, mcdc: McdcGenerator, fault_overrides: dict | None = None,
                 include_boundary=True, include_fault=True, coverage_max_candidates=2048) -> None:
        self.solver = solver
        self.mcdc = mcdc
        self.fault_overrides = fault_overrides or {}
        self.include_boundary = include_boundary
        self.include_fault = include_fault
        self.coverage_max_candidates = coverage_max_candidates

    def assemble(self, function: FunctionIR) -> tuple[list[TestRow], list[str]]:
        """Return deterministic rows and notes for one function's decisions."""
        inputs = self._inputs(function)
        defaults = self._defaults(inputs)
        if not function.decisions:
            return [TestRow(defaults, "Nominal", "nominal path")], []
        rows = []
        notes = []
        by_values = {}
        from utgen.coverage import CoverageGenerator

        concrete = CoverageGenerator(max_candidates=self.coverage_max_candidates)
        reported_exhaustion = False
        for decision in function.decisions:
            if any(atom.var is None for atom in atoms_of(decision.expr)):
                notes.append(f"decision {decision.id}: uncontrollable or degenerate condition")
            guard_assignments = self._guards(decision)
            if not guard_assignments:
                notes.append(f"decision {decision.id}: infeasible guard stack")
                continue
            assignments = self.mcdc.rows(decision.expr)
            for assignment in assignments:
                emitted = False
                for guards in guard_assignments:
                    values = self.solver.solve(self._constraints(guards, assignment))
                    if values is None:
                        continue
                    emitted = True
                    values = defaults | values
                    key = tuple(values.items())
                    outcome = eval_expr(decision.expr, assignment)
                    intent = self._intent(function, decision, outcome)
                    existing = by_values.get(key)
                    if existing is not None:
                        existing.description += f"; decision {decision.id} = {outcome}"
                        existing.intent.update(intent)
                        break
                    kind = "Switch" if decision.kind == "case" else "MC/DC"
                    row = TestRow(values, kind, f"decision {decision.id} = {outcome}", intent)
                    by_values[key] = row
                    rows.append(row)
                    break
                if not emitted:
                    notes.append(f"decision {decision.id}: infeasible MC/DC row")
            if not assignments:
                notes.append(f"decision {decision.id}: no feasible MC/DC pair")
            for coverage in concrete.rows(function, rows, (decision.id,)):
                key = tuple(coverage.values.items())
                if key not in by_values:
                    by_values[key] = coverage
                    rows.append(coverage)
            if concrete.exhausted and not reported_exhaustion:
                notes.append(f"decision {decision.id}: concrete coverage search budget exhausted")
                reported_exhaustion = True
        if self.include_boundary:
            from utgen.boundary import BoundaryGenerator

            for boundary in BoundaryGenerator(self.solver).rows(function):
                key = tuple(boundary.values.items())
                existing = by_values.get(key)
                if existing is not None:
                    existing.description += f"; {boundary.description}"
                    existing.intent.update(boundary.intent)
                    continue
                by_values[key] = boundary
                rows.append(boundary)
        if self.include_fault:
            from utgen.fault import FaultGenerator

            inventory = Inventory(function.leaves, function.stubs)
            for fault in FaultGenerator(self.fault_overrides).rows(inventory):
                key = tuple(fault.values.items())
                existing = by_values.get(key)
                if existing is not None:
                    existing.description += f"; {fault.description}"
                    continue
                by_values[key] = fault
                rows.append(fault)
        return rows, notes

    @staticmethod
    def _inputs(function: FunctionIR) -> list:
        out = []
        for leaf in function.leaves:
            if leaf.direction in (Direction.IN, Direction.INOUT):
                out.append(leaf)
        return out

    @staticmethod
    def _defaults(inputs: list) -> dict:
        out = {}
        for leaf in inputs:
            out[leaf.name] = 1 if leaf.address_of else min(max(0, leaf.ctype.lo), leaf.ctype.hi)
        return out

    @staticmethod
    def _guards(decision) -> list:
        out = [{}]
        for guard in decision.guards:
            merged = []
            for current in out:
                for recipe in RowAssembler._recipes(guard.expr, guard.outcome):
                    candidate = current | recipe
                    if all(current.get(atom, outcome) == outcome for atom, outcome in recipe.items()):
                        merged.append(candidate)
                        if len(merged) == 32:
                            break
                if len(merged) == 32:
                    break
            out = merged
        return out

    @staticmethod
    def _recipes(expr, outcome: bool) -> list:
        canonical = MaskingRecipes.make(expr, outcome)
        atoms = []
        for atom in atoms_of(expr):
            if atom not in atoms:
                atoms.append(atom)
        if len(atoms) > 12:
            return [canonical]
        out = [canonical]
        seen = {frozenset(canonical.items())}
        for values in product((False, True), repeat=len(atoms)):
            recipe = {}
            for atom, value in zip(atoms, values):
                recipe[atom] = value
            if eval_expr(expr, recipe) != outcome or frozenset(recipe.items()) in seen:
                continue
            seen.add(frozenset(recipe.items()))
            out.append(recipe)
            if len(out) == 16:
                break
        return out

    @staticmethod
    def _constraints(*assignments: dict) -> list:
        out = []
        for assignment in assignments:
            for atom, outcome in assignment.items():
                if isinstance(atom, Atom):
                    out.extend(AtomConstraints.for_outcome(atom, outcome))
        return out

    @staticmethod
    def _intent(function: FunctionIR, decision, outcome: bool) -> dict:
        out = {decision.id: outcome}
        for guard in decision.guards:
            for candidate in function.decisions:
                if candidate.expr == guard.expr:
                    out[candidate.id] = guard.outcome
                    break
        return out
