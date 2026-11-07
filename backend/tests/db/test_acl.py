"""Group-based access control, proven against real SQL.

The core promise: a user never retrieves (or lists, opens, or learns about)
chunks from a document that isn't shared with one of their groups.
"""

import pytest
from sqlalchemy import Engine, insert

from app.auth import accounts
from app.auth.acl import Viewer
from app.core.config import Settings, get_settings
from app.db.tables import chunks
from app.ingestion import registry
from app.retrieval.search import hybrid_search

pytestmark = [pytest.mark.db, pytest.mark.enable_socket]

SETTINGS = get_settings()
DIM = SETTINGS.embed_dim
PASSWORD = "a-long-test-password"

QUERY = [1.0] + [0.0] * (DIM - 1)


def _vec(i: int) -> list[float]:
    """Close to QUERY (cosine ~0.98) and slightly different per document."""
    v = list(QUERY)
    v[1 + i] = 0.2
    return v


def _viewer(engine: Engine, user_id: int) -> Viewer:
    u = accounts.get_user(engine, user_id)
    return Viewer(u.id, u.username, u.is_admin)


def _doc(
    engine: Engine, name: str, text: str, *, owner: int | None, groups: list[int], i: int
) -> int:
    doc_id, _ = registry.register(
        engine,
        filename=name,
        sha256=name.ljust(64, "0")[:64],
        ext="txt",
        size=len(text),
        storage_path=f"/nonexistent/{name}",
        enqueue=False,
        uploaded_by=owner,
        group_ids=groups,
    )
    with engine.begin() as conn:
        conn.execute(
            insert(chunks).values(
                document_id=doc_id,
                page=None,
                ordinal=0,
                text=text,
                embedding=_vec(i),
                embed_model=SETTINGS.embed_model,
            )
        )
        conn.exec_driver_sql("UPDATE documents SET status = 'ready' WHERE id = %s", (doc_id,))
    return doc_id


@pytest.fixture
def world(db: Engine) -> dict[str, int]:
    settings = Settings()
    finance = accounts.create_group(db, "Finance")
    hr = accounts.create_group(db, "HR")
    ids = {
        "finance": finance,
        "hr": hr,
        "fiona": accounts.create_user(
            db, settings, username="fiona", password=PASSWORD, group_ids=[finance]
        ),
        "harry": accounts.create_user(
            db, settings, username="harry", password=PASSWORD, group_ids=[hr]
        ),
        "admin": accounts.create_user(
            db, settings, username="root", password=PASSWORD, is_admin=True, group_ids=[]
        ),
    }
    ids["budget"] = _doc(
        db, "budget.txt", "Budget forecast zebracode", owner=None, groups=[finance], i=0
    )
    ids["salaries"] = _doc(
        db, "salaries.txt", "Salary bands quokkacode", owner=None, groups=[hr], i=1
    )
    ids["draft"] = _doc(db, "draft.txt", "Fiona private draft", owner=ids["fiona"], groups=[], i=2)
    return ids


def _search(engine: Engine, viewer: Viewer, question: str = "anything") -> set[str]:
    hits = hybrid_search(
        engine,
        question,
        QUERY,
        viewer=viewer,
        embed_model=SETTINGS.embed_model,
        k=10,
        candidates=30,
        min_similarity=0.1,
        keyword_keep=5,
    )
    return {h.filename for h in hits}


def test_retrieval_is_limited_to_shared_groups_and_own_uploads(
    db: Engine, world: dict[str, int]
) -> None:
    assert _search(db, _viewer(db, world["fiona"])) == {"budget.txt", "draft.txt"}
    assert _search(db, _viewer(db, world["harry"])) == {"salaries.txt"}
    assert _search(db, _viewer(db, world["admin"])) == {"budget.txt", "salaries.txt", "draft.txt"}


def test_keyword_leg_respects_acl(db: Engine, world: dict[str, int]) -> None:
    # An exact term from HR's document must not surface it for Finance.
    assert "salaries.txt" not in _search(db, _viewer(db, world["fiona"]), "quokkacode")
    assert "salaries.txt" in _search(db, _viewer(db, world["harry"]), "quokkacode")


def test_removing_a_user_from_a_group_takes_effect_immediately(
    db: Engine, world: dict[str, int]
) -> None:
    accounts.update_user(db, world["fiona"], acting_user_id=world["admin"], group_ids=[])
    assert _search(db, _viewer(db, world["fiona"])) == {"draft.txt"}


def test_listing_and_opening_follow_the_same_rules(db: Engine, world: dict[str, int]) -> None:
    harry = _viewer(db, world["harry"])
    listed = {d.filename for d in registry.list_documents(db, SETTINGS.embed_model, harry)}
    assert listed == {"salaries.txt"}
    with pytest.raises(registry.NotFoundError):
        registry.get_document(db, world["budget"], SETTINGS.embed_model, harry)
    with pytest.raises(registry.NotFoundError):
        registry.visible_file(db, world["budget"], harry)
    with pytest.raises(registry.NotFoundError):
        registry.delete_document(db, world["budget"], harry)


def test_only_uploader_or_admin_can_manage(db: Engine, world: dict[str, int]) -> None:
    fiona = _viewer(db, world["fiona"])
    # Fiona can see the Finance budget but didn't upload it.
    with pytest.raises(registry.ForbiddenError):
        registry.delete_document(db, world["budget"], fiona)
    info = registry.get_document(db, world["draft"], SETTINGS.embed_model, fiona)
    assert info.can_manage is True


def test_sharing_is_limited_to_own_groups(db: Engine, world: dict[str, int]) -> None:
    fiona = _viewer(db, world["fiona"])
    with pytest.raises(registry.ForbiddenError):
        registry.set_groups(db, world["draft"], [world["hr"]], fiona)
    registry.set_groups(db, world["draft"], [world["finance"]], fiona)
    assert registry.get_document(db, world["draft"], SETTINGS.embed_model, fiona).group_ids == [
        world["finance"]
    ]
