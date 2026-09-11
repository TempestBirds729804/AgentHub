# 08 — 知识库与向量检索

## 前置依赖

阶段 [06-async-worker.md](06-async-worker.md) 的验收清单全部通过。特别确认：

- arq worker 可用（文档处理是后台任务）
- MinIO 服务在跑且控制台能登录（阶段 01 建的）
- `vector` 扩展已启用（阶段 01 的迁移）

本阶段与 07 / 09 / 10 相互独立。

## 本阶段目标

给 Agent 接上私有知识：

- `KnowledgeBase` / `Document` / `Chunk` 三张表，Chunk 上带 pgvector 向量列
- 文档上传到 MinIO，后台 worker 做解析、切片、向量化
- LangGraph 里的检索节点
- 回复里标注答案来源

## 本阶段不做什么

不做重排序（rerank）、不做混合检索（BM25 + 向量）、不做多模态文档、不做增量更新（文档改了就重新上传）。这些都是可以无限深入的方向，第一版做通基础链路即可。

---

## 任务 1：依赖与对象存储

### 1.1 装依赖

```bash
cd backend
uv add "minio" "pypdf" "langchain-text-splitters"
```

说明：

- `minio` 是 MinIO 官方 Python 客户端，也兼容 S3。它是**同步**的，需要在异步代码里用线程池包一层（见 1.2）。选它而不是 `aioboto3` 是因为 API 简单得多，而对象存储的调用量不大
- `pypdf` 解析 PDF。Word 文档需要 `python-docx`，如果只支持 PDF 和纯文本就不用装
- `langchain-text-splitters` 提供 `RecursiveCharacterTextSplitter`，比自己写切片逻辑可靠

Embedding 用 `langchain-openai` 的 `OpenAIEmbeddings`，已经在阶段 01 装了。

### 1.2 存储客户端

新建 `backend/app/core/storage.py`：

```python
from functools import lru_cache
from urllib.parse import urlparse

from anyio import to_thread
from minio import Minio

from app.core.config import settings


@lru_cache(maxsize=1)
def get_minio_client() -> Minio:
    parsed = urlparse(settings.S3_ENDPOINT)
    return Minio(
        f"{parsed.hostname}:{parsed.port or (443 if settings.S3_SECURE else 80)}",
        access_key=settings.S3_ACCESS_KEY,
        secret_key=settings.S3_SECRET_KEY,
        secure=settings.S3_SECURE,
    )


async def ensure_bucket() -> None:
    """确保 bucket 存在。在 prestart 里调一次。"""
    client = get_minio_client()
    exists = await to_thread.run_sync(client.bucket_exists, settings.S3_BUCKET)
    if not exists:
        await to_thread.run_sync(client.make_bucket, settings.S3_BUCKET)


async def put_object(*, key: str, data: bytes, content_type: str) -> None:
    client = get_minio_client()
    await to_thread.run_sync(
        lambda: client.put_object(
            settings.S3_BUCKET,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
    )


async def get_object(*, key: str) -> bytes:
    client = get_minio_client()

    def _read() -> bytes:
        response = client.get_object(settings.S3_BUCKET, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    return await to_thread.run_sync(_read)


async def delete_object(*, key: str) -> None:
    client = get_minio_client()
    await to_thread.run_sync(client.remove_object, settings.S3_BUCKET, key)
```

要点：

- `anyio.to_thread.run_sync` 把同步调用放到线程池，不阻塞事件循环。FastAPI 底层就是 anyio，直接用它不需要额外依赖
- `get_object` 的 `finally` 里必须 `close()` 和 `release_conn()`。漏了会泄漏连接，几百次请求后 MinIO 客户端就卡死。这是 minio 库最常见的误用
- `S3_ENDPOINT` 配置里带 `http://` 前缀，但 `Minio()` 要的是不带 scheme 的 `host:port`，所以要解析

在 `prestart.sh` 里加一步建 bucket。新建 `backend/app/setup_storage.py`（结构参照阶段 06 的 `setup_checkpointer.py`），然后在 `prestart.sh` 里调。

### 1.3 对象 key 规范

```python
def document_key(*, owner_id: uuid.UUID, document_id: uuid.UUID, filename: str) -> str:
    """
    对象 key 带上 owner_id 前缀，便于按用户统计用量和批量清理。
    文件名只保留扩展名，避免用户提供的文件名里有路径穿越字符。
    """
    ext = Path(filename).suffix.lower()[:10]
    return f"documents/{owner_id}/{document_id}{ext}"
```

**不要把用户提供的文件名直接拼进 key**。`../../etc/passwd` 这种输入在某些 S3 实现上会产生意外行为。只取扩展名，原始文件名存数据库。

---

## 任务 2：数据模型

新建 `backend/app/models/knowledge.py`：

