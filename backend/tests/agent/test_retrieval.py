import json
import random
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import text
from sqlmodel import Session

from app.agent import retrieval
from app.agent.graph import compile_agent_graph
from app.agent.nodes import retrieve_context
from app.core.async_db import async_session_maker
from app.core.config import settings
from app.models import Chunk, Document, DocumentStatus
from tests.agent.test_graph_tools import initial_state, tool_reply
from tests.utils.knowledge import create_random_knowledge_base


def test_retrieval_and_index(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = create_random_knowledge_base(db)
    other = create_random_knowledge_base(db)
    target = [1.0] + [0.0] * (settings.EMBEDDING_DIM - 1)
    doc = Document(
        knowledge_base_id=kb.id,
        filename="known.txt",
        mime_type="text/plain",
        storage_key="unused",
        size_bytes=1,
        status=DocumentStatus.READY,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    db.add(
        Chunk(
            document_id=doc.id,
            knowledge_base_id=kb.id,
            seq=0,
            content="target",
            embedding=target,
        )
    )
    db.add(
        Chunk(
            document_id=doc.id,
            knowledge_base_id=kb.id,
            seq=1,
            content="other",
            embedding=[0.0, 1.0] + [0.0] * (settings.EMBEDDING_DIM - 2),
        )
    )
    db.commit()
    db.execute(text("ANALYZE chunk"))
    db.commit()

    class Fake:
        async def aembed_query(self, _query: str) -> list[float]:
            return target

    monkeypatch.setattr(retrieval, "get_embeddings", lambda: Fake())

    async def check() -> None:
        async with async_session_maker() as session:
            assert (
                await retrieval.retrieve(
                    session=session, knowledge_base_ids=[], query="q"
                )
                == []
            )
            assert (
                await retrieval.retrieve(
                    session=session, knowledge_base_ids=[other.id], query="q"
                )
                == []
            )
            rows = await retrieval.retrieve(
                session=session, knowledge_base_ids=[kb.id], query="q", top_k=5
            )
            assert (
                len(rows) == 1
                and rows[0].content == "target"
                and rows[0].score == pytest.approx(1)
            )
            rows = await retrieval.retrieve(
                session=session,
                knowledge_base_ids=[kb.id],
                query="q",
                top_k=1,
                score_threshold=0,
            )
            assert len(rows) == 1

    client.portal.call(check)
    kb.sqlmodel_update({"embedding_model": "wrong"})
    db.add(kb)
    db.commit()

    async def mismatch() -> None:
        async with async_session_maker() as session:
            with pytest.raises(retrieval.EmbeddingMismatchError):
                await retrieval.retrieve(
                    session=session, knowledge_base_ids=[kb.id], query="q"
                )

    client.portal.call(mismatch)


def test_hnsw_index_is_used(db: Session) -> None:
    kb = create_random_knowledge_base(db)
    doc = Document(
        knowledge_base_id=kb.id,
        filename="index.txt",
        mime_type="text/plain",
        storage_key="unused",
        size_bytes=1,
        status=DocumentStatus.READY,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    rng = random.Random(8)
    for seq in range(1000):
        db.add(
            Chunk(
                document_id=doc.id,
                knowledge_base_id=kb.id,
                seq=seq,
                content="index fixture",
                embedding=[rng.uniform(-1, 1) for _ in range(settings.EMBEDDING_DIM)],
            )
        )
    db.commit()
    # Test databases are reused after bulk cleanup. Rebuild the index so the
    # planner check measures these fixtures rather than historical index bloat.
    db.execute(text("REINDEX INDEX chunk_embedding_hnsw_idx"))
    db.execute(text("ANALYZE chunk"))
    plan = db.execute(
        text(
            "EXPLAIN (FORMAT JSON) SELECT id FROM chunk ORDER BY embedding <=> CAST(:vec AS vector) LIMIT 5"
        ),
        {"vec": str([1.0] + [0.0] * (settings.EMBEDDING_DIM - 1))},
    ).scalar_one()
    db.commit()
    assert "chunk_embedding_hnsw_idx" in json.dumps(plan)


@pytest.mark.asyncio
async def test_node_database_failure() -> None:
    def broken() -> None:
        raise RuntimeError("database unavailable")

    result = await retrieve_context(
        {
            "knowledge_base_ids": [str(uuid.uuid4())],
            "messages": [HumanMessage(content="query")],
        },
        session_factory=broken,
    )
    assert result == {"retrieved_chunks": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, True])
async def test_retrieval_runs_once_across_tools_and_resume(
    fake_chat_model: GenericFakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
    approval: bool,
) -> None:
    calls = 0
    chunks = [{"index": 1, "filename": "fact.txt", "seq": 0, "content": "Fact 7429"}]

    async def retrieve_once(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"retrieved_chunks": chunks}

    def bind(
        self: GenericFakeChatModel, *_args: Any, **_kwargs: Any
    ) -> GenericFakeChatModel:
        return self

    def calculate(expression: str) -> str:
        assert expression == "1+1"
        return "2"

    monkeypatch.setattr("app.agent.graph.retrieve_context", retrieve_once)
    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", bind)
    reply = tool_reply()
    fake_chat_model.messages = iter([reply, AIMessage(content="Fact 7429 [1]")])
    tool = StructuredTool.from_function(
        calculate, name="calculator", description="Calculate"
    )
    graph = compile_agent_graph(
        [tool],
        with_retrieval=True,
        with_approval=approval,
        checkpointer=InMemorySaver(),
    )
    state = initial_state()
    state["knowledge_base_ids"] = [str(uuid.uuid4())]
    state["approval_required_tools"] = ["calculator"] if approval else []
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await graph.ainvoke(state, config)
    if approval:
        saved = await graph.aget_state(config)
        assert saved.next == ("approval_gate",)
        assert saved.values["retrieved_chunks"] == chunks
        result = await graph.ainvoke(
            Command(resume={reply.tool_calls[0]["id"]: {"approved": True}}), config
        )
    assert calls == 1
    assert result["retrieved_chunks"] == chunks
    assert result["iteration"] == 2
    assert result["messages"][-1].content == "Fact 7429 [1]"
