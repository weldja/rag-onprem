"""Retrieval-only view of the eval cases: what each question retrieves, with
cosine and rerank scores, and which chunks are expected sources. No LLM calls,
so it takes seconds. Used to choose MIN_SIMILARITY / RERANK_MIN_SCORE.

Runs inside the api image on the backend network (scripts/retrieval-probe.sh),
as the eval user, so it sees exactly what the eval sees. Run scripts/eval.sh
once first so the corpus and user exist.
"""

import json
from pathlib import Path

from sqlalchemy import select

from app.api.deps import get_embedder, get_reranker
from app.auth.acl import Viewer
from app.core.config import get_settings
from app.db.engine import get_engine
from app.db.tables import users
from app.retrieval.answer import retrieval_query
from app.retrieval.service import DbSearch
from evals.run_eval import USER, _label, _matches


def main() -> None:
    settings = get_settings()
    engine = get_engine()
    with engine.connect() as conn:
        user_id = conn.execute(select(users.c.id).where(users.c.username == USER)).scalar_one()
    search = DbSearch(engine, Viewer(user_id, USER, False), settings, get_reranker())
    retrieve = search.retriever()
    embedder = get_embedder()
    cases = json.loads((Path(__file__).parent / "cases.json").read_text())

    print(f"reranker={settings.reranker_model or 'none'} min_similarity={settings.min_similarity}")
    for case in cases:
        query = retrieval_query(case["question"], case.get("history", []))
        hits = retrieve(query, embedder.embed([query])[0])
        expected = case.get("expect", [])
        print(f"\n{case['id']}  ({'answerable' if expected else 'UNANSWERABLE'})")
        for n, h in enumerate(hits, start=1):
            src = {"document": h.filename, "page": h.page, "section": h.section}
            mark = "*" if any(_matches(src, e) for e in expected) else " "
            rr = f"{h.rerank_score:7.2f}" if h.rerank_score is not None else "      -"
            print(f"  {mark} [{n}] cos={h.score:.3f} rerank={rr}  {_label(src)}")
        if not hits:
            print("  (nothing retrieved)")


if __name__ == "__main__":
    main()
