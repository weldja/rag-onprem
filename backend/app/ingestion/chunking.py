"""Text chunking.

RecursiveTextSplitter is copied from rag-claude/backend/rag_core.py, where it
was verified chunk-for-chunk against LangChain's RecursiveCharacterTextSplitter
(default separators, keep separator at the start of the next piece, strip
whitespace, length measured in characters). Token-aware sizing can come later.
"""

import copy
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Document:
    """One piece of text plus its metadata (page, ...)."""

    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class RecursiveTextSplitter:
    _SEPARATORS = ["\n\n", "\n", " ", ""]

    def __init__(self, chunk_size: int, chunk_overlap: int) -> None:
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
        if chunk_overlap < 0:
            raise ValueError(f"chunk_overlap must be >= 0, got {chunk_overlap}")
        if chunk_overlap > chunk_size:
            raise ValueError(
                f"chunk_overlap ({chunk_overlap}) is larger than chunk_size ({chunk_size})"
            )
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    @staticmethod
    def _split_keep_start(text: str, separator: str) -> list[str]:
        # Split on a literal separator, attaching each separator to the START
        # of the piece that follows it.
        if not separator:
            return list(text)
        parts = re.split(f"({re.escape(separator)})", text)
        pieces = [parts[0]] + [parts[i] + parts[i + 1] for i in range(1, len(parts) - 1, 2)]
        return [p for p in pieces if p]

    @staticmethod
    def _join(docs: list[str]) -> str | None:
        text = "".join(docs).strip()
        return text or None

    def _merge_splits(self, splits: list[str]) -> list[str]:
        docs: list[str] = []
        current: list[str] = []
        total = 0
        for d in splits:
            len_ = len(d)
            if total + len_ > self._chunk_size:  # noqa: SIM102 - kept identical to verified port
                if current:
                    doc = self._join(current)
                    if doc is not None:
                        docs.append(doc)
                    while total > self._chunk_overlap or (
                        total + len_ > self._chunk_size and total > 0
                    ):
                        total -= len(current[0])
                        current = current[1:]
            current.append(d)
            total += len_
        doc = self._join(current)
        if doc is not None:
            docs.append(doc)
        return docs

    def _split(self, text: str, separators: list[str]) -> list[str]:
        final: list[str] = []
        separator = separators[-1]
        remaining: list[str] = []
        for i, s in enumerate(separators):
            if not s:
                separator = s
                break
            if s in text:
                separator = s
                remaining = separators[i + 1 :]
                break

        good: list[str] = []
        for piece in self._split_keep_start(text, separator):
            if len(piece) < self._chunk_size:
                good.append(piece)
            else:
                if good:
                    final.extend(self._merge_splits(good))
                    good = []
                if not remaining:
                    final.append(piece)
                else:
                    final.extend(self._split(piece, remaining))
        if good:
            final.extend(self._merge_splits(good))
        return final

    def split_text(self, text: str) -> list[str]:
        return self._split(text, self._SEPARATORS)

    def split_documents(self, documents: list[Document]) -> list[Document]:
        out: list[Document] = []
        for doc in documents:
            for chunk in self.split_text(doc.page_content):
                out.append(Document(page_content=chunk, metadata=copy.deepcopy(doc.metadata)))
        return out
