import pytest

from app.core.config import Settings
from app.retrieval.rerank import rerank
from app.retrieval.search import (
    rrf_merge,
    select_hits,
    spread_positions,
    summary_positions,
    to_or_tsquery,
)
from tests.fakes import FakeReranker, chunk


def test_rerank_reorders_keeps_top_k_and_cosine_score() -> None:
    hits = [
        chunk(1, text="nothing here", score=0.9),
        chunk(2, text="leave leave leave", score=0.5),
        chunk(3, text="leave once", score=0.6),
    ]
    out = rerank(FakeReranker(), "leave policy", hits, k=2, min_score=None)
    assert [h.chunk_id for h in out] == [102, 103]
    assert out[0].score == 0.5 and out[0].rerank_score == 3.0


def test_rerank_min_score_drops_weak_chunks() -> None:
    hits = [chunk(1, text="leave"), chunk(2, text="unrelated")]
    out = rerank(FakeReranker(), "leave", hits, k=5, min_score=0.5)
    assert [h.chunk_id for h in out] == [101]


def test_rerank_of_nothing_is_nothing() -> None:
    assert rerank(FakeReranker(), "q", [], k=3, min_score=None) == []


@pytest.mark.parametrize(
    ("n", "k", "expected"),
    [(3, 5, [0, 1, 2]), (10, 4, [0, 3, 6, 9]), (100, 1, [0]), (5, 2, [0, 4])],
)
def test_spread_positions(n: int, k: int, expected: list[int]) -> None:
    assert spread_positions(n, k) == expected


def test_short_document_is_summarised_in_full() -> None:
    # 7 small sections (the real Complaints_Procedure.docx shape): sampling 6
    # skipped the section with the response deadlines.
    small = [80, 200, 300, 350, 300, 250, 200]
    assert summary_positions(small, 6, 6000) == list(range(7))
    assert summary_positions(small, 6, None) == spread_positions(7, 6)
    big = [1000] * 40
    assert summary_positions(big, 6, 6000) == spread_positions(40, 6)


def test_empty_env_values_mean_unset() -> None:
    assert Settings(rerank_min_score="").rerank_min_score is None  # type: ignore[arg-type]
    assert Settings(rerank_min_score="-2.5").rerank_min_score == -2.5  # type: ignore[arg-type]


def test_tsquery_strips_syntax_and_dedupes() -> None:
    assert to_or_tsquery("What's the 'VPN' & MFA policy? VPN!") == (
        "what | the | vpn | mfa | policy"
    )
    assert to_or_tsquery("!!! ? a") is None


def test_rrf_rewards_agreement_between_lists() -> None:
    a, b, c = chunk(1), chunk(2), chunk(3)
    merged = rrf_merge([[a, b, c], [c, b]])
    # b and c appear in both lists; c ranks first in the second list.
    assert [h.chunk_id for h in merged] == [c.chunk_id, b.chunk_id, a.chunk_id]


def test_off_topic_returns_nothing_even_with_keyword_hits() -> None:
    vector = [chunk(1, score=0.30), chunk(2, score=0.25)]
    keyword = [chunk(3, score=0.20)]
    assert select_hits(vector, keyword, k=5, min_similarity=0.35, keyword_keep=5) == []


def test_keeps_close_vector_hits_and_top_keyword_hits() -> None:
    vector = [chunk(1, score=0.7), chunk(2, score=0.5), chunk(3, score=0.2)]
    keyword = [chunk(4, score=0.1), chunk(5, score=0.15)]
    hits = select_hits(vector, keyword, k=10, min_similarity=0.35, keyword_keep=1)
    ids = {h.chunk_id for h in hits}
    assert ids == {101, 102, 104}  # 103 too far, 105 beyond keyword_keep


def test_identical_text_from_another_document_is_kept_once() -> None:
    vector = [
        chunk(1, score=0.9, text="Probation lasts 6 months.", filename="deck-v1.pptx"),
        chunk(2, score=0.89, text="Probation  lasts 6 MONTHS.", filename="deck-v2.pptx"),
        chunk(3, score=0.5, text="Reviews at 3 months."),
    ]
    hits = select_hits(vector, [], k=5, min_similarity=0.35, keyword_keep=0)
    assert [h.chunk_id for h in hits] == [101, 103]


def test_each_legs_best_hits_are_kept_even_if_fusion_ranks_them_low() -> None:
    # The closest chunk in meaning (1) isn't among the keyword hits; emails 2-9
    # appear in both legs at middling ranks, so fusion alone ranks them above it.
    best = chunk(1, score=0.65)
    emails = [chunk(n, score=0.55 - n / 100) for n in range(2, 10)]
    vector = [best, *emails]
    keyword = list(reversed(emails))
    plain = select_hits(vector, keyword, k=6, min_similarity=0.35, keyword_keep=5)
    assert 101 not in {h.chunk_id for h in plain}
    kept = select_hits(vector, keyword, k=6, min_similarity=0.35, keyword_keep=5, leg_keep=2)
    ids = [h.chunk_id for h in kept]
    assert len(ids) == 6 and 101 in ids and 102 in ids  # vector leg's top two
    assert 109 in ids and 108 in ids  # keyword leg's top two
    rank = {h.chunk_id: i for i, h in enumerate(rrf_merge([vector, keyword]))}
    assert ids == sorted(ids, key=rank.__getitem__)  # still in fused order


def test_respects_k() -> None:
    vector = [chunk(i, score=0.9 - i / 100) for i in range(10)]
    assert len(select_hits(vector, [], k=3, min_similarity=0.35, keyword_keep=0)) == 3
