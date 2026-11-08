from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with the library's current defaults

# Verified against when the username doesn't exist, so a failed login takes
# the same time whether or not the account exists.
_DUMMY_HASH = _hasher.hash("not-a-real-password-used-for-timing-only")


class WeakPasswordError(ValueError):
    pass


def check_strength(password: str, min_length: int) -> None:
    """Length is the only rule: any characters, including spaces, are allowed."""
    if not password or len(password) < min_length:
        raise WeakPasswordError(f"Password must be at least {max(min_length, 1)} characters")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
