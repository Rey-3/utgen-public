from __future__ import annotations

from itertools import product

from utgen.mcdc import McdcGenerator, MaskingRecipes, eval_expr
from utgen.model import (
    AssemblyResult,
    Atom,
    Direction,
    FunctionIR,
    GeneratedRowProvenance,
    Inventory,
    RecipeProvenance,
    TestRow,
    atom_text,
    atoms_of,
)
from utgen.solver import AtomConstraints, Solver


class RowAssembler:
    """Build the final test rows from symbolic decision coverage requirements."""

    def __init__(self, solver: Solver, mcdc: McdcGenerator, fault_overrides: dict | None = None,
                 include_boundary=True, include_fault=True) -> None:
        self.solver = solver
        self.mcdc = mcdc
        self.fault_overrides = fault_overrides or {}
        self.include_boundary = include_boundary
        self.include_fault = include_fault

    def assemble(self, function: FunctionIR) -> list[TestRow]:
        """Convert MC/DC, coverage, boundary and fault requirements into concrete input rows."""

        return self._assemble(function)[0]

    def assemble_with_provenance(self, function: FunctionIR) -> AssemblyResult:
        """Return generated rows together with their read-only recipe mapping."""

        rows, recipes, generated_rows = self._assemble(function, collect_provenance=True)
        return AssemblyResult(tuple(rows), tuple(recipes), tuple(generated_rows))

    def _assemble(self, function: FunctionIR, *, collect_provenance=False):
        """Build rows once, optionally collecting logical-to-concrete mappings."""

        from utgen.stubs import prepare_snapshot_callbacks

        prepare_snapshot_callbacks(function)
        inputs = self._inputs(function)
        # Every generated row starts from a complete, valid input set. The solver only
        # replaces variables that are needed to satisfy the current decision constraints.
        defaults = self._defaults(inputs)
        if not function.decisions:
            row = TestRow(defaults, "Nominal", "nominal path")
            return ([row], [], [GeneratedRowProvenance("Nominal", 0, 0)]) if collect_provenance else ([row], [], [])

        rows = []
        recipes = []
        generated_rows = []
        # Different coverage goals can resolve to exactly the same concrete inputs.
        # Keep one row per input set and merge the coverage intent into that row.
        by_values = {}
        row_indices = {}
        seen_recipes = set()

        from utgen.coverage import CoverageGenerator

        concrete = CoverageGenerator()
        deferred_cases = []
        repair_decisions = []
        for decision_index, decision in enumerate(function.decisions):
            # A nested decision is only reachable when all of its enclosing guards have
            # the required outcomes. _guards() expands those guards into compatible atom recipes.
            guard_assignments = self._guards(decision)
            if not guard_assignments:
                continue

            # MC/DC works with boolean atom outcomes first. The solver below translates
            # those logical True/False assignments into actual C input values.
            assignments = self.mcdc.rows(decision.expr)
            if decision.kind == "case":
                deferred_cases.append(decision.id)
                assignments = [assignment for assignment in assignments
                               if eval_expr(decision.expr, assignment)]
            for assignment in assignments:
                for guards in guard_assignments:
                    values = self.solver.solve(self._constraints(guards, assignment))
                    if values is None:
                        # Variable-to-variable relations normally prefer midpoint values.
                        # Retry without that preference when the midpoint makes the combined
                        # guard + MC/DC constraints unsatisfiable.
                        values = self.solver.solve(
                            self._constraints(guards, assignment, midpoint_relations=False)
                        )
                    if values is None:
                        continue

                    values = defaults | values
                    key = tuple(values.items())
                    outcome = eval_expr(decision.expr, assignment)
                    intent = self._intent(function, decision, outcome)
                    existing = by_values.get(key)
                    if existing is not None:
                        existing.description += f"; decision {decision.id} = {outcome}"
                        existing.intent.update(intent)
                        concrete_row_index = row_indices[key] if collect_provenance else None
                        if collect_provenance:
                            self._append_recipe(
                                recipes, seen_recipes, decision_index, decision,
                                assignment, outcome, concrete_row_index,
                            )
                        break

                    kind = "Switch" if decision.kind == "case" else "MC/DC"
                    row = TestRow(values, kind, f"decision {decision.id} = {outcome}", intent,
                                  target_decision_id=decision.id)
                    by_values[key] = row
                    row_indices[key] = len(rows)
                    rows.append(row)
                    if collect_provenance:
                        self._append_recipe(
                            recipes, seen_recipes, decision_index, decision,
                            assignment, outcome, row_indices[key],
                        )
                    break

            # Sibling switch arms naturally provide each other's false outcomes. Defer
            # case repair until all arms exist so those rows stay grouped by source arm.
            if decision.kind == "case":
                continue
            for coverage in concrete.rows(function, rows, (decision.id,)):
                key = tuple(coverage.values.items())
                if key not in by_values:
                    by_values[key] = coverage
                    row_indices[key] = len(rows)
                    rows.append(coverage)
            repair_decisions.append(decision.id)

        if deferred_cases:
            for coverage in concrete.rows(function, rows, deferred_cases):
                key = tuple(coverage.values.items())
                if key not in by_values:
                    by_values[key] = coverage
                    row_indices[key] = len(rows)
                    rows.append(coverage)

        # A normal loop exit supplies valid later-iteration seeds for body repair.
        for coverage in concrete.rows(function, rows, ()):
            key = tuple(coverage.values.items())
            if key not in by_values:
                by_values[key] = coverage
                row_indices[key] = len(rows)
                rows.append(coverage)

        # Retry unfinished searches with cached executions and later seeds, even
        # when the first pass exhausted its budget without emitting a new row.
        for decision_id in repair_decisions:
            for coverage in concrete.rows(function, rows, (decision_id,)):
                key = tuple(coverage.values.items())
                if key not in by_values:
                    by_values[key] = coverage
                    row_indices[key] = len(rows)
                    rows.append(coverage)

        if self.include_boundary:
            # Boundary rows exercise values immediately below, at and above relational
            # thresholds while preserving the guards required to reach the decision.
            from utgen.boundary import BoundaryGenerator

            for boundary in BoundaryGenerator(self.solver).rows(function):
                key = tuple(boundary.values.items())
                existing = by_values.get(key)
                if existing is not None:
                    existing.description += f"; {boundary.description}"
                    existing.intent.update(boundary.intent)
                    continue
                by_values[key] = boundary
                row_indices[key] = len(rows)
                rows.append(boundary)

        if self.include_fault:
            # Fault rows deliberately inject abnormal input values (for example NULL
            # pointers, enum overflow, or numeric extremes) and share the same dedup path.
            from utgen.fault import FaultGenerator

            inventory = Inventory(function.leaves, function.stubs)
            for fault in FaultGenerator(self.fault_overrides).rows(inventory):
                key = tuple(fault.values.items())
                existing = by_values.get(key)
                if existing is not None:
                    existing.description += f"; {fault.description}"
                    continue
                by_values[key] = fault
                row_indices[key] = len(rows)
                rows.append(fault)
        if collect_provenance:
            linked = {item.concrete_row_index for item in recipes}
            linked.update(item.concrete_row_index for item in generated_rows)
            source_orders = {}
            for index, row in enumerate(rows):
                if index in linked:
                    continue
                order = source_orders.get(row.kind, 0)
                generated_rows.append(GeneratedRowProvenance(row.kind, order, index))
                source_orders[row.kind] = order + 1
        return (rows, recipes, generated_rows)

    @staticmethod
    def _inputs(function: FunctionIR) -> list:
        out = []
        for leaf in function.leaves:
            if leaf.direction in (Direction.IN, Direction.INOUT):
                out.append(leaf)
        return out

    @staticmethod
    def _defaults(inputs: list) -> dict:
        """Create neutral values for inputs that are not constrained by a test objective."""

        out = {}
        for leaf in inputs:
            # Address-control leaves use 1 so pointers are non-NULL by default. Normal
            # values prefer zero, clamped into the legal range of the C type.
            out[leaf.name] = 1 if leaf.address_of else min(max(0, leaf.ctype.lo), leaf.ctype.hi)
        return out

    @staticmethod
    def _guards(decision) -> list:
        """Build compatible truth assignments that make every enclosing guard reachable."""

        out = [{}]
        for guard in decision.guards:
            merged = []
            for current in out:
                for recipe in RowAssembler._recipes(guard.expr, guard.outcome):
                    candidate = current | recipe
                    # Reject recipes that require an atom to be both True and False after
                    # combining multiple nesting guards.
                    if all(current.get(atom, outcome) == outcome for atom, outcome in recipe.items()):
                        merged.append(candidate)
                        # Guard alternatives grow exponentially for nested boolean expressions.
                        # A fixed cap keeps row generation bounded without losing the first
                        # useful recipes, including the canonical masking recipe.
                        if len(merged) == 32:
                            break
                if len(merged) == 32:
                    break
            out = merged
        return out

    @staticmethod
    def _recipes(expr, outcome: bool) -> list:
        """Return bounded atom assignments that force an expression to the requested outcome."""

        # Start with the masking recipe because it gives a compact deterministic assignment.
        canonical = MaskingRecipes.make(expr, outcome)
        if canonical is None:
            return []

        atoms = []
        for atom in atoms_of(expr):
            if atom not in atoms:
                atoms.append(atom)

        # Exhaustively enumerating boolean combinations is 2^N. For large expressions,
        # use only the canonical recipe to avoid an explosion in generation time.
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
            # Extra recipes improve the chance that one remains solvable with outer guards,
            # but keeping all valid combinations would unnecessarily multiply solver work.
            if len(out) == 16:
                break
        return out

    @staticmethod
    def _constraints(*assignments: dict, midpoint_relations=True) -> list:
        """Translate boolean atom outcomes into numeric constraints understood by Solver."""

        out = []
        for assignment in assignments:
            for atom, outcome in assignment.items():
                if isinstance(atom, Atom):
                    out.extend(AtomConstraints.for_outcome(atom, outcome, midpoint_relations))
        return out

    @staticmethod
    def _intent(function: FunctionIR, decision, outcome: bool) -> dict:
        """Record which target and guard decision outcomes a generated row is meant to cover."""

        out = {decision.id: outcome}
        for guard in decision.guards:
            for candidate in function.decisions:
                if candidate.expr is guard.expr or candidate.expr == guard.expr:
                    out[candidate.id] = guard.outcome
                    break
        return out

    @staticmethod
    def _append_recipe(
        recipes: list[RecipeProvenance], seen: set, decision_index: int,
        decision, assignment: dict, outcome: bool, concrete_row_index: int,
    ) -> None:
        atoms = tuple(atoms_of(decision.expr))
        atom_texts = tuple(atom_text(atom) for atom in atoms)
        if outcome is None or any(atom not in assignment for atom in atoms):
            return
        atom_values = tuple(bool(assignment[atom]) for atom in atoms)
        key = decision_index, atom_values, bool(outcome)
        if key in seen:
            return
        seen.add(key)
        recipes.append(
            RecipeProvenance(
                decision_index,
                decision.id,
                atom_texts,
                atom_values,
                bool(outcome),
                concrete_row_index,
            )
        )
