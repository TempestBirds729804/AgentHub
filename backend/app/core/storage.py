import io
import uuid
from functools import lru_cache
from pathlib import Path
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
    client = get_minio_client()
    if not await to_thread.run_sync(client.bucket_exists, settings.S3_BUCKET):
        await to_thread.run_sync(client.make_bucket, settings.S3_BUCKET)


def document_key(*, owner_id: uuid.UUID, document_id: uuid.UUID, filename: str) -> str:
    ext = Path(filename).suffix.lower()[:10]
    return f"documents/{owner_id}/{document_id}{ext}"


async def put_object(*, key: str, data: bytes, content_type: str) -> None:
    await to_thread.run_sync(
        lambda: get_minio_client().put_object(
            settings.S3_BUCKET,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
        )
    )


async def get_object(*, key: str) -> bytes:
    def read() -> bytes:
        response = get_minio_client().get_object(settings.S3_BUCKET, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    return await to_thread.run_sync(read)


async def delete_object(*, key: str) -> None:
    await to_thread.run_sync(get_minio_client().remove_object, settings.S3_BUCKET, key)
