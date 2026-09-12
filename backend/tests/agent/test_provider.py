import pytest
from langchain_openai import ChatOpenAI

from app.agent.exceptions import ModelNotConfiguredError
from app.agent.models.openai_compat import OpenAICompatProvider
from app.core.config import settings


def test_provider_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    with pytest.raises(ModelNotConfiguredError, match="LLM_API_KEY"):
        OpenAICompatProvider()


def test_provider_configuration() -> None:
    provider = OpenAICompatProvider(
        api_key="test-key", base_url="http://127.0.0.1:1/v1"
    )
    model = provider.get_chat_model(
        model="custom-model", temperature=0, max_tokens=123, streaming=True
    )
    assert isinstance(model, ChatOpenAI)
    assert model.temperature == 0
    assert model.max_tokens == 123
    assert model.streaming and model.stream_usage
    assert model.openai_api_base == "http://127.0.0.1:1/v1"
    assert provider.supports("custom-model")