```python
import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from app.core.config import settings
from app.models.base import get_datetime_utc


class DocumentStatus(str, enum.Enum):
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
    pass


class KnowledgeBaseUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    # chunk_size 和 chunk_overlap 不可改：改了已有切片就与新切片不一致。
    # 需要改就新建知识库。


class KnowledgeBase(KnowledgeBaseBase, table=True):
    __tablename__ = "knowledge_base"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True
    )
    # 记录建库时使用的 embedding 模型和维度。换模型需要重建整个库。
    embedding_model: str = Field(max_length=128)
    embedding_dim: int

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    documents: list["Document"] = Relationship(
        back_populates="knowledge_base", cascade_delete=True
    )


class Document(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    knowledge_base_id: uuid.UUID = Field(
        foreign_key="knowledge_base.id", nullable=False, ondelete="CASCADE", index=True
    )
    filename: str = Field(max_length=512)
    storage_key: str = Field(max_length=1024)
    mime_type: str = Field(max_length=128)
    size_bytes: int
    status: DocumentStatus = Field(default=DocumentStatus.PENDING, index=True)
    error: str | None = Field(default=None, max_length=2000)
    chunk_count: int = 0

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    processed_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)  # type: ignore
    )

    knowledge_base: KnowledgeBase | None = Relationship(back_populates="documents")
    chunks: list["Chunk"] = Relationship(
        back_populates="document", cascade_delete=True
    )


class Chunk(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    document_id: uuid.UUID = Field(
        foreign_key="document.id", nullable=False, ondelete="CASCADE", index=True
    )
    # 冗余存 kb_id，检索时不用 join document
    knowledge_base_id: uuid.UUID = Field(
        foreign_key="knowledge_base.id", nullable=False, ondelete="CASCADE", index=True
    )
    seq: int
    content: str
    embedding: Any = Field(sa_type=Vector(settings.EMBEDDING_DIM))
    metadata_: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    document: Document | None = Relationship(back_populates="chunks")
```

### 几个关键决策

**`embedding` 字段标注为 `Any`**。`Vector` 不是 Python 类型，mypy strict 下没法给出准确标注。`Any` + `sa_type=Vector(...)` 是可行的写法。取值时是 numpy array 或 list，赋值时给 list。

**向量维度来自 `settings.EMBEDDING_DIM`**（阶段 01 定的 1536）。这意味着**维度在建表时就固定了**，换 embedding 模型（维度不同）需要新的迁移改列类型并重算所有向量。所以 `KnowledgeBase` 上记录了 `embedding_model` 和 `embedding_dim`，检索时校验一致性，不一致就明确报错而不是给出错误结果。

**`metadata_` 带下划线后缀**。`metadata` 是 SQLAlchemy `DeclarativeBase` 的保留属性名，直接用会报错。列名可以通过 `sa_column_kwargs` 指定为 `metadata`，但为了简单直接让列名也叫 `metadata_`。

**`chunk_size` 和 `chunk_overlap` 不可改**。改了之后新文档的切片粒度和老文档不一致，检索结果质量会诡异地下降且难以排查。`KnowledgeBaseUpdate` 里刻意不包含它们。

**`Chunk.knowledge_base_id` 是冗余字段**。检索是最热的路径，省掉一次 join 值得。代价是写入时要保证两个 id 一致（在 worker 里从 document 读出来填）。

更新 `models/__init__.py`，`conftest.py` teardown 元组加 `(Chunk, Document, KnowledgeBase)`，顺序不能错。

---

## 任务 3：迁移与向量索引

### 3.1 生成迁移

```bash
cd backend
uv run alembic revision --autogenerate -m "Add knowledge base document and chunk models"
```

**pgvector 列的 autogenerate 几乎肯定需要手工修**。检查生成的文件：

- `embedding` 列的类型应该是 `Vector(dim=1536)`
- 文件顶部**必须**有 `from pgvector.sqlalchemy import Vector`。autogenerate 经常不加这个 import，导致迁移一跑就 NameError
- 如果生成的类型是 `sa.NullType()` 或类似的占位符，手工改成 `Vector(1536)`

### 3.2 手工加向量索引

autogenerate **不会**生成向量索引，必须手工加。在同一个迁移文件的 `upgrade()` 末尾：

```python
    # HNSW 索引，用于余弦距离的近似最近邻搜索。
    # 必须在表建好之后创建。
    op.execute(
        "CREATE INDEX chunk_embedding_hnsw_idx ON chunk "
        "USING hnsw (embedding vector_cosine_ops)"
    )
```

`downgrade()` 里对应：

```python
    op.execute("DROP INDEX IF EXISTS chunk_embedding_hnsw_idx")
```

关于索引选择：

- **HNSW** 查询快、精度高，但建索引慢、占内存多
- **IVFFlat** 建索引快，但需要先有数据才能确定聚类中心，空表上建的索引效果差

选 HNSW。它可以在空表上建，后续插入数据会自动维护索引。IVFFlat 在空表上建完全没用，必须等数据灌完再建，流程复杂。

**距离算子必须与查询时用的一致**。索引用 `vector_cosine_ops`，查询就必须用余弦距离（`<=>` 或 SQLAlchemy 的 `cosine_distance`）。用了 L2 距离查询，这个索引不会被使用，会退化成全表扫描。这是最隐蔽的性能坑，在代码注释里写明。

### 3.3 验证

```bash
uv run alembic upgrade head
uv run alembic downgrade -1 && uv run alembic upgrade head
uv run alembic check

docker compose exec db psql -U postgres -d app -c "\d chunk"
# embedding 列类型是 vector(1536)

docker compose exec db psql -U postgres -d app -c "\di chunk*"
# 能看到 chunk_embedding_hnsw_idx
```

