"""Drop the app tables, apply db/schema.sql and verify every table exists (make db-reset).

Destroys all app data in the database DATABASE_URL points at (backend/.env).
Laptops point at the Supabase dev project; never run this against prod by accident.
"""

import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "db" / "schema.sql"
ENV_FILE = ROOT / "backend" / ".env"


def database_url() -> str:
    # Same precedence as app/core/config.py: the environment wins over backend/.env.
    url = os.environ.get("DATABASE_URL") or dotenv_values(ENV_FILE).get("DATABASE_URL")
    if not url:
        sys.exit(f"DATABASE_URL is not set (environment or {ENV_FILE})")
    # asyncpg wants a plain postgresql:// URL, not SQLAlchemy's dialect form.
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def schema_objects(sql: str) -> tuple[list[str], list[str]]:
    tables = re.findall(r"^CREATE TABLE (\w+)", sql, flags=re.MULTILINE)
    sequences = re.findall(r"^CREATE SEQUENCE (\w+)", sql, flags=re.MULTILINE)
    return tables, sequences


async def main() -> None:
    url = database_url()
    parts = urlsplit(url)
    print(f"Database: {parts.hostname}:{parts.port or 5432}{parts.path}")

    sql = SCHEMA.read_text(encoding="utf-8")
    tables, sequences = schema_objects(sql)

    # The Transaction pooler (port 6543) does not support prepared statements (§8).
    kwargs = {"statement_cache_size": 0} if parts.port == 6543 else {}
    conn = await asyncpg.connect(url, **kwargs)
    try:
        print(await conn.fetchval("SELECT version()"))
        async with conn.transaction():
            for table in reversed(tables):
                await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
            for sequence in sequences:
                await conn.execute(f'DROP SEQUENCE IF EXISTS "{sequence}" CASCADE')
            await conn.execute(sql)

        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
        present = {r["table_name"] for r in rows}
        missing = [t for t in tables if t not in present]
        extensions = await conn.fetch(
            "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector', 'pgcrypto') ORDER BY extname"
        )
    finally:
        await conn.close()

    print("Extensions: " + ", ".join(f"{e['extname']} {e['extversion']}" for e in extensions))
    print(f"Tables ({len(tables) - len(missing)}/{len(tables)}):")
    for table in tables:
        print(f"  {'ok     ' if table in present else 'MISSING'} {table}")
    if missing:
        sys.exit(f"Missing tables: {', '.join(missing)}")
    print("Schema applied and verified.")


if __name__ == "__main__":
    asyncio.run(main())
