import pytest

from app.auth.passwords import (
    WeakPasswordError,
    check_strength,
    hash_password,
    verify_password,
)
from app.auth.sessions import token_hash


def test_hash_and_verify() -> None:
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")


def test_unknown_user_never_verifies() -> None:
    # The dummy hash is checked for timing; it must never succeed.
    assert not verify_password(None, "not-a-real-password-used-for-timing-only")


def test_garbage_hash_is_rejected_not_raised() -> None:
    assert not verify_password("not-a-hash", "x")


@pytest.mark.parametrize("pw", ["short", " leading-space-password", "trailing-space-pass "])
def test_weak_passwords(pw: str) -> None:
    with pytest.raises(WeakPasswordError):
        check_strength(pw, 12)


def test_session_tokens_are_stored_hashed() -> None:
    assert token_hash("abc") != "abc"
    assert len(token_hash("abc")) == 64