---

## 任务 4：文档处理流水线

### 4.1 解析

新建 `backend/app/agent/ingestion.py`：

```python
SUPPORTED_MIME_TYPES = {
    "application/pdf": "pdf",
    "text/plain": "text",
    "text/markdown": "text",
}

MAX_DOCUMENT_SIZE_BYTES = 20 * 1024 * 1024      # 20 MB
MAX_EXTRACTED_CHARS = 2_000_000                  # 约 500k tokens


def extract_text(*, data: bytes, mime_type: str, filename: str) -> str:
    kind = SUPPORTED_MIME_TYPES.get(mime_type)
    if kind is None:
        raise ValueError(f"Unsupported file type: {mime_type}")

    if kind == "pdf":
        text = _extract_pdf(data)
    else:
        text = data.decode("utf-8", errors="replace")

    if not text.strip():
        raise ValueError(
            "No text could be extracted. Scanned PDFs without OCR are not supported."
        )
    if len(text) > MAX_EXTRACTED_CHARS:
        raise ValueError(
            f"Document too large: {len(text)} chars, limit is {MAX_EXTRACTED_CHARS}"
        )
    return text


def _extract_pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages)
```

要点：

- **扫描版 PDF 提取不出文字**，会得到空字符串。必须显式检测并给出有意义的错误信息，不然用户会困惑为什么上传成功但检索不到。OCR 超出本项目范围
- MIME 类型白名单校验，不要靠文件扩展名
- 大小上限双重限制：上传时按字节数，解析后按字符数

### 4.2 切片

```python
from langchain_text_splitters import RecursiveCharacterTextSplitter


def split_text(*, text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """
    递归切分。优先在段落边界切，其次句子，最后才硬切字符。
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", ". ", " ", ""],
    )
    return [c for c in splitter.split_text(text) if c.strip()]
```

`separators` 里加了中文句号 `。`，默认的分隔符列表对中文文档效果很差（会在英文句号和空格处切，中文文本里几乎没有这些）。这个细节对中文知识库的检索质量影响很大。

### 4.3 Embedding

```python
from langchain_openai import OpenAIEmbeddings

EMBEDDING_BATCH_SIZE = 64


@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=settings.EMBEDDING_MODEL,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        dimensions=settings.EMBEDDING_DIM,
    )


async def embed_chunks(texts: list[str]) -> list[list[float]]:
    """分批向量化。一次请求塞太多会超出 API 的输入上限。"""
    embeddings = get_embeddings()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i : i + EMBEDDING_BATCH_SIZE]
        vectors.extend(await embeddings.aembed_documents(batch))
    return vectors
```

`dimensions` 参数只有部分模型支持（`text-embedding-3-*` 系列支持，老的 `ada-002` 不支持）。如果用的模型不支持，去掉这个参数并确保 `EMBEDDING_DIM` 与模型的原生维度一致。

分批是必需的。OpenAI 的 embedding 接口对单次请求的总 token 数有限制，一次塞几百个 chunk 会报 400。

### 4.4 Worker 任务

`backend/app/worker/tasks.py` 新增：

```python
async def process_document_task(ctx: dict[str, Any], document_id: str) -> dict[str, Any]:
    """
    解析、切片、向量化一个文档。

    幂等：已经是 ready 的文档跳过。重跑时先删掉旧 chunk 再重建。
    """
    doc_uuid = uuid.UUID(document_id)
    async with async_session_maker() as session:
        doc = await session.get(Document, doc_uuid)
        if doc is None:
            return {"status": "not_found"}
        if doc.status == DocumentStatus.READY:
            return {"status": "already_ready"}

        kb = await session.get(KnowledgeBase, doc.knowledge_base_id)
        doc.status = DocumentStatus.PROCESSING
        doc.error = None
        session.add(doc)
        await session.commit()

        try:
            # 重跑时清掉旧数据，避免重复 chunk
            await session.execute(delete(Chunk).where(Chunk.document_id == doc_uuid))
            await session.commit()

            data = await get_object(key=doc.storage_key)
            text = extract_text(
                data=data, mime_type=doc.mime_type, filename=doc.filename
            )
            pieces = split_text(
                text=text,
                chunk_size=kb.chunk_size,
                chunk_overlap=kb.chunk_overlap,
            )
            vectors = await embed_chunks(pieces)

            for seq, (content, vector) in enumerate(zip(pieces, vectors, strict=True)):
                session.add(
                    Chunk(
                        document_id=doc.id,
                        knowledge_base_id=doc.knowledge_base_id,
                        seq=seq,
                        content=content,
                        embedding=vector,
                        metadata_={"filename": doc.filename},
                    )
                )

            doc.status = DocumentStatus.READY
            doc.chunk_count = len(pieces)
            doc.processed_at = get_datetime_utc()
            session.add(doc)
            await session.commit()
            return {"status": "ready", "chunks": len(pieces)}

        except Exception as exc:
            await session.rollback()
            doc.status = DocumentStatus.FAILED
            doc.error = str(exc)[:2000]
            session.add(doc)
            await session.commit()
            logger.exception("Failed to process document %s", document_id)
            return {"status": "failed"}
```

