import io
from typing import Any

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.agent.ingestion import DashScopeEmbeddings, extract_text, split_text
from app.core.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_connection_retry_and_error_details(
    monkeypatch: pytest.MonkeyPatch, recover: bool
) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "fake-private-key")
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 2)
    calls = 0

    async def post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        nonlocal calls
        calls += 1
        if not recover or calls < 3:
            raise httpx.ConnectError("") from OSError(
                "upstream fake-private-key unavailable"
            )
        return httpx.Response(
            200, json={"output": {"embeddings": [{"index": 0, "embedding": [1, 1]}]}}
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    if recover:
        assert await DashScopeEmbeddings().aembed_query("q") == [1, 1]
    else:
        with pytest.raises(ValueError, match="ConnectError") as error:
            await DashScopeEmbeddings().aembed_query("q")
        assert "OSError" in str(error.value)
        assert "fake-private-key" not in str(error.value)
    assert calls == 3


def test_extract_and_split() -> None:
    assert (
        extract_text(data=b"hello", mime_type="text/plain", filename="a.txt") == "hello"
    )
    chunks = split_text(text="a" * 250, chunk_size=100, chunk_overlap=20)
    assert [len(c) for c in chunks] == [100, 100, 90]
    assert chunks[0][-20:] == chunks[1][:20]
    chinese = split_text(
        text="甲" * 60 + "。" + "乙" * 60 + "。", chunk_size=100, chunk_overlap=0
    )
    assert chinese[0] == "甲" * 60


@pytest.mark.parametrize(
    "data,mime,error",
    [
        (b"", "text/plain", "OCR"),
        (b"x", "image/png", "Unsupported"),
        (b"x" * 2_000_001, "text/plain", "too large"),
    ],
    ids=["empty", "mime", "oversize"],
)
def test_invalid_documents(data: bytes, mime: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        extract_text(data=data, mime_type=mime, filename="test")


def test_pdf() -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ValueError, match="OCR"):
        extract_text(
            data=buffer.getvalue(), mime_type="application/pdf", filename="scan.pdf"
        )
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (AgentHub secret 7429) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert "7429" in extract_text(
        data=buffer.getvalue(), mime_type="application/pdf", filename="text.pdf"
    )


@pytest.mark.asyncio
async def test_embedding_batches_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "fake")
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 2)
    batches = []

    async def post(
        _self: httpx.AsyncClient, _url: str, **kwargs: Any
    ) -> httpx.Response:
        body = kwargs["json"]
        count = len(body["input"]["contents"])
        batches.append(count)
        assert body["parameters"] == {"dimension": 2}
        return httpx.Response(
            200,
            json={
                "output": {
                    "embeddings": [
                        {"index": i, "embedding": [i + 1, 1]}
                        for i in reversed(range(count))
                    ]
                }
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    vectors = await DashScopeEmbeddings().aembed_documents(["x"] * 21)
    assert batches == [20, 1]
    assert len(vectors) == 21 and vectors[0] == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,rows", [(401, []), (200, []), (200, [{"index": 0, "embedding": [1]}])]
)
async def test_embedding_errors(
    monkeypatch: pytest.MonkeyPatch, status: int, rows: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "fake")

    async def post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(status, json={"output": {"embeddings": rows}})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(ValueError):
        await DashScopeEmbeddings().aembed_query("x")
