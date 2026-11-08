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


@pytest.mark.parametrize("pw", ["", "short", "eleven-char"])
def test_too_short_passwords(pw: str) -> None:
    with pytest.raises(WeakPasswordError):
        check_strength(pw, 12)


@pytest.mark.parametrize(
    "pw", [" leading-space-password", "trailing-space-pass ", "  ", "ünïcödé €😀 pass", "a\tb"]
)
def test_any_characters_allowed(pw: str) -> None:
    check_strength(pw, 2)
    assert verify_password(hash_password(pw), pw)


def test_minimum_can_be_one_but_never_empty() -> None:
    check_strength("x", 1)
    with pytest.raises(WeakPasswordError):
        check_strength("", 1)
    with pytest.raises(WeakPasswordError):
        check_strength("", 0)


def test_min_length_setting_bounds() -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings(password_min_length=1).password_min_length == 1
    with pytest.raises(ValidationError):
        Settings(password_min_length=0)


def test_session_tokens_are_stored_hashed() -> None:
    assert token_hash("abc") != "abc"
    assert len(token_hash("abc")) == 64
