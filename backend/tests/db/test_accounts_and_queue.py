import pytest
from sqlalchemy import Engine, update

from app.auth import accounts, sessions
from app.core.config import Settings
from app.db.tables import users
from app.ingestion import registry

pytestmark = [pytest.mark.db, pytest.mark.enable_socket]

PASSWORD = "a-long-test-password"


def test_login_creates_a_resolvable_session(db: Engine) -> None:
    settings = Settings()
    accounts.create_user(db, settings, username="Alice", password=PASSWORD)
    viewer = accounts.authenticate(db, "ALICE ", PASSWORD, settings)
    assert viewer is not None and viewer.username == "alice"
    token = sessions.create_session(db, viewer.id, settings)
    assert sessions.resolve(db, token, settings) == viewer
    sessions.end_session(db, token)
    assert sessions.resolve(db, token, settings) is None


def test_new_users_join_the_default_group(db: Engine) -> None:
    settings = Settings()
    user_id = accounts.create_user(db, settings, username="bob", password=PASSWORD)
    default = accounts.group_ids_by_name(db, [settings.default_group])
    assert accounts.get_user(db, user_id).group_ids == default


def test_lockout_after_repeated_failures(db: Engine) -> None:
    settings = Settings(login_max_failures=3)
    accounts.create_user(db, settings, username="carol", password=PASSWORD)
    for _ in range(3):
        assert accounts.authenticate(db, "carol", "wrong-password", settings) is None
    # Locked now, even with the right password.
    assert accounts.authenticate(db, "carol", PASSWORD, settings) is None
    with db.begin() as conn:
        conn.execute(update(users).values(locked_until=None))
    assert accounts.authenticate(db, "carol", PASSWORD, settings) is not None


def test_disabling_a_user_ends_their_sessions(db: Engine) -> None:
    settings = Settings()
    admin = accounts.create_user(db, settings, username="root", password=PASSWORD, is_admin=True)
    dave = accounts.create_user(db, settings, username="dave", password=PASSWORD)
    token = sessions.create_session(db, dave, settings)
    accounts.update_user(db, dave, acting_user_id=admin, disabled=True)
    assert sessions.resolve(db, token, settings) is None
    assert accounts.authenticate(db, "dave", PASSWORD, settings) is None


def test_password_change_keeps_only_the_current_session(db: Engine) -> None:
    settings = Settings()
    erin = accounts.create_user(db, settings, username="erin", password=PASSWORD)
    keep = sessions.create_session(db, erin, settings)
    other = sessions.create_session(db, erin, settings)
    accounts.set_password(
        db, settings, erin, "another-long-password", keep_session=sessions.token_hash(keep)
    )
    assert sessions.resolve(db, keep, settings) is not None
    assert sessions.resolve(db, other, settings) is None


def test_queue_hands_each_job_out_once(db: Engine) -> None:
    doc_id, created = registry.register(
        db,
        filename="a.txt",
        sha256="a" * 64,
        ext="txt",
        size=1,
        storage_path="/nonexistent/a.txt",
        enqueue=True,
    )
    assert created
    job = registry.claim_job(db)
    assert job is not None and job.document_id == doc_id and job.attempts == 1
    assert registry.claim_job(db) is None
    registry.finish_job(db, job.id, error="boom", retry=True)
    again = registry.claim_job(db)
    assert again is not None and again.id == job.id and again.attempts == 2


def test_duplicate_content_returns_existing_document(db: Engine) -> None:
    kwargs = dict(filename="a.txt", sha256="b" * 64, ext="txt", size=1, storage_path="/x")
    first, created = registry.register(db, enqueue=False, **kwargs)  # type: ignore[arg-type]
    second, created_again = registry.register(db, enqueue=False, **kwargs)  # type: ignore[arg-type]
    assert created and not created_again and first == second