要点：

- **失败必须落 `FAILED` 状态并记录错误**，不能让文档永远停在 `processing`。`except` 里先 `rollback` 再写状态，和阶段 03 的 `_fail_run` 同理
- 重跑时先 `DELETE` 旧 chunk。arq 会重投失败任务，不清理就会有重复 chunk，检索结果里同一段内容出现多次
- `zip(..., strict=True)`：向量数和切片数必须一致，不一致说明 embedding 接口返回异常，宁可报错也不要错位存储
- **不要一次 add 几千个 Chunk 然后一次 commit**。大文档可能产生上万 chunk，单次事务太大。改成每 500 个 commit 一次（但要注意部分成功的状态处理，简单做法是保持单次 commit 但限制 `MAX_EXTRACTED_CHARS`，本文档采用后者）

注册到 `WorkerSettings.functions`。

加一个清理僵尸文档的 cron（参照阶段 06 的 `reap_stale_runs`）：超过 30 分钟停在 `processing` 的标记为 `failed`。

---

## 任务 5：检索

### 5.1 检索函数

新建 `backend/app/agent/retrieval.py`：

```python
DEFAULT_TOP_K = 5
DEFAULT_SCORE_THRESHOLD = 0.35


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    seq: int
    content: str
    score: float


async def retrieve(
    *,
    session: AsyncSession,
    knowledge_base_ids: list[uuid.UUID],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
) -> list[RetrievedChunk]:
    """
    向量检索。

    用余弦距离，必须与 HNSW 索引的 vector_cosine_ops 一致，
    否则索引不会被使用，退化成全表扫描。
    """
    if not knowledge_base_ids:
        return []

    query_vector = (await get_embeddings().aembed_query(query))

    distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
    statement = (
        select(Chunk, Document.filename, distance)
        .join(Document, Document.id == Chunk.document_id)
        .where(col(Chunk.knowledge_base_id).in_(knowledge_base_ids))
        .order_by(distance)
        .limit(top_k)
    )
    result = await session.execute(statement)

    out: list[RetrievedChunk] = []
    for chunk, filename, dist in result.all():
        # 余弦距离范围 [0, 2]，相似度 = 1 - 距离
        score = 1.0 - float(dist)
        if score < score_threshold:
            continue
        out.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                filename=filename,
                seq=chunk.seq,
                content=chunk.content,
                score=score,
            )
        )
    return out
```

要点：

- `Chunk.embedding.cosine_distance(...)` 是 `pgvector.sqlalchemy` 提供的。如果 SQLModel 的字段访问方式导致它不可用，用 `col(Chunk.embedding).cosine_distance(...)`
- **`order_by(distance)` 必须直接排序距离表达式**。写成 `order_by(desc(score))` 之类的包装形式，Postgres 的查询规划器认不出来，索引就不会被用
- 阈值过滤在**应用层**做而不是 SQL 的 WHERE 里。在 WHERE 里加距离条件会让 HNSW 索引失效（近似索引不支持带过滤的精确范围查询）。先取 top_k 再过滤，可能返回少于 top_k 条，这是可接受的
- 阈值 0.35 是经验值，需要根据实际 embedding 模型调。调的时候用一个明确不相关的 query 测试，确认它返回空

### 5.2 验证维度一致性

检索前校验知识库的 embedding 配置与当前配置一致：

```python
async def assert_embedding_compatible(
    *, session: AsyncSession, knowledge_base_ids: list[uuid.UUID]
) -> None:
    """
    知识库建库时用的 embedding 模型必须与当前配置一致。
    不一致时向量空间不同，检索结果毫无意义，必须明确报错。
    """
    result = await session.execute(
        select(KnowledgeBase.id, KnowledgeBase.embedding_model, KnowledgeBase.embedding_dim)
        .where(col(KnowledgeBase.id).in_(knowledge_base_ids))
    )
    for kb_id, model, dim in result.all():
        if model != settings.EMBEDDING_MODEL or dim != settings.EMBEDDING_DIM:
            raise EmbeddingMismatchError(
                f"Knowledge base {kb_id} was built with {model} (dim {dim}), "
                f"but the platform is now configured with "
                f"{settings.EMBEDDING_MODEL} (dim {settings.EMBEDDING_DIM}). "
                "Rebuild the knowledge base."
            )
```

`EmbeddingMismatchError` 加到 `app/agent/exceptions.py`。

这个检查看起来多余，但换 embedding 模型是很容易发生的事（换供应商、升级模型），而不检查的后果是"检索静默返回垃圾结果"，极难排查。

### 5.3 检索节点

`app/agent/nodes.py` 新增：

