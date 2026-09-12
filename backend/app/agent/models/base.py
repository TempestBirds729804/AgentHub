from abc import ABC, abstractmethod

from langchain_core.language_models import BaseChatModel


class ModelProvider(ABC):
    """Interface for model providers."""

    @abstractmethod
    def get_chat_model(
        self,
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        streaming: bool = False,
    ) -> BaseChatModel:
        """Return a LangChain chat model."""

    @abstractmethod
    def supports(self, model: str) -> bool:
        """Return whether the provider supports the model."""
