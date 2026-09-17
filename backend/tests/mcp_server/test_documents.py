import json
from pathlib import Path

import pytest

from app.mcp_server.documents import load_documents, read_document, search_documents


def make_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip(
                "Windows lacks symlink privilege; required checks also run in Linux container"
            )
        raise


def test_document_search_and_read(tmp_path: Path) -> None:
    (tmp_path / "02.md").write_text("# Second\nCheckpoint details", encoding="utf-8")
    (tmp_path / "01.md").write_text(
        "# First\nCheckpoint introduction", encoding="utf-8"
    )
    (tmp_path / ".hidden.md").write_text("secret", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    documents = load_documents(tmp_path)
    assert list(documents) == ["01", "02"]
    result = json.loads(
        search_documents(documents=documents, query="checkpoint", limit=1)
    )
    assert result["count"] == 1 and result["data"][0]["doc_id"] == "01"
    assert len(result["data"][0]["summary"]) <= 500
    assert (
        json.loads(search_documents(documents=documents, query="absent"))["data"] == []
    )
    read = json.loads(read_document(documents=documents, doc_id="02"))
    assert read["title"] == "Second" and not read["truncated"]
    for doc_id in ("../01", ".env", "unknown", str(tmp_path / "01.md")):
        with pytest.raises(ValueError, match="Unknown document"):
            read_document(documents=documents, doc_id=doc_id)


@pytest.mark.parametrize("query,limit", [(" ", 5), ("x" * 201, 5), ("x", 0), ("x", 11)])
def test_invalid_search(query: str, limit: int) -> None:
    with pytest.raises(ValueError):
        search_documents(documents={}, query=query, limit=limit)


def test_response_budget_and_missing_root(tmp_path: Path) -> None:
    for number in range(10):
        (tmp_path / f"{number}.md").write_text(
            "# Large\n" + '\x00"\\\n中' * 20000, encoding="utf-8"
        )
    documents = load_documents(tmp_path)
    read = read_document(documents=documents, doc_id="0")
    assert len(read) <= 16000 and json.loads(read)["truncated"]
    search = search_documents(documents=documents, query="Large", limit=10)
    assert len(search) <= 16000
    assert 0 < json.loads(search)["count"] <= 10
    with pytest.raises(FileNotFoundError):
        load_documents(tmp_path / "missing")
    with pytest.raises(ValueError, match="directory"):
        load_documents(tmp_path / "0.md")


def test_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    make_symlink(root / "escape.md", outside)
    with pytest.raises(ValueError, match="inside"):
        load_documents(root)


def test_symlink_hidden_target(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    make_symlink(tmp_path / "alias.md", tmp_path / ".env")
    with pytest.raises(ValueError, match="visible Markdown"):
        load_documents(tmp_path)
