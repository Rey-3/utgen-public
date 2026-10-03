"""Generate abstract boolean assignments used for MC/DC test generation.

This module works with ``Atom -> bool`` assignments. It determines which
conditions must be True/False to prove condition independence; the solver
later converts those requirements into concrete C input values.
"""

from itertools import product

from utgen.model import And, Atom, Not, Op, Or, atoms_of


def eval_expr(node, assign) -> bool | None:
    """Evaluate a decision using an ``Atom -> bool`` assignment.

    This is used to verify that an MC/DC candidate changes the result of the
    complete decision, not only the target condition.
    """

    if isinstance(node, Atom):
        return bool(assign[node])
    if isinstance(node, Not):
        value = eval_expr(node.child, assign)
        return None if value is None else not value
    # Keep short-circuit behaviour: masking recipes may intentionally leave
    # conditions unassigned once an AND/OR result is already determined.
    if isinstance(node, And):
        for child in node.children:
            value = eval_expr(child, assign)
            if value is None:
                return None
            if not value:
                return False
        return True
    if isinstance(node, Or):
        for child in node.children:
            value = eval_expr(child, assign)
            if value is None:
                return None
            if value:
                return True
        return False
    return None


def _merge(*assignments):
    """Merge partial condition assignments and reject contradictions."""

    out = {}
    for assignment in assignments:
        if assignment is None:
            return None
        for atom, outcome in assignment.items():
            # A candidate is invalid if the same condition is required to be
            # both True and False in the surrounding MC/DC context.
            if atom in out and out[atom] != outcome:
                return None
            out[atom] = outcome
    return out


class MaskingRecipes:
    """Construct condition assignments that force a subtree to an outcome.

    ``make`` fully specifies a canonical recipe. ``masked`` keeps only the
    conditions still needed after short-circuiting, which matters when several
    conditions are derived from the same concrete C variable.
    """

    @classmethod
    def make(cls, node, outcome) -> dict | None:
        """Build a canonical assignment that makes ``node`` equal ``outcome``."""

        if isinstance(node, Atom):
            return {node: outcome}
        if isinstance(node, Not):
            return cls.make(node.child, not outcome)
        if isinstance(node, And):
            values = []
            for index, child in enumerate(node.children):
                # AND=True needs every child True. For AND=False, make the first
                # child decisive (False) and keep later siblings neutral (True).
                values.append(cls.make(child, outcome or index != 0))
            return cls._combine(values)
        if isinstance(node, Or):
            values = []
            for index, child in enumerate(node.children):
                # OR=False needs every child False. For OR=True, make the first
                # child decisive (True) and keep later siblings neutral (False).
                values.append(cls.make(child, outcome and index == 0))
            return cls._combine(values)
        return None

    @classmethod
    def masked(cls, node, outcome) -> dict | None:
        """Force an outcome without binding short-circuited conditions.

        Example: once one child makes an AND False, the other children do not
        need values. Leaving them unbound avoids creating unnecessary or
        impossible constraints for shared input variables.
        """

        if isinstance(node, Atom):
            return {node: outcome}
        if isinstance(node, Not):
            return cls.masked(node.child, not outcome)
        if isinstance(node, And):
            children = node.children
            if not children:
                return {}
            if outcome:
                return cls._combine(cls.masked(child, True) for child in children)
            return cls.masked(children[0], False)
        if isinstance(node, Or):
            children = node.children
            if not children:
                return {}
            if outcome:
                # Reach the last OR branch so later conditions share a fully
                # evaluated context, including mutually exclusive enum states.
                return cls._combine(cls.masked(child, index == len(children) - 1)
                                    for index, child in enumerate(children))
            return cls._combine(cls.masked(child, False) for child in children)
        return None

    @staticmethod
    def _combine(assignments) -> dict | None:
        out = {}
        for assignment in assignments:
            if assignment is None:
                return None
            out.update(assignment)
        return out


