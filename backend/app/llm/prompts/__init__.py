"""Versioned prompt templates. Edit the .md files, not inline strings."""

from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


# Phrase the system prompt tells the model to use when nothing is relevant.
# Adapted from rag-claude (NOT_FOUND_MARKERS / is_not_found_answer).
NOT_FOUND_MARKERS = (
    "couldn't find that in your documents",
    "couldn't find anything in your documents",
)

NOT_FOUND_ANSWER = (
    "I couldn't find anything in your documents that answers that question. "
    "Try rephrasing it, or check that the relevant documents have been ingested."
)


def is_not_found_answer(text: str) -> bool:
    """True only when the whole answer is a not-found reply. A full answer that
    mentions one missing detail is a real answer and must be returned."""
    t = (text or "").strip().lower().replace("’", "'")
    return len(t) < 300 and any(m in t for m in NOT_FOUND_MARKERS)
