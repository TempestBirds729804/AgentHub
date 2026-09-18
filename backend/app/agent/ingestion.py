import io
import logging
import math
from functools import lru_cache

import httpx
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings

SUPPORTED_MIME_TYPES = {
    "application/pdf": "pdf",
    "text/plain": "text",
    "text/markdown": "text",
}
MAX_DOCUMENT_SIZE_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_CHARS = 2_000_000
EMBEDDING_BATCH_SIZE = 20
logger = logging.getLogger(__name__)


def embedding_error_detail(exc: Exception) -> str:
    """Preserve empty-message exception types and causes without exposing keys."""
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(parts) < 5:
        parts.append(f"{type(current).__name__}: {str(current) or '(no message)'}")
        current = current.__cause__
    detail = " -> ".join(parts)
    for key in (settings.EMBEDDING_API_KEY, settings.LLM_API_KEY):
        if key:
            detail = detail.replace(key, "[REDACTED]")
    return detail[:1800]


@retry(
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ConnectTimeout)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=2),
    reraise=True,
)
async def _post_embeddings(
    client: httpx.AsyncClient, texts: list[str]
) -> httpx.Response:
    """Retry only failures while connecting, before a request is submitted."""
    try:
        return await client.post(
            settings.EMBEDDING_BASE_URL.rstrip("/")
            + "/services/embeddings/multimodal-embedding/multimodal-embedding",
            headers={"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"},
            json={
                "model": settings.EMBEDDING_MODEL,
                "input": {"contents": [{"text": text} for text in texts]},
                "parameters": {"dimension": settings.EMBEDDING_DIM},
            },
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        logger.warning("Embedding connection failed: %s", embedding_error_detail(exc))
        raise


def extract_text(*, data: bytes, mime_type: str, filename: str) -> str:
    kind = SUPPORTED_MIME_TYPES.get(mime_type)
    if kind is None:
        raise ValueError(f"Unsupported file type: {mime_type} ({filename})")
    if kind == "pdf":
        pages: list[str] = []
        length = 0
        for page in PdfReader(io.BytesIO(data)).pages:
            content = page.extract_text() or ""
            length += len(content) + 2
            if length > MAX_EXTRACTED_CHARS:
                raise ValueError("Document too large after text extraction")
            pages.append(content)
        text = "\n\n".join(pages)
    else:
        text = data.decode("utf-8", errors="replace")
    text = text.replace("\x00", "")
    if not text.strip():
        raise ValueError(
            "No text could be extracted. Scanned PDFs without OCR are not supported."
        )
    if len(text) > MAX_EXTRACTED_CHARS:
        raise ValueError("Document too large after text extraction")
    return text


def split_text(*, text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", ". ", " ", ""],
    )
    return [part for part in splitter.split_text(text) if part.strip()]


class DashScopeEmbeddings:
    """Text-only use of Qwen's native multimodal embedding endpoint."""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        if not settings.EMBEDDING_API_KEY:
            raise ValueError("EMBEDDING_API_KEY is not configured")
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60) as client:
            for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                batch = texts[start : start + EMBEDDING_BATCH_SIZE]
                try:
                    response = await _post_embeddings(client, batch)
                except httpx.RequestError as exc:
                    raise ValueError(
                        f"Embedding transport failed: {embedding_error_detail(exc)}"
                    ) from exc
                if response.is_error:
                    # Do not persist upstream response bodies or credentials in document errors.
                    raise ValueError(
                        f"Embedding request failed (HTTP {response.status_code})"
                    )
                rows = response.json().get("output", {}).get("embeddings", [])
                if len(rows) != len(batch) or sorted(
                    row["index"] for row in rows
                ) != list(range(len(batch))):
                    raise ValueError("Embedding response has invalid indices or count")
                for row in sorted(rows, key=lambda item: item["index"]):
                    vector = [float(value) for value in row["embedding"]]
                    if (
                        len(vector) != settings.EMBEDDING_DIM
                        or not all(math.isfinite(v) for v in vector)
                        or not any(vector)
                    ):
                        raise ValueError(
                            "Embedding response has invalid dimension or values"
                        )
                    vectors.append(vector)
        return vectors

    async def aembed_query(self, text: str) -> list[float]:
        return (await self.aembed_documents([text]))[0]


@lru_cache(maxsize=1)
def get_embeddings() -> DashScopeEmbeddings:
    return DashScopeEmbeddings()


async def embed_chunks(texts: list[str]) -> list[list[float]]:
    return await get_embeddings().aembed_documents(texts)