```python
async def retrieve_context(
    state: AgentState, *, session_factory: Callable[[], AsyncSession]
) -> dict[str, object]:
    """
    用最后一条用户消息检索知识库，把结果注入 state。

    检索失败不中断执行，降级为无检索结果继续跑。
    """
    kb_ids = [uuid.UUID(i) for i in state["knowledge_base_ids"]]
    if not kb_ids:
        return {"retrieved_chunks": []}

    query = _last_human_text(state["messages"])
    if not query:
        return {"retrieved_chunks": []}

    try:
        async with session_factory() as session:
            await assert_embedding_compatible(session=session, knowledge_base_ids=kb_ids)
            chunks = await retrieve(
                session=session, knowledge_base_ids=kb_ids, query=query
            )
    except Exception:
        logger.warning("Retrieval failed, continuing without context", exc_info=True)
        return {"retrieved_chunks": []}

    return {
        "retrieved_chunks": [
            {
                "chunk_id": str(c.chunk_id),
                "document_id": str(c.document_id),
                "filename": c.filename,
                "seq": c.seq,
                "content": c.content,
                "score": round(c.score, 4),
            }
            for c in chunks
        ]
    }
```

要点：

- **`retrieved_chunks` 存成 dict 列表而不是 dataclass**。它要进 checkpoint，必须是可序列化的原生类型（和阶段 07 的 `set` 问题同源）
- 检索失败降级而不是失败整个 Run。知识库挂了 Agent 还能靠模型自身知识回答
- 节点需要数据库 session，但 `AgentState` 里不能放 session（不可序列化）。用**闭包传入 session factory**，和阶段 05 传工具的方式一致

### 5.4 状态与图

`AgentState` 追加：

```python
    knowledge_base_ids: list[str]          # 字符串形式的 UUID，便于序列化
    retrieved_chunks: list[dict[str, Any]]
```

图结构：检索节点放在 `call_model` **之前**：

```python
    if knowledge_base_ids:
        graph.add_node("retrieve_context", _retrieve_context)
        graph.add_edge(START, "retrieve_context")
        graph.add_edge("retrieve_context", "call_model")
    else:
        graph.add_edge(START, "call_model")
```

**只在图的入口检索一次，不在工具循环里重复检索**。`execute_tools` 回到 `call_model` 时不再经过检索节点，这是有意的：重复检索同一个 query 浪费 embedding 调用且结果一样。

如果要支持模型主动多次检索，正确做法是把检索做成一个**工具**（阶段 05 的 Function Tool）而不是固定节点。这是个值得考虑的扩展方向，但第一版用固定节点更简单可控。在偏差记录里留一笔。

`KNOWN_NODE_NAMES` 加 `"retrieve_context"`。

### 5.5 把检索结果喂给模型

`call_model` 节点里，把 `retrieved_chunks` 拼进 system message：

```python
    prompt_parts = [state["system_prompt"]] if state["system_prompt"] else []

    chunks = state.get("retrieved_chunks") or []
    if chunks:
        context_block = "\n\n".join(
            f"[{i + 1}] (source: {c['filename']}, chunk {c['seq']})\n{c['content']}"
            for i, c in enumerate(chunks)
        )
        prompt_parts.append(
            "Use the following retrieved context to answer the user. "
            "Cite sources with bracketed numbers like [1] when you use them. "
            "If the context does not contain the answer, say so instead of guessing.\n\n"
            f"{context_block}"
        )
```

要点：

- 每段带编号 `[1]` `[2]`，并明确要求模型用这个格式引用。这是"答案来源引用"功能的实现方式 —— 不需要额外的结构化输出，让模型在文本里标注编号，前端解析编号映射回文档
- **明确指示"上下文里没有就说没有"**。不加这句，模型会用自身知识编造答案，RAG 就失去意义了
- 检索结果放 system message 而不是 user message，避免污染对话历史

---

## 任务 6：路由

新建 `backend/app/api/routes/knowledge.py`，`tags=["knowledge"]`。

| 方法 | 路径 | response_model | session | 说明 |
|------|------|----------------|---------|------|
| GET | `/knowledge/` | `KnowledgeBasesPublic` | sync | 知识库列表 |
| POST | `/knowledge/` | `KnowledgeBasePublic` | sync | 创建（自动填当前 embedding 配置） |
| GET | `/knowledge/{id}` | `KnowledgeBasePublic` | sync | 详情 |
| PATCH | `/knowledge/{id}` | `KnowledgeBasePublic` | sync | 改名/描述 |
| DELETE | `/knowledge/{id}` | `Message` | **async** | 删除（要清 MinIO 对象） |
| GET | `/knowledge/{id}/documents` | `DocumentsPublic` | sync | 文档列表 |
| POST | `/knowledge/{id}/documents` | `DocumentPublic` | **async** | 上传文档 |
| DELETE | `/knowledge/{id}/documents/{doc_id}` | `Message` | **async** | 删除文档 |
| POST | `/knowledge/{id}/documents/{doc_id}/reprocess` | `DocumentPublic` | **async** | 重新处理失败的文档 |
| POST | `/knowledge/{id}/search` | `SearchResultsPublic` | **async** | 检索测试 |

### 上传接口

```python
@router.post("/{id}/documents", response_model=DocumentPublic, status_code=202)
async def upload_document(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    file: Annotated[UploadFile, File()],
) -> Any:
    """
    Upload a document for ingestion.

    Returns immediately with status=pending. The document is parsed,
    chunked and embedded by a background worker.
    """
```

流程：

1. 校验知识库归属
2. **校验 MIME 类型**在白名单里，不在返回 415
3. 读文件内容，**校验大小**不超过 `MAX_DOCUMENT_SIZE_BYTES`，超了返回 413
4. 建 Document 记录拿到 id
5. 传 MinIO
6. 入队 `process_document_task`
7. 返回 202

