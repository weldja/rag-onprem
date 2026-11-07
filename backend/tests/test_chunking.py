import pytest

from app.ingestion.chunking import Document, RecursiveTextSplitter


def test_short_text_is_one_chunk() -> None:
    assert RecursiveTextSplitter(100, 10).split_text("hello world") == ["hello world"]


def test_chunks_respect_size_and_prefer_paragraphs() -> None:
    para = "word " * 30  # 150 chars
    text = "\n\n".join([para.strip()] * 4)
    out = RecursiveTextSplitter(200, 20).split_text(text)
    assert len(out) == 4
    assert all(len(c) <= 200 for c in out)


def test_overlap_repeats_trailing_words() -> None:
    text = " ".join(f"w{i}" for i in range(100))
    out = RecursiveTextSplitter(50, 15).split_text(text)
    assert len(out) > 1
    assert all(len(c) <= 50 for c in out)
    # The start of each chunk overlaps the end of the previous one.
    for prev, nxt in zip(out, out[1:], strict=False):
        assert nxt.split()[0] in prev.split()


def test_split_documents_copies_metadata() -> None:
    docs = [Document("a " * 100, {"page": 3})]
    out = RecursiveTextSplitter(60, 0).split_documents(docs)
    assert len(out) > 1
    assert all(d.metadata == {"page": 3} for d in out)
    out[0].metadata["page"] = 99
    assert out[1].metadata["page"] == 3


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (10, -1), (10, 11)])
def test_invalid_settings_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        RecursiveTextSplitter(size, overlap)
