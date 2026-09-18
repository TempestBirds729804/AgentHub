import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage
from sqlmodel import Session, select

from app.agent import ingestion, retrieval
from app.core.config import settings
from app.core.storage import get_minio_client
from app.models import Chunk, Document, DocumentStatus, RunEvent, User, get_datetime_utc
from app.worker.tasks import (
    execute_run_task,
    process_document_task,
    reap_stale_documents,
)
from tests.utils.knowledge import create_random_knowledge_base


@pytest.fixture
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] + [0.0] * (settings.EMBEDDING_DIM - 1) for _ in texts]

        async def aembed_query(self, text: str) -> list[float]:
            return (await self.aembed_documents([text]))[0]

    monkeypatch.setattr(ingestion, "get_embeddings", lambda: Fake())
    monkeypatch.setattr(retrieval, "get_embeddings", lambda: Fake())


@pytest.mark.usefixtures("fake_embeddings")
def test_knowledge_lifecycle(
    client: TestClient, db: Session, normal_user_token_headers: dict[str, str]
) -> None:
    h = normal_user_token_headers
    root = "/api/v1/knowledge/"
    r = client.post(
        root,
        headers=h,
        json={"name": "phase08", "chunk_size": 100, "chunk_overlap": 20},
    )
    assert r.status_code == 200, r.text
    kb = r.json()
    assert kb["embedding_model"] == settings.EMBEDDING_MODEL
    assert kb["embedding_dim"] == 1536
    url = root + kb["id"]
    r = client.post(
        url + "/documents",
        headers=h,
        files={
            "file": ("../../fact.txt", b"AgentHub secret is 7429. " * 20, "text/plain")
        },
    )
    assert r.status_code == 202, r.text
    doc = r.json()
    assert doc["status"] == "pending"
    stored = db.get(Document, uuid.UUID(doc["id"]))
    assert ".." not in stored.storage_key
    key = stored.storage_key
    try:
        assert (
            client.portal.call(process_document_task, {}, doc["id"])["status"]
            == "ready"
        )
        assert (
            client.portal.call(process_document_task, {}, doc["id"])["status"]
            == "already_ready"
        )
        detail = client.get(url, headers=h).json()
        assert detail["document_count"] == detail["ready_document_count"] == 1
        result = client.post(
            url + "/search", headers=h, json={"query": "secret", "top_k": 2}
        )
        assert result.status_code == 200, result.text
        assert result.json()["count"] == 2
        assert result.json()["data"][0]["score"] == pytest.approx(1)
        db.refresh(stored)
        count = stored.chunk_count
        stored.sqlmodel_update({"status": DocumentStatus.FAILED})
        db.add(stored)
        db.commit()
        assert (
            client.post(
                url + f"/documents/{doc['id']}/reprocess", headers=h
            ).status_code
            == 202
        )
        assert (
            client.portal.call(process_document_task, {}, doc["id"])["status"]
            == "ready"
        )
        assert (
            len(db.exec(select(Chunk).where(Chunk.document_id == stored.id)).all())
            == count
        )
    finally:
        assert client.delete(url, headers=h).status_code == 200
    with pytest.raises(Exception) as error:
        get_minio_client().stat_object(settings.S3_BUCKET, key)
    assert "NoSuchKey" in str(error.value)


def test_permissions_and_validation(
    client: TestClient, db: Session, normal_user_token_headers: dict[str, str]
) -> None:
    h = normal_user_token_headers
    other = create_random_knowledge_base(db)
    assert client.get(f"/api/v1/knowledge/{other.id}", headers=h).status_code == 403
    assert (
        client.post(
            "/api/v1/agents/",
            headers=h,
            json={"name": "test", "knowledge_base_ids": [str(other.id)]},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/knowledge/",
            headers=h,
            json={"name": "bad", "chunk_size": 100, "chunk_overlap": 100},
        ).status_code
        == 422
    )
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    kb = create_random_knowledge_base(db, user.id)
    url = f"/api/v1/knowledge/{kb.id}/documents"
    assert (
        client.post(
            url, headers=h, files={"file": ("a.png", b"a", "image/png")}
        ).status_code
        == 415
    )
    assert (
        client.post(
            url,
            headers=h,
            files={"file": ("a.txt", b"x" * (20 * 1024 * 1024 + 1), "text/plain")},
        ).status_code
        == 413
    )


@pytest.mark.usefixtures("fake_embeddings")
def test_failed_ingestion(
    client: TestClient, db: Session, normal_user_token_headers: dict[str, str]
) -> None:
    h = normal_user_token_headers
    kb = client.post("/api/v1/knowledge/", headers=h, json={"name": "failure"}).json()
    url = f"/api/v1/knowledge/{kb['id']}"
    doc = client.post(
        url + "/documents", headers=h, files={"file": ("blank.txt", b"", "text/plain")}
    ).json()
    try:
        assert (
            client.portal.call(process_document_task, {}, doc["id"])["status"]
            == "failed"
        )
        data = client.get(url + "/documents", headers=h).json()["data"][0]
        assert "OCR" in data["error"]
        stored = db.get(Document, uuid.UUID(doc["id"]))
        get_minio_client().remove_object(settings.S3_BUCKET, stored.storage_key)
        assert (
            client.portal.call(process_document_task, {}, doc["id"])["status"]
            == "failed"
        )
    finally:
        client.delete(url, headers=h)


