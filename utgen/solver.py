"""Small integer constraint solver used to assemble generated test rows."""
from __future__ import annotations

from utgen.model import Atom, Constraint, Op


NEGATE = {Op.EQ: Op.NE, Op.NE: Op.EQ, Op.LT: Op.GE, Op.GE: Op.LT,
          Op.LE: Op.GT, Op.GT: Op.LE}


class Domain:
    def __init__(self, ctype):
        self.ctype = ctype
        self.values = tuple(sorted({value for _, value in ctype.enum_values}))
        self.lo = min(self.values) if self.values else ctype.lo
        self.hi = max(self.values) if self.values else ctype.hi
        self.banned = set()
        self.required = 0
        self.forbidden = 0
        self.any_required = []

    @classmethod
    def for_type(cls, ctype):
        return cls(ctype)

    def restrict(self, op, value) -> bool:
        if op == Op.EQ:
            self.lo = max(self.lo, value)
            self.hi = min(self.hi, value)
        elif op == Op.NE:
            self.banned.add(value)
        elif op == Op.LT:
            self.hi = min(self.hi, value - 1)
        elif op == Op.LE:
            self.hi = min(self.hi, value)
        elif op == Op.GT:
            self.lo = max(self.lo, value + 1)
        elif op == Op.GE:
            self.lo = max(self.lo, value)
        elif op == Op.BITSET:
            if value < 0:
                self.forbidden |= -value
            else:
                self.required |= value
        elif op == Op.BITANY:
            self.any_required.append(value)
        else:
            raise ValueError(f"unsupported constraint operator: {op}")
        possible = all(mask & ~self.forbidden & ((1 << self.ctype.bits) - 1) for mask in self.any_required)
        return not self.required & self.forbidden and possible and self._find(self.lo) is not None

    def _alive(self, value):
        while value in self.banned:
            value += 1
        return value if value <= self.hi else None

    def _find(self, start):
        if self.values:
            for value in self.values:
                if value >= max(self.lo, start) and value <= self.hi:
                    if value not in self.banned and self._bits_ok(value):
                        return value
            return None
        for lower, upper in self._ranges(start):
            raw = self._next_bits(lower, upper)
            while raw is not None:
                value = self._from_raw(raw)
                alive = self._alive(value)
                if alive == value:
                    return value
                if alive is None or alive > self.hi:
                    break
                raw = self._next_bits(self._to_raw(alive), upper)
        return None

    def _ranges(self, start):
        lo = max(self.lo, start)
        if not self.ctype.signed:
            return [(lo, self.hi)] if lo <= self.hi else []
        mod = 1 << self.ctype.bits
        out = []
        if lo < 0:
            out.append((lo + mod, min(self.hi, -1) + mod))
        if self.hi >= 0:
            out.append((max(lo, 0), self.hi))
        return out

    def _next_bits(self, start, end):
        if self.required & self.forbidden or self.required >> self.ctype.bits:
            return None
        missing = (1 << len(self.any_required)) - 1
        memo = {}

        def build(bit, tight, pending):
            key = bit, tight, pending
            if key in memo:
                return memo[key]
            if bit < 0:
                return 0 if not pending else None
            floor = (start >> bit) & 1
            required = (self.required >> bit) & 1
            forbidden = (self.forbidden >> bit) & 1
            for digit in (0, 1):
                if digit < required or digit and forbidden or tight and digit < floor:
                    continue
                remaining = pending
                if digit:
                    for index, mask in enumerate(self.any_required):
                        if mask >> bit & 1:
                            remaining &= ~(1 << index)
                tail = build(bit - 1, tight and digit == floor, remaining)
                if tail is not None:
                    memo[key] = digit << bit | tail
                    return memo[key]
            memo[key] = None
            return None
        value = build(self.ctype.bits - 1, True, missing)
        return value if value is not None and value <= end else None

    def _to_raw(self, value):
        return value % (1 << self.ctype.bits)

    def _from_raw(self, value):
        sign = 1 << (self.ctype.bits - 1)
        return value - (sign << 1) if self.ctype.signed and value & sign else value

    def _bits_ok(self, value):
        raw = self._to_raw(value)
        return (raw & self.required == self.required and not raw & self.forbidden and
                all(raw & mask for mask in self.any_required))

    def pick(self, prefer=None) -> int:
        if prefer is not None and self.lo <= prefer <= self.hi and prefer not in self.banned:
            if (not self.values or prefer in self.values) and self._bits_ok(prefer):
                return prefer
        value = self._find(self.lo)
        if value is None:
            raise ValueError("empty domain")
        return value


class AtomConstraints:
    @staticmethod
    def for_outcome(atom: Atom, outcome: bool, midpoint=True) -> list[Constraint]:
        if atom.var is None:
            return []
        if atom.op == Op.TRUTHY:
            return [Constraint(atom.var, Op.NE, 0, 1) if outcome else Constraint(atom.var, Op.EQ, 0)]
        if atom.op == Op.BITSET:
            value = atom.rhs or 0
            return [Constraint(atom.var, Op.BITANY, value) if outcome else Constraint(atom.var, Op.BITSET, value, False)]
        op = atom.op if outcome else NEGATE[atom.op]
        value = atom.rhs
        if atom.rhs_var is not None:
            if not midpoint:
                value = atom.rhs_var.ctype.hi if op in (Op.LT, Op.LE) else atom.rhs_var.ctype.lo
                return [Constraint(atom.var, op, value), Constraint(atom.rhs_var, Op.EQ, value)]
            value = (atom.rhs_var.ctype.lo + atom.rhs_var.ctype.hi) // 2
            prefer = value - 1 if op == Op.LT else value + 1 if op in (Op.NE, Op.GT) else value
            prefer = min(max(prefer, atom.var.ctype.lo), atom.var.ctype.hi)
            return [Constraint(atom.var, op, value, prefer), Constraint(atom.rhs_var, Op.EQ, value)]
        return [Constraint(atom.var, op, value)]


class Solver:
    def solve(self, constraints) -> dict[str, int] | None:
        domains = {}
        preferences = {}
        for constraint in constraints:
            name = constraint.var.name
            domain = domains.setdefault(name, Domain.for_type(constraint.var.ctype))
            value = -constraint.value if constraint.op == Op.BITSET and constraint.prefer is False else constraint.value
            if not domain.restrict(constraint.op, value):
                return None
            if constraint.prefer is not None and not isinstance(constraint.prefer, bool):
                preferences[name] = constraint.prefer
        out = {}
        for name, domain in domains.items():
            out[name] = domain.pick(preferences.get(name))
        return out
