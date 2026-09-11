from typing import Any

from pydantic import BaseModel, Field


class AgentSnapshot(BaseModel):
    """Validated structure stored in AgentVersion.snapshot."""

    name: str
    description: str | None = None
    system_prompt: str = ""
    llm_model: str = ""
    llm_settings: dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = 10
    timeout_seconds: int = 300
