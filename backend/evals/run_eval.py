"""Answer-quality evaluation against the running stack (scripts/eval.sh).

Builds the corpus, uploads it shared only with an "Eval corpus" group, and asks
every case in cases.json as an `eval-user` who sees nothing else, so other
documents in the index can't skew the numbers.

Metrics (answerable cases unless noted):
  retrieval_recall   an expected source was among the chunks sent to the LLM
  citation_recall    the answer cited an expected source
  citation_precision share of all citations that point at an expected source
  answer_accuracy    every expected fact (any of its spellings) is in the answer
  false_not_found    answerable, but the system said it couldn't find it
  abstention         unanswerable cases correctly answered "not found"
"""

import argparse
import json
import os
import secrets
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from evals.corpus import build

HERE = Path(__file__).parent
CSRF = {"X-Requested-With": "rag-onprem"}
GROUP = "Eval corpus"
USER = "eval-user"


@dataclass
class CaseResult:
    id: str
    answerable: bool
    found: bool
    retrieval_hit: bool = False
    citation_hit: bool = False
    cited: int = 0
    cited_relevant: int = 0
    answer_correct: bool = False
    seconds: float = 0.0
    answer: str = ""
    cited_sources: list[str] = field(default_factory=list)
    retrieved_sources: list[str] = field(default_factory=list)  # [n] order, for diagnosis


def _client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, headers=CSRF, verify=False, timeout=900)


def _login(base: str, user: str, password: str) -> httpx.Client:
    c = _client(base)
    c.post("/api/auth/login", json={"username": user, "password": password}).raise_for_status()
    return c


def _matches(source: dict[str, Any], expected: dict[str, Any]) -> bool:
    if source["document"] != expected["document"]:
        return False
    if "page" in expected and source.get("page") != expected["page"]:
        return False
    return not ("section" in expected and expected["section"] not in (source.get("section") or ""))


def _label(s: dict[str, Any]) -> str:
    where = (
        f" p{s['page']}" if s.get("page") else (f" [{s['section']}]" if s.get("section") else "")
    )
    return f"{s['document']}{where}"


def setup(admin: httpx.Client, corpus_dir: Path) -> str:
    """Ensure the eval group, user and corpus exist; return the user's password."""
    groups = {g["name"]: g["id"] for g in admin.get("/api/admin/groups").json()}
    group_id = (
        groups.get(GROUP) or admin.post("/api/admin/groups", json={"name": GROUP}).json()["id"]
    )
    password = secrets.token_urlsafe(18)
    users = {u["username"]: u for u in admin.get("/api/admin/users").json()}
    if USER in users:
        admin.patch(
            f"/api/admin/users/{users[USER]['id']}",
            json={"password": password, "group_ids": [group_id], "disabled": False},
        ).raise_for_status()
    else:
        admin.post(
            "/api/admin/users",
            json={"username": USER, "password": password, "group_ids": [group_id]},
        ).raise_for_status()

    doc_ids = []
    for path in build(corpus_dir):
        resp = admin.post(
            "/api/documents",
            files={"file": (path.name, path.read_bytes())},
            data={"group_ids": [str(group_id)]},
        )
        resp.raise_for_status()
        doc = resp.json()["document"]
        if group_id not in doc["group_ids"]:  # uploaded earlier with other sharing
            admin.put(f"/api/documents/{doc['id']}/groups", json={"group_ids": [group_id]})
        if doc["stale"]:
            admin.post(f"/api/documents/{doc['id']}/reindex")
        doc_ids.append(doc["id"])

    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        states = {admin.get(f"/api/documents/{i}").json()["status"] for i in doc_ids}
        if states <= {"ready", "failed"}:
            break
        time.sleep(2)
    failed = [i for i in doc_ids if admin.get(f"/api/documents/{i}").json()["status"] != "ready"]
    if failed:
        raise SystemExit(f"Corpus documents not ready: {failed}")
    return password


