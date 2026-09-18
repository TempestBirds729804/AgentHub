import asyncio
import logging
import uuid
from datetime import timedelta
from functools import partial
from typing import Any

from anyio import to_thread
from sqlalchemy import delete, text
from sqlmodel import col, select

from app.agent.ingestion import embed_chunks, extract_text, split_text
from app.core.async_db import async_engine, async_session_maker
from app.core.config import settings
from app.core.redis import get_redis
from app.core.storage import get_object
from app.models import (
    Chunk,
    Document,
    DocumentStatus,
    KnowledgeBase,
    Run,
    RunEventType,
    RunStatus,
    get_datetime_utc,
)
from app.services.event_bus import TERMINAL_STATUSES, RedisEventPublisher
from app.services.rate_limit import release_run_slot, try_acquire_run_slot
from app.services.run_service import (
    RunEventRecorder,
    execute_run_with_checkpoint,
    resume_run_after_approval,
)
from app.worker.settings import STALE_RUN_THRESHOLD_SECONDS

logger = logging.getLogger(__name__)


def run_lock_key(run_id: uuid.UUID) -> int:
    """A transaction advisory lock prevents concurrent executions of one Run."""
    return int.from_bytes(run_id.bytes[:8], "big", signed=True)


def run_job_id(run: Run) -> str:
    return f"agenthub:run:{run.id}:attempt:{run.retry_count}"


def resume_job_id(run: Run) -> str:
    return f"{run_job_id(run)}:approval:{run.checkpoint_id}"


async def resume_run_task(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Resume approved/rejected calls in the worker with the same execution lock."""
    return await execute_run_task({**ctx, "approval_resume": True}, run_id)


async def execute_run_task(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Reload a Run and its tools; only the ID crosses the queue boundary."""
    run_uuid = uuid.UUID(run_id)
    # Dedicated transaction: commits in the execution Session cannot release this
    # lock. PostgreSQL releases it if the worker process/connection dies.
    async with async_engine.begin() as connection:
        if ctx.get("approval_resume"):
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": run_lock_key(run_uuid)},
            )
        locked = await connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": run_lock_key(run_uuid)},
        )
        if not locked:
            return {"status": "already_running"}
        async with async_session_maker() as session:
            run = await session.get(Run, run_uuid)
            if run is None:
                return {"status": "not_found"}
            expected_job_id = run_job_id(run)
            job_id = ctx.get("job_id")
            # A resumed job may be redelivered after its checkpoint advanced.
            # The retry attempt is stable; the current checkpoint is not.
            obsolete = job_id is not None and (
                not str(job_id).startswith(f"{expected_job_id}:approval:")
                if ctx.get("approval_resume")
                else job_id != expected_job_id
            )
            if obsolete:
                return {"status": "obsolete"}
            if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                return {"status": str(run.status)}
            owner_id = run.owner_id
            redis = get_redis()
            publisher = RedisEventPublisher(redis=redis, run_id=run_uuid)
            try:
                if not await try_acquire_run_slot(
                    redis=redis, user_id=owner_id, run_id=run_uuid
                ):
                    raise RuntimeError("Concurrent run limit reached during recovery")
                execute = (
                    resume_run_after_approval
                    if ctx.get("approval_resume")
                    else execute_run_with_checkpoint
                )
                await execute(session=session, run=run, publisher=publisher)
            except asyncio.CancelledError:
                # arq redelivers on shutdown; keep RUNNING with checkpoint intact.
                # Reaper handles a second interruption or a hard process kill.
                raise
            except Exception as exc:
                await session.rollback()
                await session.refresh(run)
                if run.status not in TERMINAL_STATUSES:
                    recorder = RunEventRecorder(session=session, run_id=run_uuid)
                    await recorder.resync_seq()
                    run.sqlmodel_update(
                        {
                            "status": RunStatus.FAILED,
                            "error": (str(exc) or type(exc).__name__)[:4000],
                            "finished_at": get_datetime_utc(),
                        }
                    )
                    session.add(run)
                    await recorder.record(
                        RunEventType.RUN_FAILED, payload={"error": run.error}
                    )
                    await session.commit()
                    await session.refresh(run)
                logger.exception("Run %s failed", run_uuid)
            finally:
                try:
                    await publisher.close()
                except Exception:
                    logger.warning(
                        "Failed to close Run %s stream; TTL will expire", run_uuid
                    )
                try:
                    await release_run_slot(
                        redis=redis, user_id=owner_id, run_id=run_uuid
                    )
                except Exception:
                    logger.warning(
                        "Failed to release Run %s slot; TTL will expire", run_uuid
                    )
            return {"status": run.status}


