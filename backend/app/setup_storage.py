import asyncio

from app.core.storage import ensure_bucket

if __name__ == "__main__":
    asyncio.run(ensure_bucket())
