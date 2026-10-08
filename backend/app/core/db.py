from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


def asyncpg_connect_args(database_url: str) -> dict:
    # Supabase pooler can't use asyncpg's prepared-statement cache (ARCHITECTURE.md §8).
    return {"statement_cache_size": 0} if "pooler.supabase.com" in database_url else {}


_url = get_settings().database_url
# The API shares the Supabase Session pooler's 15 client slots with the seven MCP servers
# (mcp_servers/common/db.py), so it keeps three connections and opens at most two more; past that a
# request waits for one here rather than failing at the pooler with EMAXCONNSESSION.
engine = create_async_engine(_url, pool_pre_ping=True, pool_size=3, max_overflow=2,
                             connect_args=asyncpg_connect_args(_url))
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session