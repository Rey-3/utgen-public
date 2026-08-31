
from __future__ import annotations

from utgen.model import Inventory, TestRow, Variable


class FaultGenerator:


    def __init__(self, overrides: dict | None = None) -> None:
        self.overrides = overrides or {}

    def rows(self, inventory: Inventory) -> list[TestRow]:

        from utgen.assembler import RowAssembler

        defaults = RowAssembler._defaults(inventory.inputs())
        rows = []
        seen = set()
        for variable in inventory.inputs():
            for value in self._values(variable):
                values = defaults | {variable.name: value}
                key = tuple(values.items())
                if key in seen:
                    continue
                seen.add(key)
                from utgen.iteration.fingerprint import logical_argument_key, obligation_key
                rows.append(TestRow(
                    values,
                    "Fault Injection",
                    f"fault {variable.name}: {value}",
                    obligation_keys=(obligation_key(
                        "fault",
                        logical_argument_key_value=logical_argument_key(variable.name),
                        outcome=value,
                    ),),
                ))
        return rows

    def _values(self, variable: Variable) -> list[int | str]:
        if variable.is_pointer:
            return ["NULL"]
        override = self.overrides.get(variable.ctype.name)
        if override is not None:
            return list(override)
        if variable.ctype.enum_values:
            return [variable.ctype.enum_max + 1]
        if variable.ctype.signed:
            return [variable.ctype.lo, variable.ctype.hi]
        return [0, variable.ctype.hi]