大小校验要注意：`UploadFile` 是流式的，`await file.read()` 会把整个文件读进内存。20 MB 上限下可接受，但要**先检查 `file.size`**（如果客户端提供了 Content-Length）再 read，避免恶意大文件打爆内存：

```python
    if file.size is not None and file.size > MAX_DOCUMENT_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    data = await file.read()
    if len(data) > MAX_DOCUMENT_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
```

两层检查都要有，`file.size` 可能是 None 或者被伪造。

**MinIO 上传失败要回滚 Document 记录**，否则会留下指向不存在对象的记录（和阶段 06 的入队失败同理）。

FastAPI 处理文件上传需要 `python-multipart`，项目已有。

### 删除要清对象存储

删除知识库和文档时，数据库有 CASCADE，但 **MinIO 里的对象不会自动删**。必须显式清理：

```python
    # 先收集所有 storage_key，再删数据库记录
    keys = (await session.execute(
        select(Document.storage_key).where(Document.knowledge_base_id == id)
    )).scalars().all()

    await session.delete(kb)
    await session.commit()

    # 对象删除失败只记日志，不影响业务。孤儿对象由清理任务处理。
    for key in keys:
        try:
            await delete_object(key=key)
        except Exception:
            logger.warning("Failed to delete object %s", key, exc_info=True)
```

顺序是**先删数据库后删对象**。反过来的话，对象删了但数据库删除失败，就会有记录指向不存在的对象。孤儿对象只是占空间，孤儿记录会导致功能报错。

可选加一个 cron 任务扫 MinIO 里没有对应 Document 记录的对象并清理。演示项目可以跳过，在偏差记录里写明。

### 检索测试接口

```python
class SearchRequest(SQLModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
```

返回每条结果的 content、filename、seq、score。这个接口对调试检索质量非常有用（阈值调多少合适、切片粒度对不对），一定要做。

### Agent 绑定知识库

`AgentBase` 追加：

```python
    knowledge_base_ids: list[uuid.UUID] = Field(default_factory=list, sa_type=JSONB)
```

`AgentUpdate` 里加对应可选字段，路由里校验归属（和阶段 05 的 `tool_ids` 完全同理）。

`AgentSnapshot` 追加 `knowledge_base_ids: list[uuid.UUID] = Field(default_factory=list)`，**必须有默认值**。

迁移里给 `agent` 表加列，注意 `server_default="[]"`。

---

## 任务 7：测试

### 7.1 `backend/tests/agent/test_ingestion.py`

- 纯文本解析正确
- PDF 解析：用一个最小的测试 PDF（可以用 pypdf 自己生成一个，或者放一个小 fixture 文件在 `tests/fixtures/`）
- 不支持的 MIME 类型抛 ValueError
- 空内容（模拟扫描版 PDF）抛 ValueError 且错误信息提到 OCR
- 超长文本抛 ValueError
- 切片：给定 `chunk_size=100, overlap=20`，断言每片长度不超过 100 且相邻片有重叠
- 切片：中文文本能在句号处切分（这条验证 `separators` 里的中文句号生效）

### 7.2 `backend/tests/agent/test_retrieval.py`

Embedding 要 mock，不能调真实 API：

```python
@pytest.fixture
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """确定性的假 embedding：按文本哈希生成固定向量。"""

    def _fake_vector(text: str) -> list[float]:
        rng = random.Random(hash(text) % (2**32))
        return [rng.random() for _ in range(settings.EMBEDDING_DIM)]

    class FakeEmbeddings:
        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            return [_fake_vector(t) for t in texts]

        async def aembed_query(self, text: str) -> list[float]:
            return _fake_vector(text)

    monkeypatch.setattr("app.agent.ingestion.get_embeddings", lambda: FakeEmbeddings())
    monkeypatch.setattr("app.agent.retrieval.get_embeddings", lambda: FakeEmbeddings())
```

注意要 patch 两个模块里的引用（Python 的 `from x import y` 会在导入处创建新绑定）。或者统一从一个地方 import 避免这个问题。

**更好的测试方式**：手工插入已知向量的 Chunk，然后用完全相同的向量查询，断言它排第一。这样不依赖 embedding 的语义正确性，只验证检索管道：

```python
async def test_retrieve_returns_nearest(async_db) -> None:
    target = [1.0] + [0.0] * (settings.EMBEDDING_DIM - 1)
    other = [0.0, 1.0] + [0.0] * (settings.EMBEDDING_DIM - 2)
    # 插入两个 chunk，用 target 向量查询，断言 target chunk 排第一且 score 接近 1.0
```

其它场景：

- 空 `knowledge_base_ids` 返回空列表不报错
- `top_k` 生效
- 低于阈值的结果被过滤
- `assert_embedding_compatible` 在模型不匹配时抛 `EmbeddingMismatchError`
- 检索节点在 session 抛异常时返回空列表而不是传播异常

### 7.3 索引生效验证

这条测试证明 HNSW 索引真的被用了：

