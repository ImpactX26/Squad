"""Apply db/schema.sql to DATABASE_URL (backend/.env), then verify every table exists.

From the repo root:
    uv run --project backend python db/apply_schema.py

Drops every table named in schema.sql first, so it destroys all app data.
"""

import asyncio
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import asyncpg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import asyncpg_connect_args  # noqa: E402

SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"
REQUIRED_EXTENSIONS = ("vector", "pgcrypto")


def schema_tables() -> list[str]:
    return re.findall(r"^CREATE TABLE (\w+)", SCHEMA_SQL.read_text(encoding="utf-8"), re.M)


async def main() -> int:
    url = get_settings().database_url
    target = make_url(url)
    print(f"Target: {target.host}:{target.port}/{target.database}")
    try:
        conn = await asyncpg.connect(
            target.set(drivername="postgresql").render_as_string(hide_password=False),
            timeout=20,
            **asyncpg_connect_args(url),
        )
    except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as e:
        print(f"Can't connect: {type(e).__name__}: {e}")
        print("Check DATABASE_URL in backend/.env.")
        return 1

    try:
        print((await conn.fetchval("SELECT version()")).split(",")[0])
        tables = schema_tables()
        async with conn.transaction():
            await conn.execute(f"DROP TABLE IF EXISTS {', '.join(tables)} CASCADE; DROP SEQUENCE IF EXISTS ticket_seq;")
            await conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        print(f"Applied db/schema.sql ({len(tables)} tables)")

        present = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            )
        }
        extensions = {
            r["extname"]: r["extversion"]
            for r in await conn.fetch("SELECT extname, extversion FROM pg_extension WHERE extname = ANY($1)", list(REQUIRED_EXTENSIONS))
        }
        has_seq = await conn.fetchval("SELECT to_regclass('ticket_seq') IS NOT NULL")
    finally:
        await conn.close()

    missing_tables = [t for t in tables if t not in present]
    missing_ext = [e for e in REQUIRED_EXTENSIONS if e not in extensions]
    for t in tables:
        print(f"  {'ok     ' if t in present else 'MISSING'} {t}")
    print("Extensions:", ", ".join(f"{k} {v}" for k, v in extensions.items()), "| ticket_seq:", "ok" if has_seq else "MISSING")
    if missing_tables or missing_ext or not has_seq:
        print(f"FAILED: missing tables {missing_tables}, extensions {missing_ext}")
        return 1
    print(f"Verified: all {len(tables)} tables, extensions, and ticket_seq exist.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))