class McdcGenerator:
    """Generate independence pairs and deduplicated rows for MC/DC coverage."""

    def pairs(self, expr) -> dict:
        """Build one MC/DC pair for each unique condition.

        The two rows keep the surrounding context fixed and flip only the target
        Atom. The pair is valid only if that change flips the whole decision.
        """

        atoms = self._unique_atoms(expr)
        shared = self._needs_masking(expr)
        out = {}
        for atom in atoms:
            context = self._context(expr, atom, shared)
            off = _merge(context, {atom: False})
            on = _merge(context, {atom: True})
            # The constructed context is only a fast recipe. Always verify the
            # complete decision; nested/shared expressions may need fallback.
            if not self._flips(expr, off, on):
                off, on = self._fallback_pair(expr, atom, atoms)
            if off is not None:
                out[atom] = (off, on)
        return out

    def rows(self, expr) -> list:

        pairs = self.pairs(expr)
        if not pairs:
            return []
        rows = []
        seen = set()
        # Reuse a canonical pair endpoint as the first row when possible.
        # Do not add it if it is not already evidence for one MC/DC pair.
        base = MaskingRecipes.make(expr, isinstance(expr, And))
        if base is not None and any(base in pair for pair in pairs.values()):
            self._append(rows, seen, base)
        for off, on in pairs.values():
            self._append(rows, seen, off)
            self._append(rows, seen, on)
        return rows

    @staticmethod
    def _append(rows, seen, assignment) -> None:
        key = frozenset(assignment.items())
        if key not in seen:
            seen.add(key)
            rows.append(assignment)

    @staticmethod
    def _unique_atoms(expr) -> list:
        out = []
        for atom in atoms_of(expr):
            if atom not in out:
                out.append(atom)
        return out

    @staticmethod
    def _needs_masking(expr) -> bool:
        """Detect one variable compared to multiple equality constants.

        ``x == 1`` and ``x == 2`` are separate Atoms, but their truth values are
        not independent when both must come from one concrete value of ``x``.
        """

        equalities = {}
        for atom in atoms_of(expr):
            if atom.var is None or atom.op != Op.EQ or atom.rhs is None:
                continue
            values = equalities.setdefault(atom.var, set())
            values.add(atom.rhs)
        for values in equalities.values():
            if len(values) > 1:
                return True
        return False

    def _context(self, node, target, shared=False):
        """Build the neutral sibling context that exposes ``target``'s effect.

        The recursion follows the branch containing ``target``. Siblings are set
        to the neutral value of the parent operator: True for AND and False for
        OR. Shared-equality expressions use masking to leave irrelevant siblings
        unbound.
        """

        if isinstance(node, Atom):
            return {} if node == target else None
        if isinstance(node, Not):
            return self._context(node.child, target, shared)
        children = node.children
        for index, child in enumerate(children):
            context = self._context(child, target, shared)
            if context is None:
                continue
            values = [context]
            for sibling_index, sibling in enumerate(children):
                if sibling_index != index:
                    recipe = MaskingRecipes.masked if shared else MaskingRecipes.make
                    values.append(recipe(sibling, isinstance(node, And)))
            return _merge(*values)
        return None

    @staticmethod
    def _flips(expr, off, on) -> bool:
        if off is None or on is None:
            return False
        left, right = eval_expr(expr, off), eval_expr(expr, on)
        return left is not None and right is not None and left != right

    def _fallback_pair(self, expr, target, atoms):
        """Exhaustively search for a fixed context when recipes cannot find one.

        Every non-target Atom has the same value in both rows, while only the
        target flips. This costs O(2^(n-1)), so the search is capped at 12 Atoms.
        """

        if len(atoms) > 12:
            return None, None
        others = []
        for atom in atoms:
            if atom != target:
                others.append(atom)
        for values in product((False, True), repeat=len(others)):
            off = {target: False}
            on = {target: True}
            for atom, value in zip(others, values):
                off[atom] = value
                on[atom] = value
            if self._flips(expr, off, on):
                return off, on
        return None, None