def run_case(client: httpx.Client, case: dict[str, Any]) -> tuple[CaseResult, str | None]:
    """Ask one case; returns its result and the server's prompt version."""
    started = time.monotonic()
    resp = client.post(
        "/api/ask",
        json={"question": case["question"], "history": case.get("history", []), "debug": True},
    )
    resp.raise_for_status()
    body = resp.json()
    elapsed = time.monotonic() - started
    answerable = case.get("expect_found", True)
    result = CaseResult(
        id=case["id"],
        answerable=answerable,
        found=body["found"],
        seconds=round(elapsed, 2),
        answer=body["answer"],
        cited_sources=[f"[{s['id']}] {_label(s)}" for s in body["sources"]],
        retrieved_sources=[f"[{s['id']}] {_label(s)}" for s in body.get("retrieved", [])],
    )
    version = body.get("prompt_version")
    if not answerable:
        return result, version
    expected = case["expect"]
    result.retrieval_hit = any(_matches(s, e) for s in body["retrieved"] for e in expected)
    relevant = [s for s in body["sources"] if any(_matches(s, e) for e in expected)]
    result.cited = len(body["sources"])
    result.cited_relevant = len(relevant)
    result.citation_hit = bool(relevant)
    answer = body["answer"].lower()
    result.answer_correct = body["found"] and all(
        any(alt.lower() in answer for alt in group) for group in case["answer_any"]
    )
    return result, version


def summarise(results: list[CaseResult]) -> dict[str, float]:
    ans = [r for r in results if r.answerable]
    un = [r for r in results if not r.answerable]

    def rate(xs: list[bool]) -> float:
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    cited = sum(r.cited for r in ans)
    return {
        "answerable_cases": len(ans),
        "unanswerable_cases": len(un),
        "retrieval_recall": rate([r.retrieval_hit for r in ans]),
        "citation_recall": rate([r.citation_hit for r in ans]),
        "citation_precision": round(sum(r.cited_relevant for r in ans) / cited, 3)
        if cited
        else 0.0,
        "answer_accuracy": rate([r.answer_correct for r in ans]),
        "false_not_found": rate([not r.found for r in ans]),
        "abstention": rate([not r.found for r in un]),
        "median_seconds": round(statistics.median(r.seconds for r in results), 2),
        "max_seconds": max(r.seconds for r in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("RAG_BASE_URL", "https://proxy"))
    parser.add_argument("--label", default="", help="name for this run, e.g. 'rerank-L6'")
    parser.add_argument("--out", type=Path, default=Path("/results"))
    parser.add_argument("--only", nargs="*", help="run only these case ids")
    args = parser.parse_args()

    cases = json.loads((HERE / "cases.json").read_text())
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]

    admin = _login(args.base, os.environ["RAG_ADMIN_USER"], os.environ["RAG_ADMIN_PASSWORD"])
    health = admin.get("/api/health").json()
    with tempfile.TemporaryDirectory() as tmp:
        password = setup(admin, Path(tmp))
    user = _login(args.base, USER, password)

    results: list[CaseResult] = []
    prompt_version = None
    for case in cases:
        result, prompt_version = run_case(user, case)
        results.append(result)
        mark = (
            ("ok " if result.answer_correct else "BAD")
            if result.answerable
            else ("ok " if not result.found else "BAD")
        )
        print(f"{mark} {result.id:<22} {result.seconds:6.1f}s  cites: {result.cited_sources}")
        sys.stdout.flush()

    metrics = summarise(results)
    report = {
        "label": args.label,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": health.get("models"),
        "reranker": health.get("reranker"),
        "prompt_version": prompt_version,
        "metrics": metrics,
        "cases": [asdict(r) for r in results],
    }
    print()
    for key, value in metrics.items():
        print(f"  {key:<20} {value}")
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = args.out / f"eval-{stamp}{'-' + args.label if args.label else ''}.json"
    path.write_text(json.dumps(report, indent=2))
    print(f"\nReport: {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