```python
async def test_hnsw_index_is_used(async_db) -> None:
    """
    检索查询必须走 HNSW 索引。
    如果距离算子与索引不匹配，这里会看到 Seq Scan。
    """
    # 需要足够多的数据，否则 Postgres 会选择全表扫描（小表上更快）
    # 插入 1000 个 chunk
    ...
    result = await async_db.execute(text(
        "EXPLAIN (FORMAT JSON) SELECT id FROM chunk "
        "ORDER BY embedding <=> :vec LIMIT 5"
    ), {"vec": str(query_vector)})
    plan = json.dumps(result.scalar_one())
    assert "chunk_embedding_hnsw_idx" in plan
```

注意小表上 Postgres 会选全表扫描（那确实更快），所以要插足够数据。这个测试可能较慢，标记为 `@pytest.mark.slow` 并在 CI 里跳过。

### 7.4 `backend/tests/worker/test_process_document.py`

- 上传纯文本后处理成功，`status == ready`，`chunk_count` 正确，`chunk` 表里有对应记录
- 解析失败的文档 `status == failed` 且 `error` 非空
- 重跑已 ready 的文档跳过
- 重跑 failed 的文档会先删旧 chunk（先手工插几个假 chunk，跑完断言总数正确而不是翻倍）
- MinIO 里对象不存在时落 failed 而不是抛异常

MinIO 在测试里用真实服务（compose 里有）。加一个 fixture 在测试后清理测试对象。

### 7.5 `backend/tests/api/routes/test_knowledge.py`

- 创建知识库自动填 `embedding_model` 和 `embedding_dim`
- 上传返回 202 且 `status == pending`
- 不支持的类型返回 415
- 超大文件返回 413
- 别人的知识库返回 403
- 删除知识库后 MinIO 里的对象也没了
- `/search` 返回结果
- Agent 绑定不属于自己的知识库返回 400

### 7.6 端到端手动验证

```bash
# 1. 建知识库
# 2. 上传一个内容明确的 PDF 或 txt（例如一份产品说明，里面有一个不可能被模型猜到的事实，
#    比如 "AgentHub 的默认并发限制是 3 个 Run"）
# 3. 等状态变 ready（前端轮询会自动更新）
# 4. 用 /search 接口搜一下，确认能命中那段内容
# 5. 把知识库绑到 agent，发布版本
# 6. 在 Playground 问那个事实
# 7. 确认回复正确且带 [1] 引用标记，前端能展开看到来源文档和原文段落
# 8. 问一个知识库里完全没有的问题，确认模型说"上下文里没有"而不是编造
```

第 8 步很重要，它验证了 prompt 里那句"没有就说没有"真的生效。

---

## 任务 8：前端

### 8.1 知识库列表页

新建 `frontend/src/routes/_layout/knowledge.tsx`。

列：name、description、文档数、就绪文档数、created_at、actions。

创建对话框：name、description、chunk_size（默认 1000，带说明）、chunk_overlap（默认 200）。表单上要提示"切片参数创建后不可修改"。

侧栏加 `{ icon: BookOpen, title: "Knowledge", path: "/knowledge" }`。

### 8.2 知识库详情页

`frontend/src/routes/_layout/knowledge.$kbId.tsx`，两个页签。

**Documents 页签**：

- 拖拽上传区（`<input type="file">` + drop 事件），显示支持的格式和大小上限
- 文档表格：filename、status（Badge）、size、chunk_count、created_at、actions
- **状态轮询**：有 `pending` 或 `processing` 的文档时每 3 秒刷新（与阶段 06 的 Runs 列表同一模式）
- `failed` 的文档在 status 旁显示错误图标，hover 显示 `error` 内容，操作菜单里有 "Reprocess"
- 上传用原生 `fetch` + `FormData`。生成的客户端对 multipart 的支持可能别扭，直接手写更可控：

  ```typescript
  const form = new FormData()
  form.append("file", file)
  await fetch(`${baseURL}/api/v1/knowledge/${kbId}/documents`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  })
  ```

  注意**不要手动设 `Content-Type`**，浏览器会自动加上带 boundary 的正确值。手动设会导致后端解析失败，这是最常见的上传失败原因。

**Search 页签**：

检索测试界面。输入 query、调 top_k，展示结果列表（每条显示 score、来源文件名、chunk 序号、内容）。score 用进度条或颜色深浅可视化。

这个页签是调参工具，能让用户理解"为什么 Agent 没找到我要的内容"。

### 8.3 Agent 详情页绑定知识库

Configuration 页签加 "Knowledge Bases" 区块，多选列表，与阶段 05 的工具绑定 UI 一致。

每个选项旁显示该知识库的就绪文档数，为 0 的加警告提示（绑了空知识库是常见的困惑来源）。

### 8.4 来源引用展示

这是本阶段前端的亮点。Playground 的 assistant 消息里，把 `[1]` `[2]` 这样的标记渲染成可点击的角标。

实现思路：

1. Run 结束后从 `run_event` 里拿 `retrieved_chunks`（需要在 `run_started` 或一个新事件里带上检索结果）
2. 用正则 `/\[(\d+)\]/g` 匹配回复文本里的标记
3. 替换成可点击元素，点击弹出 `Popover` 显示对应 chunk 的文件名、序号和原文
4. 消息底部列出本次使用的全部来源（去重后的文档列表）

