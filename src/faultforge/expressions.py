"""Parses invariant checks against a whitelist of syntax and evaluates them without eval()."""

import ast
import operator
import re
from dataclasses import dataclass

MAX_EXPRESSION_LENGTH = 500
NAMESPACES = {"before", "after", "client"}
CLIENT_FIELDS = {"accepted", "rejected", "unexpected", "transport_error", "total"}
FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
}
COMPARE_OPERATORS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class ExpressionError(ValueError):
    """The check isn't allowed. Reported as config_invalid."""


class NotEvaluable(Exception):
    """A valid check can't be computed from the values available."""


@dataclass(frozen=True)
class Expression:
    source: str
    tree: ast.expr
    variables: frozenset[str]

    def evaluate(self, values: dict[str, int]) -> bool:
        return bool(_evaluate(self.tree, values))


def _validate_value(node: ast.AST, variables: set[str]) -> None:
    if isinstance(node, ast.Constant):
        if type(node.value) is not int:
            raise ExpressionError(f"only integer constants are allowed, found {node.value!r}")
    elif isinstance(node, ast.Attribute):
        if not isinstance(node.value, ast.Name) or node.value.id not in NAMESPACES:
            raise ExpressionError("variables must be before.<field>, after.<field> or client.<field>")
        if not FIELD_PATTERN.fullmatch(node.attr):
            raise ExpressionError(f"invalid field name {node.attr!r}")
        if node.value.id == "client" and node.attr not in CLIENT_FIELDS:
            raise ExpressionError(f"unknown variable client.{node.attr}")
        variables.add(f"{node.value.id}.{node.attr}")
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in BINARY_OPERATORS:
            raise ExpressionError(f"operator {type(node.op).__name__} isn't allowed")
        _validate_value(node.left, variables)
        _validate_value(node.right, variables)
    else:
        raise ExpressionError(f"{type(node).__name__} isn't allowed as a value")


def _validate_condition(node: ast.AST, variables: set[str]) -> None:
    """Every leaf is a comparison using a variable, so no branch can be vacuously constant."""
    if isinstance(node, ast.Compare):
        if any(type(op) not in COMPARE_OPERATORS for op in node.ops):
            raise ExpressionError("only ==, !=, <, <=, > and >= comparisons are allowed")
        used: set[str] = set()
        _validate_value(node.left, used)
        for comparator in node.comparators:
            _validate_value(comparator, used)
        if not used:
            raise ExpressionError("a comparison must use at least one variable")
        variables |= used
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            _validate_condition(value, variables)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _validate_condition(node.operand, variables)
    else:
        raise ExpressionError("a check and its and/or/not operands must be comparisons or boolean expressions")


def parse(source: str) -> Expression:
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(f"check is longer than {MAX_EXPRESSION_LENGTH} characters")
    try:
        tree = ast.parse(source, mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError) as error:
        raise ExpressionError(f"can't parse {source!r}: {error}") from error
    variables: set[str] = set()
    try:
        _validate_condition(tree, variables)
    except RecursionError as error:
        raise ExpressionError(f"check {source!r} is too deeply nested") from error
    return Expression(source=source, tree=tree, variables=frozenset(variables))


def is_client_upper_bound(expression: Expression) -> bool:
    """True for client.<counter> <= <expr> or <, where <expr> uses only before.* and integers.

    Only this form holds on an incomplete trial: the client count can only grow, and the bound
    was fixed before any load.
    """
    tree = expression.tree
    if not isinstance(tree, ast.Compare) or len(tree.ops) != 1 or not isinstance(tree.ops[0], ast.Lt | ast.LtE):
        return False
    if not isinstance(tree.left, ast.Attribute) or tree.left.value.id != "client":
        return False
    return all(
        node.value.id == "before" for node in ast.walk(tree.comparators[0]) if isinstance(node, ast.Attribute)
    )


def _evaluate(node: ast.expr, values: dict[str, int]) -> int | bool:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        name = f"{node.value.id}.{node.attr}"
        if name not in values:
            raise NotEvaluable(name)
        return values[name]
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left, values), _evaluate(node.right, values)
        try:
            return BINARY_OPERATORS[type(node.op)](left, right)
        except ZeroDivisionError as error:
            raise NotEvaluable("division by zero") from error
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, values)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(comparator, values)
            if not COMPARE_OPERATORS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        results = [_evaluate(value, values) for value in node.values]
        return all(results) if isinstance(node.op, ast.And) else any(results)
    return not _evaluate(node.operand, values)
