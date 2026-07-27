"""Masking MC/DC recipes and independence-pair generation."""
from itertools import product

from utgen.model import And, Atom, Not, Or, atoms_of


def eval_expr(node, assign) -> bool:
    """Evaluate a boolean model node with atom outcomes from ``assign``."""
    if isinstance(node, Atom):
        return bool(assign[node])
    if isinstance(node, Not):
        return not eval_expr(node.child, assign)
    if isinstance(node, And):
        return all(eval_expr(child, assign) for child in node.children)
    if isinstance(node, Or):
        return any(eval_expr(child, assign) for child in node.children)
    raise TypeError(f"unsupported boolean node: {type(node).__name__}")


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
    def make(cls, node, outcome) -> dict:
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
        raise TypeError(f"unsupported boolean node: {type(node).__name__}")

    @staticmethod
    def _combine(assignments) -> dict:
        out = {}
        for assignment in assignments:
            out.update(assignment)
        return out


class McdcGenerator:
    """Generate verified masking independence pairs and their unique rows."""
    def pairs(self, expr) -> dict:
        """Return verified independence pairs keyed by independently effective atom."""
        atoms = self._unique_atoms(expr)
        out = {}
        for atom in atoms:
            context = self._context(expr, atom)
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
        if any(base in pair for pair in pairs.values()):
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

    def _context(self, node, target):
        if isinstance(node, Atom):
            return {} if node == target else None
        if isinstance(node, Not):
            return self._context(node.child, target)
        children = node.children
        for index, child in enumerate(children):
            context = self._context(child, target)
            if context is None:
                continue
            values = [context]
            for sibling_index, sibling in enumerate(children):
                if sibling_index != index:
                    values.append(MaskingRecipes.make(sibling, isinstance(node, And)))
            return _merge(*values)
        return None

    @staticmethod
    def _flips(expr, off, on) -> bool:
        return off is not None and on is not None and eval_expr(expr, off) != eval_expr(expr, on)

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
