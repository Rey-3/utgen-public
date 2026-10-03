"""Describe observed decisions using one configurable language and vocabulary."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
import re

from utgen.mcdc import eval_expr
from utgen.model import (
    And, Assign, Atom, Const, DoWhile, For, If, Not, Or, Switch, Ternary, While, atom_text, atoms_of,
)


KEYWORDS = {
    "zh": {
        "test": "测试", "decision": "判定", "precondition": "前置条件",
        "is": "为", "outcome": "整体判定", "true_value": "True", "false_value": "False",
        "and": "且", "or": "或", "not": "非", "short_circuit": "因短路未求值",
        "all_conditions": "以下条件需要全部成立", "any_condition": "以下条件任一成立即可",
        "negated_group": "以下条件组取反", "case_context": "当前 case 路径",
        "eq": "等于", "ne": "不等于", "gt": "大于", "ge": "大于等于", "lt": "小于", "le": "小于等于",
        "case": "进入 case", "default": "进入 default", "if_true": "进入 if 分支",
        "if_false": "跳过 if 分支", "else_branch": "进入 else 分支", "ternary_true": "选择三目表达式的 true 分支",
        "ternary_false": "选择三目表达式的 false 分支", "expression": "得到表达式结果",
        "loop": "循环条件", "loop_true": "进入循环", "loop_false": "循环条件不成立",
        "nominal": "正常执行路径", "timeout": "生成超时的备用用例",
        "boundary": "边界值测试", "fault": "故障注入测试", "unobserved": "未观察到判定",
    },
    "en": {
        "test": "Test", "decision": "decision", "precondition": "precondition",
        "is": "is", "outcome": "overall decision", "true_value": "True", "false_value": "False",
        "and": "and", "or": "or", "not": "not", "short_circuit": "not evaluated due to short-circuit",
        "all_conditions": "All of the following conditions must hold", "any_condition": "Any of the following conditions may hold",
        "negated_group": "Negate the following condition group", "case_context": "current case path",
        "eq": "equals", "ne": "does not equal", "gt": "greater than", "ge": "greater than or equal to",
        "lt": "less than", "le": "less than or equal to", "case": "select case", "default": "select default",
        "if_true": "take the if branch", "if_false": "skip the if branch", "else_branch": "take the else branch",
        "ternary_true": "select the ternary true arm", "ternary_false": "select the ternary false arm",
        "expression": "produce the expression result", "loop": "loop condition", "loop_true": "enter the loop",
        "loop_false": "loop condition is not satisfied", "nominal": "normal execution path",
        "timeout": "fallback case after generation timeout", "boundary": "boundary value test",
        "fault": "fault injection test", "unobserved": "no decision observed",
    },
}
_COMPARISONS = {"==": "eq", "!=": "ne", ">": "gt", ">=": "ge", "<": "lt", "<=": "le"}
_INVERSE_COMPARISONS = {"==": "ne", "!=": "eq", ">": "le", ">=": "lt", "<": "ge", "<=": "gt"}
# Preserve quoted C literals and distinguish comparisons from shifts.
_C_LITERAL = r'''"(?:\\.|[^"\\])*"|(?:'(?:\\.|[^'\\])*')'''
_TOKENS = re.compile(_C_LITERAL + r'|\s*(?<![<>=!])(?:==|!=|>=|<=|>|<)(?![<>=!])\s*|\s*[()]\s*')
_SPACES = re.compile(_C_LITERAL + r'|\s+')


class DescriptionFormatter:
    def __init__(self, config=None):
        config = {} if config is None else config
        if not isinstance(config, Mapping) or set(config) - {"language", "keywords"}:
            raise ValueError("description must contain only language and keywords")
        language = config.get("language", "zh")
        if not isinstance(language, str) or language not in KEYWORDS:
            raise ValueError("description.language must be zh or en")
        keywords = config.get("keywords", {})
        if not isinstance(keywords, Mapping) or set(keywords) - KEYWORDS[language].keys():
            raise ValueError("description.keywords contains an unknown keyword")
        if any(not isinstance(value, str) or not value.strip() for value in keywords.values()):
            raise ValueError("description.keywords values must be non-empty strings")
        self.words = KEYWORDS[language] | dict(keywords)

    @staticmethod
    def layout(function):
        """Locate branch kinds, enclosing switches and loops without executing them."""
        kinds, contexts, loops, seen = {}, {}, {}, set()

        def visit(node, switches=()):
            if id(node) in seen:
                return
            seen.add(id(node))
            decision_id = getattr(node, "decision_id", None)
            if decision_id:
                contexts[decision_id] = switches
            if isinstance(node, (Ternary, Assign)) and node.decision_id:
                kinds[node.decision_id] = "ternary" if isinstance(node, Ternary) else "expression"
            if isinstance(node, If) and node.els:
                kinds[node.decision_id] = "if_else"
            if isinstance(node, (While, For, DoWhile)):
                loops[id(node)] = node
            if isinstance(node, Switch):
                visit(node.subject, switches)
                for case in node.cases:
                    contexts[case.decision_id] = switches
                    visit(case.body, switches + (node,))
                return
            if is_dataclass(node):
                for item in fields(node):
                    visit(getattr(node, item.name), switches)
            elif isinstance(node, (tuple, list)):
                for child in node:
                    visit(child, switches)

        visit(function.body)
        return kinds, contexts, loops, {decision.id: decision for decision in function.decisions}

    def _value(self, value):
        return self.words["true_value" if value else "false_value"]

    @staticmethod
    def _comparison(text):
        """Find a single outer comparison, excluding quoted literals and operands."""
        depth, comparisons = 0, []
        for match in _TOKENS.finditer(text):
            token = match[0].strip()
            if token in {"(", ")"}:
                depth += 1 if token == "(" else -1
            elif token in _COMPARISONS and depth == 0:
                comparisons.append(match.start())
        return comparisons[0] if len(comparisons) == 1 else None

    def _atom(self, atom, invert=False):
        source = atom_text(atom)
        comparison = self._comparison(source) if invert else None

        def replace(match):
            token = match[0].strip()
            if token in _COMPARISONS:
                keyword = _INVERSE_COMPARISONS[token] if match.start() == comparison else _COMPARISONS[token]
                return f" {self.words[keyword]} "
            return " " if token in {"(", ")"} else match[0]

        text = _TOKENS.sub(replace, source)
        return _SPACES.sub(lambda match: " " if match[0].isspace() else match[0], text).strip()

    def _expression(self, expr, pattern=None):
        if isinstance(expr, Atom):
            text = self._atom(expr)
            if pattern is None:
                return text
            value = pattern.get(id(expr))
            if value is None:
                return ""
            if self._comparison(atom_text(expr)) is not None:
                return self._atom(expr, invert=not value)
            return f"{text} {self.words['is']} {self._value(value)}"
        if isinstance(expr, Not):
            text = self._expression(expr.child, pattern)
            return f"{self.words['not']} {text}" if pattern is None else text
        if isinstance(expr, (And, Or)):
            separator = f" {self.words['and' if isinstance(expr, And) else 'or']} "
            return separator.join(filter(None, (self._expression(child, pattern) for child in expr.children)))
        return str(expr.value) if isinstance(expr, Const) else str(expr)

    def _case(self, expr):
        default = isinstance(expr, And) and all(isinstance(child, Not) for child in expr.children)
        text = f" {self.words['and']} ".join(self._atom(child.child, invert=True) for child in expr.children) if default else self._expression(expr)
        return f"{self.words['default' if default else 'case']} {text}".strip()

    @staticmethod
    def _compound(expr):
        return isinstance(expr, (And, Or)) or isinstance(expr, Not) and DescriptionFormatter._compound(expr.child)

    def _condition_lines(self, expr, pattern):
        if isinstance(expr, (And, Or)):
            return [line for child in expr.children for line in self._condition_lines(child, pattern)]
        if isinstance(expr, Not) and self._compound(expr.child) and pattern is not None:
            return self._condition_lines(expr.child, pattern)
        text = self._expression(expr, pattern)
        return ["- " + text] if text else []

    def _case_path(self, decision, result, switches, decisions):
        parts = []
        for switch in switches:
            observed = [decisions[case.decision_id] for case in switch.cases
                        if case.decision_id in decisions and True in result.observed.get(case.decision_id, ())]
            selected = next((case for case in observed if any(guard.outcome and (guard.expr is case.expr or guard.expr == case.expr)
                            for guard in decision.guards)), observed[0] if observed else None)
            if selected is not None:
                parts.append(self._case(selected.expr))
        return " -> ".join(parts)

    @staticmethod
    def _pattern(decision, row, result):
        observed = result.observed[decision.id]
        actual = row.intent.get(decision.id)
        if actual not in observed:
            actual = result.outcomes.get(decision.id, min(observed))
        atoms = tuple(atoms_of(decision.expr))
        patterns = sorted(result.conditions.get(decision.id, ()), key=lambda values: tuple(-1 if value is None else int(value) for value in values))
        for pattern in patterns:
            values = {id(atom): value for atom, value in zip(atoms, pattern)}
            # A lone recorded pattern is authoritative even when IR normalization
            # negates a bit atom whose text still contains the source comparison.
            if len(patterns) == 1 or eval_expr(decision.expr, {atom: bool(values.get(id(atom))) for atom in atoms}) == actual:
                return values, actual
        return None, actual

    def describe(self, function, row, result, layout=None):
        kinds, contexts, loops, decisions = self.layout(function) if layout is None else layout
        if row.target_loop_id in loops:
            loop = loops[row.target_loop_id]
            lines = []
            for actual in sorted(result.loops.get(row.target_loop_id, ()), reverse=True):
                values = {id(loop.cond): actual} if isinstance(loop.cond, Atom) else None
                lines.append(f"{self.words['test']} {self.words['loop']}: {self._expression(loop.cond, values)}; {self.words['outcome']} {self.words['is']} {self._value(actual)}; {self.words['loop_true' if actual else 'loop_false']}")
            return "\n".join(lines)
        target = row.target_decision_id
        if target is None:
            target = next(iter(row.intent), None)
        if target is None and len(function.decisions) == 1 and row.kind not in {"Boundary", "Fault Injection", "Timeout"}:
            target = function.decisions[0].id
        parts = []
        decision = decisions.get(target)
        if decision is not None:
            observed = result.observed.get(decision.id, ())
            prefix = f"{self.words['test']} {self.words['decision']} {decision.id}: "
            if not observed:
                parts.append(f"{prefix}{self.words['unobserved']}")
                parts.extend(self._condition_lines(decision.expr, None) if self._compound(decision.expr) else [self._expression(decision.expr)])
            elif decision.kind == "case":
                _, actual = self._pattern(decision, row, result)
                if actual:
                    parts.append(prefix + self._case(decision.expr))
                else:
                    values = {id(decision.expr): False} if isinstance(decision.expr, Atom) else None
                    parts.append(f"{prefix}{self._expression(decision.expr, values)}; {self.words['outcome']} {self.words['is']} {self._value(False)}")
                path = self._case_path(decision, result, contexts.get(target, ()), decisions)
                if path:
                    parts.append(f"{self.words['case_context']}: {path}")
            else:
                values, actual = self._pattern(decision, row, result)
                branch = kinds.get(decision.id, "if")
                if branch == "if_else":
                    branch_key = "if_true" if actual else "else_branch"
                else:
                    branch_key = branch if branch == "expression" else f"{branch}_{'true' if actual else 'false'}"
                outcome = f"{self.words['outcome']} {self.words['is']} {self._value(actual)}; {self.words[branch_key]}"
                path = self._case_path(decision, result, contexts.get(target, ()), decisions)
                if self._compound(decision.expr) or path:
                    parts.append(prefix + outcome)
                    if path:
                        parts.append(f"{self.words['case_context']}: {path}")
                    parts.extend(self._condition_lines(decision.expr, values) if self._compound(decision.expr) else ["- " + self._expression(decision.expr, values)])
                else:
                    parts.append(f"{prefix}{self._expression(decision.expr, values)}; {outcome}")
        category = {"Boundary": "boundary", "Fault Injection": "fault", "Timeout": "timeout"}.get(row.kind)
        if category:
            parts.insert(0, f"{self.words['test']} {self.words[category]}")
        if not parts:
            parts.append(f"{self.words['test']} {self.words['unobserved' if function.decisions else 'nominal']}")
        return "\n".join(parts)
