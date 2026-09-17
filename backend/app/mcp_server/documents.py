import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_RESPONSE_CHARS = 16_000


def serialize(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    content: str


def load_documents(root: Path) -> dict[str, Document]:
    """Load a fixed snapshot of explicitly shared Markdown files at startup."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("MCP document root must be a directory")
    documents = {}
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.suffix != ".md":
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("MCP document links must stay inside the document root")
        if resolved.suffix != ".md" or any(
            part.startswith(".") for part in resolved.relative_to(root).parts
        ):
            raise ValueError("MCP document links must target visible Markdown files")
        if not resolved.is_file():
            continue
        content = resolved.read_text(encoding="utf-8")
        title = next(
            (
                line.lstrip("# ").strip()
                for line in content.splitlines()
                if line.startswith("# ")
            ),
            path.stem,
        )[:255]
        documents[path.stem] = Document(path.stem, title, content)
    return documents


def search_documents(
    *, documents: dict[str, Document], query: str, limit: int = 5
) -> str:
    query = query.strip()
    if not query or len(query) > 200 or not 1 <= limit <= 10:
        raise ValueError("query must contain 1–200 characters and limit must be 1–10")
    data = []
    for document in documents.values():
        offset = document.content.casefold().find(query.casefold())
        if offset < 0:
            continue
        data.append(
            {
                "doc_id": document.doc_id,
                "title": document.title,
                "summary": document.content[
                    max(0, offset - 80) : max(0, offset - 80) + 500
                ],
            }
        )
        if len(data) == limit:
            break
    while len(serialize({"data": data, "count": len(data)})) > MAX_RESPONSE_CHARS:
        data.pop()
    return serialize({"data": data, "count": len(data)})


def read_document(*, documents: dict[str, Document], doc_id: str) -> str:
    if doc_id not in documents:
        raise ValueError("Unknown document ID")
    document = documents[doc_id]
    result = {
        "doc_id": doc_id,
        "title": document.title,
        "content": document.content,
        "truncated": False,
    }
    if len(serialize(result)) <= MAX_RESPONSE_CHARS:
        return serialize(result)
    result["truncated"] = True
    low, high = 0, len(document.content)
    while low < high:
        middle = (low + high + 1) // 2
        result["content"] = document.content[:middle]
        if len(serialize(result)) <= MAX_RESPONSE_CHARS:
            low = middle
        else:
            high = middle - 1
    result["content"] = document.content[:low]
    return serialize(result)
