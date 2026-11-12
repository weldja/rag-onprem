"""Questions about the document collection itself ("how many docs are indexed",
"which documents do you have").

The model only ever sees the few excerpts retrieved for a question, so it
can't answer these (it once counted six excerpts as "6 documents indexed").
They are answered from the database instead, under the same visibility rule
and scope as retrieval, without calling the model.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import Engine, func, select

from app.auth.acl import Viewer, can_see
from app.db.tables import documents

LibraryKind = Literal["count", "list"]

# How many file names a "which documents" answer lists before "and N more".
LIST_LIMIT = 50

# Not "pdf": "how many PDFs" would be answered with the count of every document.
_DOC_WORDS = frozenset(["doc", "docs", "document", "documents", "file", "files"])
_COUNT_WORDS = frozenset(["many", "number", "count", "total"])
_LIST_WORDS = frozenset(["which", "what", "list", "show", "name", "names"])
# Everything else a library question may contain. Any other word ("How many
# documents must I submit?", "Which documents mention parking?", "What is this
# document about?") makes it an ordinary question about the documents' contents.
_FILLER_WORDS = """
    a access accessible all an and any app are available be been can collection currently
    database do does exist exists folder for got has have here how i in index indexed
    indexes ingested is kind kinds knowledge library loaded me my now of on our please right
    s searchable see share so stored system tell the there to type types uploaded us we were
    you your
"""
_FILLER = frozenset(_FILLER_WORDS.split())


def library_question(question: str) -> LibraryKind | None:
    """The kind of answer wanted ("count" or "list") if the question only asks
    about the collection itself, else None."""
    words = re.findall(r"[a-z]+", question.lower())
    if not _DOC_WORDS.intersection(words):
        return None
    allowed = _DOC_WORDS | _COUNT_WORDS | _LIST_WORDS | _FILLER
    if any(w not in allowed for w in words):
        return None
    if _COUNT_WORDS.intersection(words):
        return "count"
    if _LIST_WORDS.intersection(words):
        return "list"
    return None


@dataclass(frozen=True)
class LibraryStats:
    indexed: int  # status "ready": searchable
    in_progress: int  # "pending" or "processing"
    failed: int
    names: list[str] = field(default_factory=list)  # indexed, by name, at most LIST_LIMIT


def library_stats(
    engine: Engine,
    viewer: Viewer,
    document_ids: Sequence[int] | None = None,
    *,
    list_limit: int = LIST_LIMIT,
) -> LibraryStats:
    """Document counts by state, limited to what `viewer` may see (and to
    `document_ids` when given, as retrieval does)."""
    visible = can_see(viewer)
    if document_ids:
        visible = visible & documents.c.id.in_(list(document_ids))
    with engine.connect() as conn:
        by_status: dict[str, int] = {
            status: n
            for status, n in conn.execute(
                select(documents.c.status, func.count()).where(visible).group_by(documents.c.status)
            )
        }
        names = list(
            conn.execute(
                select(documents.c.filename)
                .where(visible & (documents.c.status == "ready"))
                .order_by(func.lower(documents.c.filename), documents.c.id)
                .limit(list_limit)
            ).scalars()
        )
    return LibraryStats(
        indexed=by_status.get("ready", 0),
        in_progress=by_status.get("pending", 0) + by_status.get("processing", 0),
        failed=by_status.get("failed", 0),
        names=names,
    )


def _documents(n: int) -> str:
    return f"{n} document" if n == 1 else f"{n} documents"


def library_answer(stats: LibraryStats, kind: LibraryKind, *, scoped: bool) -> str:
    where = " in the current scope" if scoped else ""
    if stats.indexed:
        verb = "is" if stats.indexed == 1 else "are"
        lines = [f"**{_documents(stats.indexed)}**{where} {verb} indexed and available to you."]
    else:
        lines = [f"No documents{where} are indexed and available to you yet."]
    if stats.in_progress:
        n = stats.in_progress
        more = "1 more document is" if n == 1 else f"{n} more documents are"
        lines.append(f"{more} waiting to be indexed or being indexed now.")
    if stats.failed:
        lines.append(f"{_documents(stats.failed)} could not be indexed.")
    text = " ".join(lines)
    if kind == "list" and stats.names:
        text += "\n\n" + "\n".join(f"- {name}" for name in stats.names)
        if stats.indexed > len(stats.names):
            text += f"\n\n…and {stats.indexed - len(stats.names)} more. The sidebar lists them all."
    return text
