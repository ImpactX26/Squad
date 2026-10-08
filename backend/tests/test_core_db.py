"""The API's connection pool (app/core/db.py): small enough to share the dev pooler's 15 clients."""

import asyncio
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import DB_ERRORS, make_engine


@pytest.mark.parametrize("port", [5432, 6543])
async def test_the_pool_holds_three_connections_plus_one_and_recycles_them(port):
    engine = make_engine(f"postgresql+asyncpg://nobody:nothing@127.0.0.1:{port}/none")
    try:
        pool = engine.pool
        assert pool.size() == 3
        assert pool._max_overflow == 1
        assert pool._recycle == 300
        assert pool._pre_ping is True
    finally:
        await engine.dispose()


async def test_a_fifth_connection_waits_for_one_of_the_four_to_come_back():
    url = get_settings().database_url
    engine = make_engine(url)
    held = []
    try:
        try:
            held.append(await engine.connect())
        except (*DB_ERRORS, TimeoutError) as exc:
            pytest.skip(f"database unreachable at {urlsplit(url).hostname} ({type(exc).__name__})")
        held += [await engine.connect() for _ in range(3)]
        assert engine.pool.checkedout() == 4

        fifth = asyncio.create_task(engine.connect().start())
        await asyncio.sleep(1)
        assert not fifth.done()  # no fifth connection is opened

        await held.pop().close()
        conn = await asyncio.wait_for(fifth, 10)  # it gets the one given back
        held.append(conn)
        assert (await conn.execute(text("SELECT 1"))).scalar() == 1
        assert engine.pool.checkedout() == 4
    finally:
        for conn in held:
            await conn.close()
        await engine.dispose()
