import time

import pytest

from app.agent.tools.function import FunctionToolExecutor, list_functions


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "open('/etc/passwd').read()",
        "2 ** 10000000",
        "1+" * 200,
    ],
)
async def test_calculator_safety(expression: str) -> None:
    started = time.perf_counter()
    result = await FunctionToolExecutor().execute(
        config={"function_name": "calculator"},
        arguments={"expression": expression},
        timeout_seconds=1,
    )
    assert not result.ok and result.error
    assert time.perf_counter() - started < 1


async def test_functions() -> None:
    assert {"calculator", "current_time"} <= {fn.name for fn in list_functions()}
    executor = FunctionToolExecutor()
    result = await executor.execute(
        config={"function_name": "calculator"},
        arguments={"expression": "2 * (3 + 4)"},
        timeout_seconds=1,
    )
    assert result.ok and float(result.content) == 14
    invalid = await executor.execute(
        config={"function_name": "calculator"}, arguments={}, timeout_seconds=1
    )
    assert not invalid.ok and "expression" in (invalid.error or "")
    current = await executor.execute(
        config={"function_name": "current_time"},
        arguments={"timezone": "UTC"},
        timeout_seconds=1,
    )
    assert current.ok and "+00:00" in current.content
    with pytest.raises(ValueError, match="Available:.*calculator"):
        executor.validate_config({"function_name": "missing"})
