"""The eval corpus must load, and every expected source in cases.json must be
producible by our loaders; otherwise the eval measures nothing."""

import json
from pathlib import Path

from app.ingestion.loaders import load_file
from evals.corpus import build
from evals.run_eval import CaseResult, _matches, summarise

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
        if c.get("expect_found", True):
            assert c["expect"] and c["answer_any"], c["id"]


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
