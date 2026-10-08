"""Staff passwords (bcrypt) and access tokens (JWT, HS256) (ARCHITECTURE.md §2, §10)."""

import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import bcrypt
import jwt

from app.core.config import Settings

ALGORITHM = "HS256"


class AuthNotConfigured(RuntimeError):
    """JWT_SECRET is empty, so no token can be issued or checked."""


def hash_password(password: str) -> str:
    # Same scheme as db/seed/seed.py. bcrypt refuses more than 72 bytes.
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:  # longer than 72 bytes, or not a bcrypt hash
        return False


@lru_cache
def _dummy_hash() -> str:
    return hash_password("no such staff user")


def burn_password_check(password: str) -> None:
    """Spend the time a real check costs, so an unknown email answers as slowly as a wrong password."""
    verify_password(password, _dummy_hash())


def _secret(settings: Settings) -> str:
    if not settings.jwt_secret:
        raise AuthNotConfigured("JWT_SECRET is empty in backend/.env")
    return settings.jwt_secret


def create_access_token(staff_id: uuid.UUID, role: str, settings: Settings) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.jwt_expire_minutes)
    claims = {"sub": str(staff_id), "role": role, "iat": now, "exp": expires_at}
    return jwt.encode(claims, _secret(settings), algorithm=ALGORITHM), expires_at


def staff_id_from_token(token: str, settings: Settings) -> uuid.UUID | None:
    """The staff id a valid, unexpired token was issued to; None for anything else."""
    secret = _secret(settings)
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["sub", "exp"]})
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError):
        return None
