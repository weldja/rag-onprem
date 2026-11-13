"""The eval corpus must load, and every expected source in cases.json must be
producible by our loaders; otherwise the eval measures nothing."""

import json
from pathlib import Path

import httpx

from app.ingestion.loaders import load_file
from evals.corpus import build
from evals.run_eval import CaseResult, _matches, ask_streamed, summarise

CASES = json.loads((Path(__file__).parent.parent / "evals" / "cases.json").read_text())


def test_every_expected_source_exists_in_the_loaded_corpus(tmp_path: Path) -> None:
    pieces = {}
    for path in build(tmp_path):
        loaded = load_file(path, path.suffix)
        assert loaded.pages, path.name
        pieces[path.name] = [
            {
                "document": path.name,
                "page": p.metadata.get("page"),
                "section": p.metadata.get("section"),
            }
            for p in loaded.pages
        ]
    for case in CASES:
        for expected in case.get("expect", []):
            candidates = pieces[expected["document"]]
            assert any(_matches(c, expected) for c in candidates), (case["id"], expected)


def test_case_ids_are_unique_and_well_formed() -> None:
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    for c in CASES:
        if c.get("library"):  # answered from the database: no sources to expect
            assert c["answer_any"] and "expect" not in c, c["id"]
        elif c.get("expect_found", True):
            assert c["expect"] and c["answer_any"], c["id"]


def test_library_cases_are_scored_on_their_own() -> None:
    results = [
        CaseResult("q", True, True, True, True, 1, 1, True, 1.0),
        CaseResult("lib", True, True, answer_correct=True, seconds=0.1, library=True),
    ]
    m = summarise(results)
    assert m["answerable_cases"] == 1 and m["retrieval_recall"] == 1.0
    assert (m["library_cases"], m["library_accuracy"]) == (1, 1.0)


def test_matching_and_summary() -> None:
    src = {"document": "a.xlsx", "page": None, "section": "Sheet Hotels, rows 2-5"}
    assert _matches(src, {"document": "a.xlsx", "section": "Hotels"})
    assert not _matches(src, {"document": "a.xlsx", "section": "Mileage"})
    assert not _matches({"document": "b.pdf", "page": 2}, {"document": "b.pdf", "page": 3})

    results = [
        CaseResult("a", True, True, True, True, 2, 1, True, 1.0),
        CaseResult("b", True, False, False, False, 0, 0, False, 3.0),
        CaseResult("c", False, False, seconds=2.0),
    ]
    m = summarise(results)
    assert m["retrieval_recall"] == 0.5
    assert m["citation_precision"] == 0.5
    assert m["false_not_found"] == 0.5
    assert m["abstention"] == 1.0
    assert m["median_seconds"] == 2.0
    assert m["summary_cases"] == 0


def test_summary_cases_are_scored_separately() -> None:
    results = [
        CaseResult("q", True, True, True, True, 1, 1, True, 4.0),
        CaseResult("s1", True, True, True, True, 3, 3, True, 20.0, summary=True),
        CaseResult("s2", True, True, True, False, 0, 0, True, 30.0, summary=True),
    ]
    m = summarise(results)
    assert m["answerable_cases"] == 1 and m["citation_recall"] == 1.0  # summaries excluded
    assert m["median_seconds"] == 4.0
    assert m["summary_cases"] == 2
    assert m["summary_cited"] == 0.5
    assert m["summary_accuracy"] == 1.0
    assert m["summary_median_seconds"] == 25.0


def _sse_client(events: list[tuple[str, dict[str, object]]]) -> httpx.Client:
    body = "".join(f"event: {k}\ndata: {json.dumps(v)}\n\n" for k, v in events)
    return httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)),
    )


def test_ask_streamed_returns_done_and_times_the_first_word() -> None:
    client = _sse_client(
        [
            ("status", {"stage": "retrieving"}),
            ("token", {"text": "  "}),  # blank: not a word yet
            ("token", {"text": "Five days [1]."}),
            ("done", {"answer": "Five days [1].", "found": True, "sources": []}),
        ]
    )
    body, first_word = ask_streamed(client, {"question": "q"})
    assert body["answer"] == "Five days [1]."
    assert 0 <= first_word < 5


def test_ask_streamed_without_tokens_times_the_done_event() -> None:
    client = _sse_client([("done", {"answer": "not found", "found": False, "sources": []})])
    body, first_word = ask_streamed(client, {"question": "q"})
    assert body["found"] is False and first_word >= 0


def test_summary_cases_name_a_corpus_document(tmp_path: Path) -> None:
    names = {p.name for p in build(tmp_path)}
    for case in CASES:
        if "summary_of" in case:
            assert case["summary_of"] in names, case["id"]
