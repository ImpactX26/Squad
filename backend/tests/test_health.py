from urllib.parse import urlsplit

import httpx
import pytest

from app.api.health import _ping
from app.core.config import Settings, get_settings
from app.core.db import DB_ERRORS, get_engine, make_engine
from app.main import create_app

ORIGIN = "http://localhost:3000"
# Nothing listens on port 1, so the connection is refused at once.
UNREACHABLE_URL = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"


def app_with_engine(engine):
    app = create_app(Settings(_env_file=None, cors_origins=ORIGIN))
    app.dependency_overrides[get_engine] = lambda: engine
    return app


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def real_engine():
    url = get_settings().database_url
    engine = make_engine(url)
    try:
        await _ping(engine)
    except (*DB_ERRORS, TimeoutError) as exc:
        await engine.dispose()
        pytest.skip(f"database unreachable at {urlsplit(url).hostname} ({type(exc).__name__})")
    yield engine
    await engine.dispose()


@pytest.fixture
async def unreachable_engine():
    engine = make_engine(UNREACHABLE_URL)
    yield engine
    await engine.dispose()


async def test_health_ok(real_engine):
    async with client(app_with_engine(real_engine)) as c:
        response = await c.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


async def test_health_503_when_db_unreachable(unreachable_engine):
    async with client(app_with_engine(unreachable_engine)) as c:
        response = await c.get("/api/health", headers={"Origin": ORIGIN})
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "unreachable"}
    assert response.headers["access-control-allow-origin"] == ORIGIN


async def test_db_error_on_any_route_is_503_with_cors(unreachable_engine):
    app = app_with_engine(unreachable_engine)

    @app.get("/test-db-error")
    async def db_error():
        await _ping(unreachable_engine)

    async with client(app) as c:
        response = await c.get("/test-db-error", headers={"Origin": ORIGIN})
    assert response.status_code == 503
    assert response.headers["access-control-allow-origin"] == ORIGIN
