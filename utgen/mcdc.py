"""Masking MC/DC recipes and independence-pair generation."""
from itertools import product

from utgen.model import And, Atom, Not, Op, Or, atoms_of


def eval_expr(node, assign) -> bool | None:
    """Evaluate a boolean model node with atom outcomes from ``assign``."""
    if isinstance(node, Atom):
        return bool(assign[node])
    if isinstance(node, Not):
        value = eval_expr(node.child, assign)
        return None if value is None else not value
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
    out = {}
    for assignment in assignments:
        if assignment is None:
            return None
        for atom, outcome in assignment.items():
            if atom in out and out[atom] != outcome:
                return None
            out[atom] = outcome
    return out


class MaskingRecipes:
    """Build canonical assignments that make a boolean node true or false."""
    @classmethod
    def make(cls, node, outcome) -> dict | None:
        """Return the first canonical assignment for ``node`` to have ``outcome``."""
        if isinstance(node, Atom):
            return {node: outcome}
        if isinstance(node, Not):
            return cls.make(node.child, not outcome)
        if isinstance(node, And):
            values = []
            for index, child in enumerate(node.children):
                values.append(cls.make(child, outcome or index != 0))
            return cls._combine(values)
        if isinstance(node, Or):
            values = []
            for index, child in enumerate(node.children):
                values.append(cls.make(child, outcome and index == 0))
            return cls._combine(values)
        return None

    @classmethod
    def masked(cls, node, outcome) -> dict | None:
        """Return the shortest short-circuit assignment for ``node``."""
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
                return cls.masked(children[0], True)
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
    """Generate verified masking independence pairs and their unique rows."""
    def pairs(self, expr) -> dict:
        """Return verified independence pairs keyed by independently effective atom."""
        atoms = self._unique_atoms(expr)
        shared = self._needs_masking(expr)
        out = {}
        for atom in atoms:
            context = self._context(expr, atom, shared)
            off = _merge(context, {atom: False})
            on = _merge(context, {atom: True})
            if not self._flips(expr, off, on):
                off, on = self._fallback_pair(expr, atom, atoms)
            if off is not None:
                out[atom] = (off, on)
        return out

    def rows(self, expr) -> list:
        """Return deterministic, deduplicated assignments covering all pairs."""
        pairs = self.pairs(expr)
        if not pairs:
            return []
        rows = []
        seen = set()
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
