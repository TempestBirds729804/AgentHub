import asyncio
import sys
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from redis import Redis
from sqlmodel import Session, delete

from app.agent.models import get_provider
from app.agent.models.openai_compat import OpenAICompatProvider
from app.core.config import settings
from app.core.db import engine, init_db
from app.main import app
from app.models import (
    Agent,
    AgentToolBinding,
    AgentVersion,
    ApprovalRequest,
    Conversation,
    ConvMessage,
    Run,
    RunEvent,
    Tool,
    User,
)
from tests.utils.user import authentication_token_from_email
from tests.utils.utils import get_superuser_token_headers


@pytest.fixture(autouse=True)
def clean_redis() -> Generator[None]:
    """Remove only Redis keys/jobs created by this test, never shared data."""
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    before = set(redis.scan_iter("agenthub:*")) | set(redis.scan_iter("arq:*"))
    jobs_before = set(redis.zrange("arq:queue", 0, -1))
    yield
    new_jobs = set(redis.zrange("arq:queue", 0, -1)) - jobs_before
    if new_jobs:
        redis.zrem("arq:queue", *new_jobs)
    after = set(redis.scan_iter("agenthub:*")) | set(redis.scan_iter("arq:*"))
    created = after - before
    if created:
        redis.delete(*created)
    redis.close()


@pytest.fixture(scope="session", autouse=True)
def db() -> Generator[Session]:
    with Session(engine) as session:
        init_db(session)
        yield session
        for model in (
            ApprovalRequest,
            RunEvent,
            Run,
            ConvMessage,
            Conversation,
            AgentToolBinding,
            Tool,
            AgentVersion,
            Agent,
            User,
        ):
            session.execute(delete(model))
        session.commit()


@pytest.fixture(scope="module")
def client() -> Generator[TestClient]:
    options = (
        {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    )
    with TestClient(app, backend_options=options) as c:
        yield c


@pytest.fixture(scope="module")
def superuser_token_headers(client: TestClient) -> dict[str, str]:
    return get_superuser_token_headers(client)


@pytest.fixture(scope="module")
def normal_user_token_headers(client: TestClient, db: Session) -> dict[str, str]:
    return authentication_token_from_email(
        client=client, email=settings.EMAIL_TEST_USER, db=db
    )


@pytest.fixture
def fake_chat_model(monkeypatch: pytest.MonkeyPatch) -> Generator[GenericFakeChatModel]:
    """Use a fake model and clear provider caching on both sides of every test."""
    fake = GenericFakeChatModel(messages=iter([AIMessage(content="fake reply")]))

    def _get_chat_model(
        _self: OpenAICompatProvider, **_kwargs: Any
    ) -> GenericFakeChatModel:
        return fake

    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(OpenAICompatProvider, "get_chat_model", _get_chat_model)
    get_provider.cache_clear()
    yield fake
    get_provider.cache_clear()
