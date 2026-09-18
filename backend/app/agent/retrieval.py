import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.agent.exceptions import EmbeddingMismatchError
from app.agent.ingestion import get_embeddings
from app.core.config import settings
from app.models import (
    Chunk,
    Document,
    DocumentStatus,
    KnowledgeBase,
    SearchResultPublic,
)

DEFAULT_TOP_K = 5
DEFAULT_SCORE_THRESHOLD = 0.35


async def assert_embedding_compatible(
    *, session: AsyncSession, knowledge_base_ids: list[uuid.UUID]
) -> None:
    bases = (
        (
            await session.execute(
                select(KnowledgeBase).where(
                    col(KnowledgeBase.id).in_(knowledge_base_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    for kb in bases:
        if (
            kb.embedding_model != settings.EMBEDDING_MODEL
            or kb.embedding_dim != settings.EMBEDDING_DIM
        ):
            raise EmbeddingMismatchError(
                f"Knowledge base {kb.id} embedding configuration changed. Rebuild the knowledge base."
            )


async def retrieve(
    *,
    session: AsyncSession,
    knowledge_base_ids: list[uuid.UUID],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
) -> list[SearchResultPublic]:
    if not knowledge_base_ids:
        return []
    await assert_embedding_compatible(
        session=session, knowledge_base_ids=knowledge_base_ids
    )
    vector = await get_embeddings().aembed_query(query)
    # Owner/knowledge filters and dead tuples can exhaust HNSW's first candidate
    # set; pgvector 0.8+ expands it while preserving distance order.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    # Match HNSW vector_cosine_ops: order by raw cosine distance, filter afterwards.
    distance = Chunk.embedding.cosine_distance(vector).label("distance")
    rows = (
        await session.execute(
            select(Chunk, Document.filename, distance)
            .join(Document, col(Document.id) == Chunk.document_id)
            .where(
                col(Chunk.knowledge_base_id).in_(knowledge_base_ids),
                Document.status == DocumentStatus.READY,
            )
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [
        SearchResultPublic(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            filename=filename,
            seq=chunk.seq,
            content=chunk.content,
            score=1.0 - float(dist),
        )
        for chunk, filename, dist in rows
        if 1.0 - float(dist) >= score_threshold
    ]
