"""db/seed/seed.py against the real database, inside a transaction that is rolled back.

The dev database is shared by every laptop, so this never leaves a trace: the seed's
TRUNCATE and inserts are undone at the end of the test.
"""

from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

import asyncpg
import bcrypt
import pytest

from app.core.config import get_settings
from tests.seed_support import seed


@pytest.fixture
async def conn():
    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        connection = await asyncpg.connect(url, timeout=15)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"database unreachable at {urlsplit(url).hostname} ({type(exc).__name__})")
    transaction = connection.transaction()
    await transaction.start()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


async def snapshot(conn) -> dict[str, list]:
    return {table: [r["id"] for r in await conn.fetch(f'SELECT id FROM "{table}" ORDER BY id')]
            for table in seed.COLUMNS if table != "part_compatibility"}


async def test_seeding_twice_gives_the_same_rows(conn):
    first_counts = await seed.seed(conn, "test-password")
    first = await snapshot(conn)
    second_counts = await seed.seed(conn, "test-password")
    second = await snapshot(conn)
    assert first_counts == second_counts
    assert first == second
    assert len(second["staff_users"]) == 9


async def test_the_seeded_rows_tell_the_demo_story(conn):
    await seed.seed(conn, "test-password")

    riya = await conn.fetchrow(
        """SELECT c.full_name, m.name AS model, p.color, p.warranty_until
           FROM products p JOIN customers c ON c.id = p.customer_id JOIN product_models m ON m.id = p.model_id
           WHERE p.serial_number = 'AX14-7F3K92'"""
    )
    assert (riya["full_name"], riya["model"], riya["color"]) == ("Riya Sharma", "Aurora 14", "Silver")
    assert riya["warranty_until"] == date(2025, 6, 12)

    # §5.5: labour plus the cheapest compatible part, by SKU on a tie.
    total = await conn.fetchval(
        """SELECT s.labour_fee + (
               SELECT p.unit_price FROM parts p JOIN part_compatibility pc ON pc.part_id = p.id
               JOIN product_models m ON m.id = pc.model_id
               WHERE m.model_number = 'AX14' AND p.part_type = s.part_type
               ORDER BY p.unit_price, p.sku LIMIT 1)
           FROM service_catalog s WHERE s.code = 'BATTERY_REPLACE'"""
    )
    assert total == Decimal("6.90")

    ravi = await conn.fetchrow("SELECT role, city, skills, password_hash FROM staff_users WHERE email = 'ravi@example.com'")
    assert (ravi["role"], ravi["city"]) == ("technician", "Bengaluru")
    assert "battery" in ravi["skills"]
    assert bcrypt.checkpw(b"test-password", ravi["password_hash"].encode())

    sequence = await conn.fetchrow("SELECT last_value, is_called FROM ticket_seq")
    assert (sequence["last_value"], sequence["is_called"]) == (1, False)
    assert await conn.fetchval("SELECT count(*) FROM tickets") == 0


def test_a_password_bcrypt_cannot_hash_is_refused():
    with pytest.raises(seed.SeedDataError):
        seed.hash_password("x" * 73)
