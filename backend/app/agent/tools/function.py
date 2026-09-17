import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from app.agent.tools.base import ToolExecutor, ToolResult

FunctionHandler = Callable[..., Awaitable[str]]


class RegisteredFunction(BaseModel):
    name: str
    description: str
    args_model: type[BaseModel]
    handler: FunctionHandler
    model_config = {"arbitrary_types_allowed": True}


_REGISTRY: dict[str, RegisteredFunction] = {}


def register_function(
    *, name: str, description: str, args_model: type[BaseModel]
) -> Callable[[FunctionHandler], FunctionHandler]:
    def decorator(handler: FunctionHandler) -> FunctionHandler:
        _REGISTRY[name] = RegisteredFunction(
            name=name, description=description, args_model=args_model, handler=handler
        )
        return handler

    return decorator


def list_functions() -> list[RegisteredFunction]:
    return list(_REGISTRY.values())


def get_function(name: str) -> RegisteredFunction | None:
    return _REGISTRY.get(name)


class FunctionToolExecutor(ToolExecutor):
    def validate_config(self, config: dict[str, Any]) -> None:
        name = config.get("function_name")
        if not isinstance(name, str) or get_function(name) is None:
            raise ValueError(
                f"Unknown function '{name}'. Available: {', '.join(_REGISTRY)}"
            )

    async def execute(
        self, *, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: int
    ) -> ToolResult:
        started = time.perf_counter()
        try:
            self.validate_config(config)
            fn = _REGISTRY[config["function_name"]]
            parsed = fn.args_model.model_validate(arguments)
            content = await asyncio.wait_for(
                fn.handler(**parsed.model_dump()), timeout_seconds
            )
            result = ToolResult(ok=True, content=content)
        except Exception as exc:
            result = ToolResult(
                ok=False,
                content="",
                error=(str(exc) or f"Timed out after {timeout_seconds}s")[:1000],
            )
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
