from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    content: str
    error: str | None = None
    duration_ms: int = 0


class ToolExecutor(ABC):
    @abstractmethod
    async def execute(
        self, *, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: int
    ) -> ToolResult:
        """Return failures as results so the agent can recover."""

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> None:
        """Raise ValueError for invalid configuration."""
