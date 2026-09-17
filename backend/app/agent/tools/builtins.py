import ast
import math
import operator
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.agent.tools.function import register_function

_ALLOWED_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}


def _safe_eval_arithmetic(expression: str) -> float:
    if len(expression) > 200:
        raise ValueError("Expression too long")

    def evaluate(node: ast.AST) -> float:
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
        ):
            value = float(node.value)
        elif isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
                raise ValueError("Exponent out of allowed range")
            value = _ALLOWED_OPS[type(node.op)](left, right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            value = evaluate(node.operand) * (
                -1 if isinstance(node.op, ast.USub) else 1
            )
        else:
            raise ValueError(f"Unsupported expression element: {type(node).__name__}")
        if not isinstance(value, float) or not math.isfinite(value):
            raise ValueError("Result is not a finite real number")
        return value

    return evaluate(ast.parse(expression, mode="eval").body)


class CalculatorArgs(BaseModel):
    expression: str = Field(description="Arithmetic expression, e.g. '2 * (3 + 4)'")


@register_function(
    name="calculator",
    description="Evaluate basic arithmetic with numbers, parentheses, + - * / % and bounded powers.",
    args_model=CalculatorArgs,
)
async def calculator(expression: str) -> str:
    return str(_safe_eval_arithmetic(expression))


class CurrentTimeArgs(BaseModel):
    timezone: str = Field(default="UTC", description="IANA timezone name")


@register_function(
    name="current_time",
    description="Get the current date and time in a given timezone.",
    args_model=CurrentTimeArgs,
)
async def current_time(timezone: str = "UTC") -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat()
