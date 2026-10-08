"""JWT auth: tokens and passwords, then /api/auth/login and /api/me against the real database.

The database tests add their staff user inside a transaction that is rolled back, so the
shared dev database is left as it was.
"""

import uuid
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import DB_ERRORS, get_session, make_engine
from app.core.security import (
    AuthNotConfigured,
    create_access_token,
    hash_password,
    staff_id_from_token,
    verify_password,
)
from app.main import create_app
from app.models import StaffUser

SECRET = "a" * 64
PASSWORD = "right-password"


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, jwt_secret=SECRET, **overrides)


# ---------- passwords and tokens ----------


def test_a_password_verifies_only_against_its_own_hash():
    password_hash = hash_password(PASSWORD)
    assert verify_password(PASSWORD, password_hash)
    assert not verify_password("wrong-password", password_hash)
    assert not verify_password("x" * 100, password_hash)  # past bcrypt's 72 bytes
    assert not verify_password(PASSWORD, "not-a-bcrypt-hash")


def test_a_token_carries_the_staff_id():
    staff_id = uuid.uuid4()
    token, _ = create_access_token(staff_id, "agent", settings())
    assert staff_id_from_token(token, settings()) == staff_id


@pytest.mark.parametrize(
    "token",
    [
        create_access_token(uuid.uuid4(), "agent", settings(jwt_expire_minutes=-1))[0],  # expired
        create_access_token(uuid.uuid4(), "agent", Settings(_env_file=None, jwt_secret="b" * 64))[0],  # other secret
        "not.a.token",
        "",
    ],
)
def test_an_expired_forged_or_garbage_token_is_refused(token):
    assert staff_id_from_token(token, settings()) is None


def test_no_secret_means_no_tokens():
    no_secret = Settings(_env_file=None, jwt_secret="")
    with pytest.raises(AuthNotConfigured):
        create_access_token(uuid.uuid4(), "agent", no_secret)
    with pytest.raises(AuthNotConfigured):
        staff_id_from_token("anything", no_secret)


async def test_login_is_503_when_jwt_secret_is_empty():
    app = create_app(Settings(_env_file=None, jwt_secret=""))
    app.dependency_overrides[get_session] = lambda: None
    async with client(app) as c:
        response = await c.post("/api/auth/login", json={"email": "arjun@example.com", "password": PASSWORD})
    assert response.status_code == 503


async def test_me_without_a_token_is_401():
    app = create_app(settings())
    app.dependency_overrides[get_session] = lambda: None
    async with client(app) as c:
        response = await c.get("/api/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


# ---------- against the database ----------


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
async def agent(session) -> StaffUser:
    staff = StaffUser(
        name="Test Agent",
        email=f"test-agent-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password(PASSWORD),
        role="agent",
    )
    session.add(staff)
    await session.flush()
    return staff


@pytest.fixture
def app(session):
    app = create_app(settings())

    async def same_session():
        yield session

    app.dependency_overrides[get_session] = same_session
    return app


async def test_login_returns_a_token_that_opens_me(app, agent):
    async with client(app) as c:
        # The email is matched without case or surrounding spaces.
        login = await c.post("/api/auth/login", json={"email": f"  {agent.email.upper()} ", "password": PASSWORD})
        assert login.status_code == 200
        body = login.json()
        assert body["token_type"] == "bearer"
        assert body["staff"]["id"] == str(agent.id)
        assert body["staff"]["role"] == "agent"
        assert "password_hash" not in body["staff"]

        me = await c.get("/api/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == agent.email


async def test_a_wrong_password_and_an_unknown_email_get_the_same_401(app, agent):
    async with client(app) as c:
        wrong_password = await c.post("/api/auth/login", json={"email": agent.email, "password": "wrong-password"})
        unknown_email = await c.post("/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"detail": "Wrong email or password."}


async def test_me_refuses_a_garbage_token_and_a_token_for_a_missing_user(app, session):
    ghost_token, _ = create_access_token(uuid.uuid4(), "admin", settings())
    async with client(app) as c:
        garbage = await c.get("/api/me", headers={"Authorization": "Bearer not.a.token"})
        ghost = await c.get("/api/me", headers={"Authorization": f"Bearer {ghost_token}"})
    assert garbage.status_code == ghost.status_code == 401