@pytest.mark.usefixtures("fake_embeddings")
@pytest.mark.parametrize("mode", ["sync", "stream", "async"])
def test_rag_execution(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    h = normal_user_token_headers
    kb = client.post("/api/v1/knowledge/", headers=h, json={"name": "RAG"}).json()
    url = f"/api/v1/knowledge/{kb['id']}"
    doc = client.post(
        url + "/documents",
        headers=h,
        files={"file": ("facts.txt", b"Secret 7429", "text/plain")},
    ).json()
    assert client.portal is not None
    try:
        assert (
            client.portal.call(process_document_task, {}, doc["id"])["status"]
            == "ready"
        )
        agent = client.post(
            "/api/v1/agents/",
            headers=h,
            json={"name": "rag", "llm_model": "fake", "knowledge_base_ids": [kb["id"]]},
        ).json()
        version = client.post(
            f"/api/v1/agents/{agent['id']}/versions", headers=h, json={}
        ).json()
        assert version["snapshot"]["knowledge_base_ids"] == [kb["id"]]
        fake_chat_model.messages = iter([AIMessage(content="Secret 7429 [1]")])
        captured: list[Any] = []
        original = GenericFakeChatModel.ainvoke

        async def capture(
            self: GenericFakeChatModel, input: Any, *args: Any, **kwargs: Any
        ) -> Any:
            captured.extend(input)
            return await original(self, input, *args, **kwargs)

        monkeypatch.setattr(GenericFakeChatModel, "ainvoke", capture)
        if mode == "stream":
            conv = client.post(
                "/api/v1/conversations/", headers=h, json={"agent_id": agent["id"]}
            ).json()
            r = client.post(
                f"/api/v1/conversations/{conv['id']}/stream",
                headers=h,
                json={"message": "secret?"},
            )
            assert r.status_code == 200 and "event: context_retrieved" in r.text
            from tests.utils.sse import _parse_sse

            events = _parse_sse(r.text)
            run_id = next(
                e["data"]["run_id"] for e in events if e["event"] == "run_started"
            )
        else:
            r = client.post(
                "/api/v1/runs/" + ("async" if mode == "async" else ""),
                headers=h,
                json={
                    "agent_version_id": version["id"],
                    "input": {"message": "secret?"},
                },
            )
            assert r.status_code in (200, 202), r.text
            run_id = r.json()["id"]
            if mode == "async":
                assert (
                    client.portal.call(execute_run_task, {}, run_id)["status"]
                    == "succeeded"
                )
        rows = db.exec(
            select(RunEvent).where(
                RunEvent.run_id == uuid.UUID(run_id),
                RunEvent.event_type == "context_retrieved",
            )
        ).all()
        assert (
            len(rows) == 1 and rows[0].payload["chunks"][0]["content"] == "Secret 7429"
        )
        assert any(
            isinstance(m, SystemMessage)
            and "Secret 7429" in m.content
            and "do not guess" in m.content
            for m in captured
        )
    finally:
        client.delete(url, headers=h)


def test_storage_queue_failure_and_reaper(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = normal_user_token_headers
    kb = client.post(
        "/api/v1/knowledge/", headers=h, json={"name": "failure checks"}
    ).json()
    url = f"/api/v1/knowledge/{kb['id']}"

    async def broken(**_kwargs: Any) -> None:
        raise RuntimeError("storage down")

    with monkeypatch.context() as patch:
        patch.setattr("app.api.routes.knowledge.put_object", broken)
        assert (
            client.post(
                url + "/documents",
                headers=h,
                files={"file": ("a.txt", b"a", "text/plain")},
            ).status_code
            == 503
        )
        assert client.get(url + "/documents", headers=h).json()["count"] == 0

    async def no_queue() -> None:
        raise RuntimeError("queue down")

    try:
        with monkeypatch.context() as patch:
            patch.setattr("app.api.routes.knowledge.get_arq_pool", no_queue)
            assert (
                client.post(
                    url + "/documents",
                    headers=h,
                    files={"file": ("a.txt", b"a", "text/plain")},
                ).status_code
                == 503
            )
        row = client.get(url + "/documents", headers=h).json()["data"][0]
        assert row["status"] == "failed" and row["error"]
        doc = db.get(Document, uuid.UUID(row["id"]))
        assert doc is not None
        doc.sqlmodel_update(
            {
                "status": DocumentStatus.PROCESSING,
                "started_at": get_datetime_utc() - timedelta(minutes=31),
            }
        )
        db.add(doc)
        db.commit()
        assert client.portal is not None
        assert client.portal.call(reap_stale_documents, {}) >= 1
        db.refresh(doc)
        assert doc.status == DocumentStatus.FAILED and doc.error
    finally:
        client.delete(url, headers=h)
