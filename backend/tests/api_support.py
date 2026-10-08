"""Shared helpers for API tests against the real database, inside a transaction that is rolled back.

The dev database is shared by every laptop: these tests never leave a row behind.
"""

import uuid
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import DB_ERRORS, get_session, make_engine
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models import StaffUser

SECRET = "a" * 64
PASSWORD = "right-password"


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, jwt_secret=SECRET, **overrides)


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def session():
    url = get_settings().database_url
    engine = make_engine(url)
    try:
        conn = await engine.connect()
    except (*DB_ERRORS, TimeoutError) as exc:
        await engine.dispose()
        pytest.skip(f"database unreachable at {urlsplit(url).hostname} ({type(exc).__name__})")
    transaction = await conn.begin()
    db = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield db
    finally:
        await db.close()
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture
def app(session):
    app = create_app(settings())

    async def same_session():
        yield session

    app.dependency_overrides[get_session] = same_session
    return app


async def add_staff(session, role: str = "agent", name: str = "Test Agent") -> StaffUser:
    staff = StaffUser(name=name, email=f"test-{role}-{uuid.uuid4().hex[:8]}@example.com",
                      password_hash=hash_password(PASSWORD), role=role)
    session.add(staff)
    await session.flush()
    return staff


def bearer(staff: StaffUser) -> dict[str, str]:
    token, _ = create_access_token(staff.id, staff.role, settings())
    return {"Authorization": f"Bearer {token}"}
