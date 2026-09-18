import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from sqlalchemy import case, delete
from sqlmodel import col, func, select

from app.agent.exceptions import EmbeddingMismatchError
from app.agent.ingestion import MAX_DOCUMENT_SIZE_BYTES, SUPPORTED_MIME_TYPES
from app.agent.retrieval import retrieve
from app.api.deps import AsyncSessionDep, CurrentUser, SessionDep
from app.core.storage import delete_object, document_key, put_object
from app.crud.knowledge import create_knowledge_base, update_knowledge_base
from app.models import (
    Document,
    DocumentPublic,
    DocumentsPublic,
    DocumentStatus,
    KnowledgeBase,
    KnowledgeBaseCreate,
    KnowledgeBasePublic,
    KnowledgeBasesPublic,
    KnowledgeBaseUpdate,
    Message,
    SearchRequest,
    SearchResultsPublic,
)
from app.worker.settings import get_arq_pool

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
logger = logging.getLogger(__name__)


def _check_owner(kb: KnowledgeBase | None, current_user: CurrentUser) -> KnowledgeBase:
    if kb is None:
        raise HTTPException(404, "Knowledge base not found")
    if not current_user.is_superuser and kb.owner_id != current_user.id:
        raise HTTPException(403, "Not enough permissions")
    return kb


def _public(
    session: SessionDep, bases: list[KnowledgeBase]
) -> list[KnowledgeBasePublic]:
    counts = {
        key: (total, ready)
        for key, total, ready in session.exec(
            select(
                Document.knowledge_base_id,
                func.count(),
                func.sum(
                    case((col(Document.status) == DocumentStatus.READY, 1), else_=0)
                ),
            )
            .where(col(Document.knowledge_base_id).in_([kb.id for kb in bases]))
            .group_by(col(Document.knowledge_base_id))
        ).all()
    }
    return [
        KnowledgeBasePublic.model_validate(
            kb,
            update={
                "document_count": counts.get(kb.id, (0, 0))[0],
                "ready_document_count": counts.get(kb.id, (0, 0))[1],
            },
        )
        for kb in bases
    ]


