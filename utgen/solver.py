
from __future__ import annotations

from utgen.model import Atom, Constraint, Op


NEGATE = {Op.EQ: Op.NE, Op.NE: Op.EQ, Op.LT: Op.GE, Op.GE: Op.LT,
          Op.LE: Op.GT, Op.GT: Op.LE}


class Domain:
    """Track the values that are still valid for one C variable.

    Relational constraints shrink ``lo``/``hi`` or ban individual values,
    while bit constraints are kept separately as required/forbidden masks.
    Enum domains keep their declared values explicitly instead of treating
    every integer between the minimum and maximum as valid.
    """

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
        """Apply one constraint and report whether the domain is still satisfiable."""

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
            # Solver uses a negative mask to encode the false form of a bit test:
            # positive mask -> these bits must be 1; negative mask -> these bits must be 0.
            if value < 0:
                self.forbidden |= -value
            else:
                self.required |= value
        elif op == Op.BITANY:
            # Each BITANY mask is satisfied when at least one bit from that mask is set.
            self.any_required.append(value)
        else:
            raise ValueError(f"unsupported constraint operator: {op}")

        # Reject the domain immediately if required and forbidden bits conflict,
        # a BITANY mask has no possible bit left, or no concrete value can be found.
        possible = all(mask & ~self.forbidden & ((1 << self.ctype.bits) - 1) for mask in self.any_required)
        return not self.required & self.forbidden and possible and self._find(self.lo) is not None

    def _alive(self, value):
        while value in self.banned:
            value += 1
        return value if value <= self.hi else None

    def _find(self, start):
        """Return the smallest valid value at or above ``start``."""

        if self.values:
            # Enum variables may only use declared enumerator values.
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

        # _next_bits works on the fixed-width raw bit pattern. In two's complement,
        # negative signed values occupy the upper half of that unsigned bit space,
        # so a signed interval may need to be split into two raw ranges.
        mod = 1 << self.ctype.bits
        out = []
        if lo < 0:
            out.append((lo + mod, min(self.hi, -1) + mod))
        if self.hi >= 0:
            out.append((max(lo, 0), self.hi))
        return out

    def _next_bits(self, start, end):
        """Build the smallest raw bit pattern in [start, end] satisfying all masks.

        The recursive search is a small digit-DP over the variable's bits. ``tight``
        keeps the result >= ``start`` and ``pending`` tracks BITANY masks that still
        need at least one selected bit.
        """

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
                            # This chosen 1-bit satisfies that BITANY requirement.
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
        # Convert a Python integer to the C type's fixed-width two's-complement bits.
        return value % (1 << self.ctype.bits)

    def _from_raw(self, value):
        # Convert the raw fixed-width bit pattern back to the signed C integer value.
        sign = 1 << (self.ctype.bits - 1)
        return value - (sign << 1) if self.ctype.signed and value & sign else value

    def _bits_ok(self, value):
        raw = self._to_raw(value)
        return (raw & self.required == self.required and not raw & self.forbidden and
                all(raw & mask for mask in self.any_required))

    def pick(self, prefer=None) -> int:
        """Choose a valid value, using ``prefer`` when it satisfies every constraint."""

        if prefer is not None and self.lo <= prefer <= self.hi and prefer not in self.banned:
            if (not self.values or prefer in self.values) and self._bits_ok(prefer):
                return prefer
        value = self._find(self.lo)
        if value is None:
            raise ValueError("empty domain")
        return value


class AtomConstraints:
    """Translate a desired Boolean atom outcome into concrete numeric constraints."""

    @staticmethod
    def for_outcome(atom: Atom, outcome: bool, midpoint=True) -> list[Constraint]:
        if atom.var is None:
            return []
        if atom.op == Op.TRUTHY:
            # Prefer 1 for a true truthiness test so the solver avoids an arbitrary
            # non-zero extreme value when a simple representative value is enough.
            return [Constraint(atom.var, Op.NE, 0, 1) if outcome else Constraint(atom.var, Op.EQ, 0)]
        if atom.op == Op.BITSET:
            value = atom.rhs or 0
            # True means at least one masked bit must be set. False is passed through
            # BITSET with prefer=False; Solver converts that marker into a forbidden mask.
            return [Constraint(atom.var, Op.BITANY, value) if outcome else Constraint(atom.var, Op.BITSET, value, False)]
        op = atom.op if outcome else NEGATE[atom.op]
        value = atom.rhs
        if atom.rhs_var is not None:
            # Variable-vs-variable relations are reduced to two concrete constraints
            # by fixing the RHS variable to a representative value first.
            if not midpoint:
                # Fallback mode uses an extreme RHS value when the midpoint choice
                # cannot produce a satisfiable pair.
                value = atom.rhs_var.ctype.hi if op in (Op.LT, Op.LE) else atom.rhs_var.ctype.lo
                return [Constraint(atom.var, op, value), Constraint(atom.rhs_var, Op.EQ, value)]
            value = (atom.rhs_var.ctype.lo + atom.rhs_var.ctype.hi) // 2

            # Keep the LHS close to the RHS while still satisfying the requested
            # relation. This produces stable, non-extreme test values where possible.
            prefer = value - 1 if op == Op.LT else value + 1 if op in (Op.NE, Op.GT) else value
            prefer = min(max(prefer, atom.var.ctype.lo), atom.var.ctype.hi)
            return [Constraint(atom.var, op, value, prefer), Constraint(atom.rhs_var, Op.EQ, value)]
        return [Constraint(atom.var, op, value)]


class Solver:
    def solve(self, constraints) -> dict[str, int] | None:
        """Merge constraints per variable and choose one concrete value for each domain.

        ``None`` means the collected constraints are contradictory and no test input
        can satisfy them simultaneously.
        """

        domains = {}
        preferences = {}
        for constraint in constraints:
            name = constraint.var.name
            domain = domains.setdefault(name, Domain.for_type(constraint.var.ctype))

            # prefer=False is an internal marker used only for a false bit test.
            # Convert it to the negative-mask convention understood by Domain.restrict().
            value = -constraint.value if constraint.op == Op.BITSET and constraint.prefer is False else constraint.value
            if not domain.restrict(constraint.op, value):
                return None
            if constraint.prefer is not None and not isinstance(constraint.prefer, bool):
                preferences[name] = constraint.prefer
        out = {}
        for name, domain in domains.items():
            out[name] = domain.pick(preferences.get(name))
        return out
