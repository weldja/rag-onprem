import pytest

from app.core.config import Settings
from app.retrieval.rerank import rerank
from app.retrieval.search import rrf_merge, select_hits, spread_positions, to_or_tsquery
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


def test_respects_k() -> None:
    vector = [chunk(i, score=0.9 - i / 100) for i in range(10)]
    assert len(select_hits(vector, [], k=3, min_similarity=0.35, keyword_keep=0)) == 3