为了让前端拿到检索结果，在 SSE 契约里**新增一个事件**：

| event | data |
|-------|------|
| `context_retrieved` | `{"chunks": [{"index": 1, "filename": "...", "seq": 3, "content": "...", "score": 0.82}]}` |

在 `retrieve_context` 节点结束后发布。同步更新阶段 04 的契约表格。

这个事件也要落库到 `run_event`（它是执行轨迹的一部分），所以 `RunEventType` 要加一个 `CONTEXT_RETRIEVED = "context_retrieved"`。

**注意这违反了阶段 03 定下的"事件类型在阶段 03 定全"的约定。** 加新类型是可以的（枚举是 varchar 不需要 ALTER TYPE），但要在阶段 03 文档的事件类型列表里补上这一条，保持文档一致。

---

## 阶段验收清单

- [ ] `uv run alembic check` 输出 `No new upgrade operations detected.`
- [ ] 迁移文件里有 `from pgvector.sqlalchemy import Vector` 的 import
- [ ] `docker compose exec db psql -U postgres -d app -c "\d chunk"` 里 `embedding` 是 `vector(1536)`
- [ ] `docker compose exec db psql -U postgres -d app -c "\di chunk*"` 能看到 `chunk_embedding_hnsw_idx`
- [ ] `uv run alembic downgrade -1 && uv run alembic upgrade head` 往返无错（索引的 drop 和 create 都对）
- [ ] `agent` 表的 `knowledge_base_ids` 列有 `server_default '[]'`
- [ ] `bash scripts/prestart.sh` 从干净环境跑通，MinIO bucket 被创建
- [ ] `uv run pytest tests/ -v` 全绿（需要 MinIO 和 Redis 在跑）
- [ ] 测试不调真实 embedding API（把 `LLM_BASE_URL` 改无效后测试仍全绿）
- [ ] 索引生效测试通过（EXPLAIN 里出现索引名）
- [ ] 重跑 failed 文档不产生重复 chunk 的测试通过
- [ ] `cd backend && bash scripts/lint.sh` 全绿
- [ ] `cd backend && bash scripts/test.sh` 全绿
- [ ] `bun run lint` 通过
- [ ] 手动完成任务 7.6 的八步验证，特别是第 7 步的引用标记和第 8 步的"不编造"
- [ ] 手动验证：上传一个扫描版 PDF（无文字层），文档落 failed 且错误信息提到 OCR
- [ ] 手动验证：删除知识库后，MinIO 控制台里对应的对象也消失了
- [ ] 手动验证：文档上传后列表状态自动从 pending 变 processing 变 ready（轮询生效）
- [ ] 手动验证：Search 页签能调出合理的检索结果和分数
- [ ] `SELECT status, count(*) FROM document GROUP BY status;` 没有卡在 processing 的记录
- [ ] `SELECT count(*) FROM chunk WHERE embedding IS NULL;` 返回 0

---

## 常见坑

**迁移跑起来报 `NameError: Vector`**
autogenerate 没加 import。手工在迁移文件顶部加 `from pgvector.sqlalchemy import Vector`。

**检索很慢，EXPLAIN 显示 Seq Scan**
距离算子与索引不匹配。索引是 `vector_cosine_ops` 就必须用 `<=>`（`cosine_distance`）。用 `<->`（L2）或 `<#>`（内积）都不会命中这个索引。

**在 WHERE 里加距离阈值后索引失效**
HNSW 不支持带范围过滤的查询。先 `ORDER BY distance LIMIT k` 再在应用层过滤。

**`metadata` 字段报 SQLAlchemy 保留名错误**
改成 `metadata_`。

**文档永远停在 processing**
worker 里的异常没被捕获，或者 worker 没在跑。`process_document_task` 的 `except` 必须落 FAILED 状态。加 cron 兜底。

**重跑文档后 chunk 翻倍**
处理前必须 `DELETE FROM chunk WHERE document_id = ...`。arq 会重投失败任务。

**上传报 422 或 400，说缺 boundary**
前端手动设了 `Content-Type`。删掉，让浏览器自己加。

**检索结果全是不相关内容但 score 很高**
Embedding 模型换了但知识库没重建。`assert_embedding_compatible` 就是为了在这种情况下明确报错。

**中文文档检索效果差**
`RecursiveCharacterTextSplitter` 的默认 separators 对中文无效，必须加 `"。"`。检查切片结果是不是都是按固定字符数硬切的。

**embedding 请求报 400 input too long**
批次太大。`EMBEDDING_BATCH_SIZE` 调小，或者检查有没有超长的单个 chunk。

**MinIO 连接泄漏导致卡死**
`get_object` 的响应必须 `close()` 和 `release_conn()`。

**MinIO 里的孤儿对象越来越多**
删除数据库记录时没删对象。删除接口里必须显式清理。

**检索节点在工具循环里被重复执行**
图的边设计问题。`execute_tools` 应该连回 `call_model` 而不是 `retrieve_context`。

---

## 偏差记录

- 实际 embedding 模型与维度：
- score 阈值的最终取值与调整依据：
- 支持的文件类型：
- 是否把检索做成工具（而非固定节点）：
- 是否实现孤儿对象清理：
- 其它偏差：
