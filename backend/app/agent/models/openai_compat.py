from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.agent.exceptions import ModelNotConfiguredError
from app.agent.models.base import ModelProvider
from app.core.config import settings


class OpenAICompatProvider(ModelProvider):
    """Access OpenAI-compatible services through a configurable base URL."""

    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.LLM_API_KEY
        self.base_url = base_url if base_url is not None else settings.LLM_BASE_URL
        if not self.api_key:
            raise ModelNotConfiguredError(
                "LLM_API_KEY is not configured. Set it in .env before running agents."
            )

    def get_chat_model(
        self,
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool = False,
    ) -> BaseChatModel:
        return ChatOpenAI(
            model=model,
            api_key=SecretStr(self.api_key),
            base_url=self.base_url,
            temperature=temperature if temperature is not None else 0.7,
            max_completion_tokens=max_tokens,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
            streaming=streaming,
            stream_usage=True,
        )

    def supports(self, model: str) -> bool:
        return True
