"""Agent model adapters."""

from functools import lru_cache

from app.agent.models.base import ModelProvider
from app.agent.models.openai_compat import OpenAICompatProvider


@lru_cache(maxsize=1)
def get_provider() -> ModelProvider:
    """Return the configured OpenAI-compatible model provider."""
    return OpenAICompatProvider()
