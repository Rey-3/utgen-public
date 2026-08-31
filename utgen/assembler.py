from __future__ import annotations

from itertools import product

from utgen.mcdc import McdcGenerator, MaskingRecipes, eval_expr
from utgen.model import Atom, Direction, FunctionIR, Inventory, TestRow, atoms_of
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

        inputs = self._inputs(function)
        # Every generated row starts from a complete, valid input set. The solver only
        # replaces variables that are needed to satisfy the current decision constraints.
        defaults = self._defaults(inputs)
        if not function.decisions:
            return [TestRow(defaults, "Nominal", "nominal path")]

        rows = []
        # Different coverage goals can resolve to exactly the same concrete inputs.
        # Keep one row per input set and merge the coverage intent into that row.
        by_values = {}

        from utgen.coverage import CoverageGenerator

        concrete = CoverageGenerator()
        for decision in function.decisions:
            # A nested decision is only reachable when all of its enclosing guards have
            # the required outcomes. _guards() expands those guards into compatible atom recipes.
            guard_assignments = self._guards(decision)
            if not guard_assignments:
                continue

            # MC/DC works with boolean atom outcomes first. The solver below translates
            # those logical True/False assignments into actual C input values.
            assignments = self.mcdc.rows(decision.expr)
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
                    obligations = self._obligations(function, decision, outcome)
                    if existing is not None:
                        existing.description += f"; decision {decision.id} = {outcome}"
                        existing.intent.update(intent)
                        existing.obligation_keys = self._merge_obligations(existing.obligation_keys, obligations)
                        break

                    kind = "Switch" if decision.kind == "case" else "MC/DC"
                    row = TestRow(values, kind, f"decision {decision.id} = {outcome}", intent,
                                  obligation_keys=obligations)
                    by_values[key] = row
                    rows.append(row)
                    break

            # Symbolic MC/DC recipes do not always guarantee that the concrete execution
            # reaches every expected outcome. Execute/search around the current rows and
            # add only concrete rows that close remaining coverage gaps for this decision.
            for coverage in concrete.rows(function, rows, (decision.id,)):
                key = tuple(coverage.values.items())
                if key not in by_values:
                    by_values[key] = coverage
                    rows.append(coverage)
                else:
                    existing = by_values[key]
                    existing.obligation_keys = self._merge_obligations(
                        existing.obligation_keys, coverage.obligation_keys
                    )

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
                    existing.obligation_keys = self._merge_obligations(
                        existing.obligation_keys, boundary.obligation_keys
                    )
                    continue
                by_values[key] = boundary
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
                    existing.obligation_keys = self._merge_obligations(
                        existing.obligation_keys, fault.obligation_keys
                    )
                    continue
                by_values[key] = fault
                rows.append(fault)
        return rows

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
    def _obligations(function: FunctionIR, decision, outcome: bool) -> tuple[str, ...]:
        """Translate legacy integer intent into stable objective identities.

        The legacy ``intent`` map remains untouched for compatibility with existing
        emitters.  Structured provenance is an additive field and is merged as a set
        whenever concrete inputs deduplicate across decision/boundary generators.
        """

        from utgen.iteration.fingerprint import decision_key_for, obligation_key

        # A symbolic row is specifically generated for the selected decision outcome;
        # do not attach the opposite branch merely because both are present in the
        # decision's complete obligation catalogue.  Resolve through the containing
        # function so repeated identical decisions receive distinct occurrence keys.
        selected = {
            obligation_key(
                "branch",
                decision_key_value=decision_key_for(function, decision),
                outcome=outcome,
            )
        }
        for guard in decision.guards:
            for candidate in function.decisions:
                if candidate.expr is guard.expr or candidate.expr == guard.expr:
                    selected.add(obligation_key(
                        "branch",
                        decision_key_value=decision_key_for(function, candidate),
                        outcome=guard.outcome,
                    ))
                    break
        return tuple(sorted(selected))

    @staticmethod
    def _merge_obligations(*values) -> tuple[str, ...]:
        from utgen.iteration.fingerprint import merge_obligation_keys

        return merge_obligation_keys(*values)
