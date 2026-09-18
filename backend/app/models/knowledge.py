import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

from pgvector.sqlalchemy import Vector
from pydantic import model_validator
from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from app.core.config import settings
from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.user import User


class DocumentStatus(str, enum.Enum):  # noqa: UP042
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class KnowledgeBaseBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    chunk_size: int = Field(default=1000, ge=100, le=4000)
    chunk_overlap: int = Field(default=200, ge=0, le=1000)


class KnowledgeBaseCreate(KnowledgeBaseBase):
    @model_validator(mode="after")
    def validate_overlap(self) -> Self:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        return self


class KnowledgeBaseUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)


class KnowledgeBase(KnowledgeBaseBase, table=True):
    __tablename__ = "knowledge_base"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    embedding_model: str = Field(max_length=128)
    embedding_dim: int
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    owner: "User" = Relationship(back_populates="knowledge_bases")  # noqa: UP037
    documents: list["Document"] = Relationship(  # noqa: UP037
        back_populates="knowledge_base", cascade_delete=True
    )


class KnowledgeBasePublic(KnowledgeBaseBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    embedding_model: str
    embedding_dim: int
    created_at: datetime
    document_count: int = 0
    ready_document_count: int = 0


class KnowledgeBasesPublic(SQLModel):
    data: list[KnowledgeBasePublic]
    count: int


class Document(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    knowledge_base_id: uuid.UUID = Field(
        foreign_key="knowledge_base.id", ondelete="CASCADE", index=True
    )
    filename: str = Field(max_length=512)
    storage_key: str = Field(max_length=1024)
    mime_type: str = Field(max_length=128)
    size_bytes: int
    status: DocumentStatus = Field(
        default=DocumentStatus.PENDING,
        sa_type=String(32),  # type: ignore
        index=True,
    )
    error: str | None = Field(default=None, max_length=2000)
    chunk_count: int = 0
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore
    processed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore
    knowledge_base: KnowledgeBase = Relationship(back_populates="documents")
    chunks: list["Chunk"] = Relationship(back_populates="document", cascade_delete=True)  # noqa: UP037


class DocumentPublic(SQLModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    error: str | None
    chunk_count: int
    created_at: datetime
    processed_at: datetime | None


class DocumentsPublic(SQLModel):
    data: list[DocumentPublic]
    count: int


class Chunk(SQLModel, table=True):
    __table_args__ = (
        Index(
            "chunk_embedding_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    document_id: uuid.UUID = Field(
        foreign_key="document.id", ondelete="CASCADE", index=True
    )
    knowledge_base_id: uuid.UUID = Field(
        foreign_key="knowledge_base.id", ondelete="CASCADE", index=True
    )
    seq: int
    content: str = Field(max_length=4000)
    embedding: Any = Field(sa_type=Vector(settings.EMBEDDING_DIM))  # type: ignore
    metadata_: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    document: Document = Relationship(back_populates="chunks")


class SearchRequest(SQLModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


class SearchResultPublic(SQLModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    seq: int
    content: str
    score: float


class SearchResultsPublic(SQLModel):
    data: list[SearchResultPublic]
    count: int
