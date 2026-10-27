import uuid
from decimal import Decimal
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agent.evaluation import EvaluationContext, evaluate_assertion, evaluate_case
from app.models import EvalCase, Run


def context(output: str = "Hello\nWorld") -> EvaluationContext:
    return EvaluationContext(
        output,
        Run(owner_id=uuid.uuid4(), agent_version_id=uuid.uuid4()),
        ["calculator"],
        100,
        Decimal("0.01"),
    )


@pytest.mark.parametrize(
    "assertion,passed",
    [
        ({"type": "exact", "value": " Hello\nWorld "}, True),
        ({"type": "exact", "value": "hello"}, False),
        ({"type": "contains", "value": "HELLO"}, True),
        ({"type": "contains", "value": "missing"}, False),
        ({"type": "not_contains", "value": "missing"}, True),
        ({"type": "not_contains", "value": "WORLD"}, False),
        ({"type": "regex", "value": "Hello.*World"}, True),
        ({"type": "regex", "value": "^no$"}, False),
        ({"type": "regex", "value": "["}, False),
        ({"type": "tool_called", "value": "calculator"}, True),
        ({"type": "tool_called", "value": "missing"}, False),
        ({"type": "tool_not_called", "value": "missing"}, True),
        ({"type": "tool_not_called", "value": "calculator"}, False),
        ({"type": "max_duration_ms", "value": 100}, True),
        ({"type": "max_duration_ms", "value": 99}, False),
        ({"type": "max_cost_usd", "value": 0.01}, True),
        ({"type": "max_cost_usd", "value": 0.001}, False),
        ({"type": "unknown", "value": "x"}, False),
    ],
)
async def test_assertions(assertion: dict[str, Any], passed: bool) -> None:
    assert (
        await evaluate_assertion(assertion=assertion, ctx=context())
    ).passed is passed


async def test_json_and_unknown_cost() -> None:
    ctx = context('{"items": [{"value": 2}]}')
    for value, passed in [(2, True), (3, False)]:
        assert (
            await evaluate_assertion(
                assertion={
                    "type": "json_path",
                    "path": "items.0.value",
                    "value": value,
                },
                ctx=ctx,
            )
        ).passed is passed
    ctx.cost_usd = None
    outcome = await evaluate_assertion(
        assertion={"type": "max_cost_usd", "value": 1}, ctx=ctx
    )
    assert not outcome.passed and "unknown" in (outcome.message or "")


@pytest.mark.parametrize(
    "reply,passed",
    [
        ('{"score":0.9,"reason":"good"}', True),
        ('{"score":0.5}', False),
        ('```json\n{"score":0.9}\n```', True),
        ("not JSON", False),
        ('{"score": 2}', False),
    ],
)
async def test_judge(
    fake_chat_model: GenericFakeChatModel, reply: str, passed: bool
) -> None:
    fake_chat_model.messages = iter([AIMessage(content=reply)])
    outcome = await evaluate_assertion(
        assertion={"type": "llm_judge", "value": "helpful", "threshold": 0.7},
        ctx=context(),
    )
    assert outcome.passed is passed


async def test_aggregation(fake_chat_model: GenericFakeChatModel) -> None:
    case = EvalCase(agent_id=uuid.uuid4(), name="test")
    assert await evaluate_case(case=case, ctx=context()) == (True, 1, [])
    case.expected_output = "HELLO"
    assert (await evaluate_case(case=case, ctx=context()))[:2] == (True, 1)
    case.assertions = [
        {"type": "contains", "value": v} for v in ("hello", "world", "missing")
    ]
    assert (await evaluate_case(case=case, ctx=context()))[:2] == (False, 0.6667)
    case.assertions = [{"type": "llm_judge", "value": "helpful"}]
    fake_chat_model.messages = iter([AIMessage(content='{"score":0.8}')])
    assert (await evaluate_case(case=case, ctx=context()))[:2] == (True, 0.8)


async def test_regex_timeout() -> None:
    result = await evaluate_assertion(
        assertion={"type": "regex", "value": "(a+)+$"}, ctx=context("a" * 10000 + "!")
    )
    assert not result.passed and "TimeoutError" in (result.message or "")