async def reap_stale_runs(_ctx: dict[str, Any]) -> int:
    """Fail interrupted runs after the worker timeout plus a five-minute buffer."""
    cutoff = get_datetime_utc() - timedelta(seconds=STALE_RUN_THRESHOLD_SECONDS)
    async with async_session_maker() as session:
        runs = list(
            (
                await session.execute(
                    select(Run)
                    .where(
                        Run.status == RunStatus.RUNNING, col(Run.started_at) < cutoff
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for run in runs:
            recorder = RunEventRecorder(session=session, run_id=run.id)
            await recorder.resync_seq()
            run.sqlmodel_update(
                {
                    "status": RunStatus.FAILED,
                    "error": "Run was interrupted and did not resume in time",
                    "finished_at": get_datetime_utc(),
                }
            )
            session.add(run)
            await recorder.record(RunEventType.RUN_FAILED, payload={"error": run.error})
        await session.commit()
        for run in runs:
            await session.refresh(run)
            await release_run_slot(
                redis=get_redis(), user_id=run.owner_id, run_id=run.id
            )
            publisher = RedisEventPublisher(redis=get_redis(), run_id=run.id)
            await publisher.publish(
                RunEventType.RUN_FAILED, {"run_id": str(run.id), "error": run.error}
            )
            await publisher.close()
    return len(runs)


async def process_document_task(
    _ctx: dict[str, Any], document_id: str
) -> dict[str, Any]:
    """Claim once, parse in a thread, and atomically replace all document chunks."""
    doc_id = uuid.UUID(document_id)
    async with async_session_maker() as session:
        doc = await session.get(Document, doc_id, with_for_update=True)
        if doc is None:
            return {"status": "not_found"}
        if doc.status in (DocumentStatus.READY, DocumentStatus.PROCESSING):
            return {
                "status": "already_ready"
                if doc.status == DocumentStatus.READY
                else "already_processing"
            }
        kb = await session.get(KnowledgeBase, doc.knowledge_base_id)
        assert kb is not None
        started_at = get_datetime_utc()
        doc.sqlmodel_update(
            {
                "status": DocumentStatus.PROCESSING,
                "started_at": started_at,
                "error": None,
                "chunk_count": 0,
                "processed_at": None,
            }
        )
        session.add(doc)
        await session.commit()
        try:
            if (
                kb.embedding_model != settings.EMBEDDING_MODEL
                or kb.embedding_dim != settings.EMBEDDING_DIM
            ):
                raise ValueError(
                    "Embedding configuration changed; rebuild the knowledge base"
                )
            data = await get_object(key=doc.storage_key)
            content = await to_thread.run_sync(
                partial(
                    extract_text,
                    data=data,
                    mime_type=doc.mime_type,
                    filename=doc.filename,
                )
            )
            pieces = await to_thread.run_sync(
                partial(
                    split_text,
                    text=content,
                    chunk_size=kb.chunk_size,
                    chunk_overlap=kb.chunk_overlap,
                )
            )
            vectors = await embed_chunks(pieces)
            # A deleted or reaped document must not be resurrected by a late response.
            doc = await session.get(
                Document, doc_id, with_for_update=True, populate_existing=True
            )
            if (
                doc is None
                or doc.status != DocumentStatus.PROCESSING
                or doc.started_at != started_at
            ):
                return {"status": "discarded"}
            await session.execute(delete(Chunk).where(col(Chunk.document_id) == doc_id))
            for seq, (piece, vector) in enumerate(zip(pieces, vectors, strict=True)):
                session.add(
                    Chunk(
                        document_id=doc_id,
                        knowledge_base_id=doc.knowledge_base_id,
                        seq=seq,
                        content=piece,
                        embedding=vector,
                        metadata_={"filename": doc.filename},
                    )
                )
            doc.sqlmodel_update(
                {
                    "status": DocumentStatus.READY,
                    "chunk_count": len(pieces),
                    "processed_at": get_datetime_utc(),
                }
            )
            session.add(doc)
            await session.commit()
            return {"status": "ready", "chunks": len(pieces)}
        except Exception as exc:
            await session.rollback()
            doc = await session.get(
                Document, doc_id, with_for_update=True, populate_existing=True
            )
            if (
                doc is not None
                and doc.status == DocumentStatus.PROCESSING
                and doc.started_at == started_at
            ):
                await session.execute(
                    delete(Chunk).where(col(Chunk.document_id) == doc_id)
                )
                doc.sqlmodel_update(
                    {
                        "status": DocumentStatus.FAILED,
                        "error": (str(exc) or type(exc).__name__)[:2000],
                        "chunk_count": 0,
                    }
                )
                session.add(doc)
                await session.commit()
            logger.warning(
                "Document %s processing failed: %s", document_id, type(exc).__name__
            )
            return {"status": "failed"}


async def reap_stale_documents(_ctx: dict[str, Any]) -> int:
    cutoff = get_datetime_utc() - timedelta(minutes=30)
    async with async_session_maker() as session:
        documents = (
            (
                await session.execute(
                    select(Document)
                    .where(
                        Document.status == DocumentStatus.PROCESSING,
                        col(Document.started_at) < cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for doc in documents:
            doc.sqlmodel_update(
                {
                    "status": DocumentStatus.FAILED,
                    "error": "Document processing was interrupted; reprocess to retry",
                    "chunk_count": 0,
                }
            )
            session.add(doc)
        await session.commit()
        return len(documents)