@router.get("/", response_model=KnowledgeBasesPublic)
def read_knowledge_bases(
    session: SessionDep,
    current_user: CurrentUser,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Any:
    """Retrieve knowledge bases."""
    statement = select(KnowledgeBase)
    count = select(func.count()).select_from(KnowledgeBase)
    if not current_user.is_superuser:
        statement = statement.where(KnowledgeBase.owner_id == current_user.id)
        count = count.where(KnowledgeBase.owner_id == current_user.id)
    bases = list(
        session.exec(
            statement.order_by(col(KnowledgeBase.created_at).desc())
            .offset(skip)
            .limit(limit)
        ).all()
    )
    return KnowledgeBasesPublic(
        data=_public(session, bases), count=session.exec(count).one()
    )


@router.post("/", response_model=KnowledgeBasePublic)
def create_knowledge(
    *, session: SessionDep, current_user: CurrentUser, knowledge_in: KnowledgeBaseCreate
) -> Any:
    """Create a knowledge base with the current embedding configuration."""
    return create_knowledge_base(
        session=session, knowledge_in=knowledge_in, owner_id=current_user.id
    )


@router.get("/{id}", response_model=KnowledgeBasePublic)
def read_knowledge(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """Get a knowledge base by ID."""
    return _public(
        session, [_check_owner(session.get(KnowledgeBase, id), current_user)]
    )[0]


@router.patch("/{id}", response_model=KnowledgeBasePublic)
def update_knowledge(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    knowledge_in: KnowledgeBaseUpdate,
) -> Any:
    """Update a knowledge base name or description."""
    kb = _check_owner(session.get(KnowledgeBase, id), current_user)
    if "name" in knowledge_in.model_fields_set and knowledge_in.name is None:
        raise HTTPException(422, "name cannot be null")
    return _public(
        session,
        [
            update_knowledge_base(
                session=session, db_knowledge=kb, knowledge_in=knowledge_in
            )
        ],
    )[0]


async def _remove_objects(keys: list[str]) -> None:
    for key in keys:
        try:
            await delete_object(key=key)
        except Exception:
            logger.warning("Failed to delete document object %s", key)


@router.delete("/{id}", response_model=Message)
async def delete_knowledge(
    session: AsyncSessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """Delete a knowledge base and its stored documents."""
    _check_owner(
        await session.get(KnowledgeBase, id, with_for_update=True), current_user
    )
    keys = list(
        (
            await session.execute(
                select(Document.storage_key).where(Document.knowledge_base_id == id)
            )
        ).scalars()
    )
    await session.execute(delete(KnowledgeBase).where(col(KnowledgeBase.id) == id))
    await session.commit()
    await _remove_objects(keys)
    return Message(message="Knowledge base deleted successfully")


@router.get("/{id}/documents", response_model=DocumentsPublic)
def read_documents(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Any:
    """Retrieve documents in a knowledge base."""
    _check_owner(session.get(KnowledgeBase, id), current_user)
    count = session.exec(
        select(func.count())
        .select_from(Document)
        .where(Document.knowledge_base_id == id)
    ).one()
    docs = session.exec(
        select(Document)
        .where(Document.knowledge_base_id == id)
        .order_by(col(Document.created_at).desc())
        .offset(skip)
        .limit(limit)
    ).all()
    return DocumentsPublic(
        data=[DocumentPublic.model_validate(doc) for doc in docs], count=count
    )


async def _enqueue(session: AsyncSessionDep, doc: Document) -> None:
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job("process_document_task", str(doc.id))
    except Exception:
        doc.sqlmodel_update(
            {
                "status": DocumentStatus.FAILED,
                "error": "Document queue unavailable; reprocess to retry",
            }
        )
        session.add(doc)
        await session.commit()
        raise HTTPException(503, "Document queue unavailable")


@router.post("/{id}/documents", response_model=DocumentPublic, status_code=202)
async def upload_document(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    file: Annotated[UploadFile, File()],
) -> Any:
    """Upload a document for background ingestion."""
    kb = _check_owner(
        await session.get(KnowledgeBase, id, with_for_update=True), current_user
    )
    mime = (file.content_type or "").split(";")[0].lower()
    if mime not in SUPPORTED_MIME_TYPES:
        raise HTTPException(415, "Unsupported file type")
    if file.size is not None and file.size > MAX_DOCUMENT_SIZE_BYTES:
        raise HTTPException(413, "File too large")
    data = await file.read(MAX_DOCUMENT_SIZE_BYTES + 1)
    if len(data) > MAX_DOCUMENT_SIZE_BYTES:
        raise HTTPException(413, "File too large")
    filename = file.filename or "document"
    if len(filename) > 512:
        raise HTTPException(422, "Filename too long")
    doc_id = uuid.uuid4()
    key = document_key(owner_id=kb.owner_id, document_id=doc_id, filename=filename)
    doc = Document(
        id=doc_id,
        knowledge_base_id=id,
        filename=filename,
        storage_key=key,
        mime_type=mime,
        size_bytes=len(data),
    )
    try:
        await put_object(key=key, data=data, content_type=mime)
        session.add(doc)
        await session.commit()
    except Exception:
        await session.rollback()
        await _remove_objects([key])
        raise HTTPException(503, "Document storage unavailable")
    await _enqueue(session, doc)
    await session.refresh(doc)
    return doc


async def _document(
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    doc_id: uuid.UUID,
) -> Document:
    _check_owner(await session.get(KnowledgeBase, id), current_user)
    doc = await session.get(Document, doc_id, with_for_update=True)
    if doc is None or doc.knowledge_base_id != id:
        raise HTTPException(404, "Document not found")
    return doc


@router.delete("/{id}/documents/{doc_id}", response_model=Message)
async def delete_document(
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    doc_id: uuid.UUID,
) -> Message:
    """Delete a document and its stored object."""
    doc = await _document(session, current_user, id, doc_id)
    key = doc.storage_key
    await session.execute(delete(Document).where(col(Document.id) == doc_id))
    await session.commit()
    await _remove_objects([key])
    return Message(message="Document deleted successfully")


@router.post(
    "/{id}/documents/{doc_id}/reprocess", response_model=DocumentPublic, status_code=202
)
async def reprocess_document(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    doc_id: uuid.UUID,
) -> Any:
    """Reprocess a failed document."""
    doc = await _document(session, current_user, id, doc_id)
    if doc.status != DocumentStatus.FAILED:
        raise HTTPException(409, "Only failed documents can be reprocessed")
    doc.sqlmodel_update(
        {"status": DocumentStatus.PENDING, "error": None, "started_at": None}
    )
    session.add(doc)
    await session.commit()
    await _enqueue(session, doc)
    await session.refresh(doc)
    return doc


@router.post("/{id}/search", response_model=SearchResultsPublic)
async def search_knowledge(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    search_in: SearchRequest,
) -> Any:
    """Search a knowledge base by semantic similarity."""
    _check_owner(await session.get(KnowledgeBase, id), current_user)
    try:
        rows = await retrieve(
            session=session,
            knowledge_base_ids=[id],
            query=search_in.query,
            top_k=search_in.top_k,
        )
    except EmbeddingMismatchError as exc:
        raise HTTPException(409, str(exc))
    except Exception:
        raise HTTPException(503, "Knowledge search unavailable")
    return SearchResultsPublic(data=rows, count=len(rows))
