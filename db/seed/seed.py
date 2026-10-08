"""Wipe every app table and seed the demo data (make seed, ARCHITECTURE.md §8.2).

Destroys every row in the database DATABASE_URL points at (backend/.env): run it
against the Supabase dev project only. Every run inserts the same rows with the
same ids, so links and bookmarks survive a re-seed.

Block 1 seeds the staff logins, the four demo-story customers and their devices,
the product models, parts and their compatibility, the service catalog and the
playbooks (db/seed/data/*.json). Not seeded yet: the warehouse and its stock, the
other customers and ~600 units, historical tickets, and embeddings (later blocks).
"""

import asyncio
import calendar
import json
import os
import re
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import bcrypt
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(__file__).resolve().parent / "data"
SCHEMA = ROOT / "db" / "schema.sql"
ENV_FILE = ROOT / "backend" / ".env"

# Every id is uuid5(SEED_NAMESPACE, "<table>:<natural key>"): the same on every run.
SEED_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/ImpactX26/Squad/seed")

# Vocabularies from §8.1 (CHECK constraints) and §4.4 (intake's issue types).
ROLES = {"agent", "technician", "warehouse", "admin"}
MODEL_CATEGORIES = {"laptop", "desktop", "headphones", "accessory"}
PART_TYPES = {"battery", "cmos_battery", "ram", "ssd", "charger", "keyboard", "display", "fan", "ear_cushion", "cable", "other"}
ISSUE_TYPES = {"battery", "charging", "display", "keyboard", "audio", "overheating", "boot", "os", "driver", "performance", "connectivity", "other"}

# Insert order follows the foreign keys. JSONB columns get a ::jsonb cast.
COLUMNS: dict[str, tuple[str, ...]] = {
    "staff_users": ("id", "name", "email", "password_hash", "role", "skills", "city"),
    "customers": ("id", "full_name", "email"),
    "product_models": ("id", "model_number", "brand", "name", "category", "specs", "warranty_months"),
    "products": ("id", "serial_number", "model_id", "color", "config", "purchase_date", "warranty_until", "customer_id"),
    "parts": ("id", "sku", "name", "part_type", "unit_price", "specs"),
    "part_compatibility": ("part_id", "model_id"),
    "service_catalog": ("id", "code", "name", "part_type", "labour_fee", "requires_visit", "required_skill"),
    "kb_playbooks": ("id", "issue_type", "category", "model_id", "title", "steps"),
}
JSONB_COLUMNS = {"specs", "config", "steps"}


class SeedDataError(ValueError):
    """A record in db/seed/data breaks the schema's vocabularies or another record."""


def seed_id(table: str, key: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, f"{table}:{key}")


def load_data(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    names = ("staff", "customers", "models", "parts", "services", "playbooks")
    return {name: json.loads((data_dir / f"{name}.json").read_text(encoding="utf-8")) for name in names}


def add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def money(value: str) -> Decimal:
    amount = Decimal(value)
    if amount <= 0 or amount.as_tuple().exponent != -2:
        raise SeedDataError(f"amount {value!r} must be positive with exactly two decimals")
    return amount


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SeedDataError(message)


def unique(values: list[str], what: str) -> None:
    seen = set()
    for value in values:
        check(value not in seen, f"duplicate {what}: {value}")
        seen.add(value)


def build_rows(data: dict[str, list[dict]], password_hash: str) -> dict[str, list[tuple]]:
    """Validate the seed data and turn it into rows, in COLUMNS order, per table."""
    rows: dict[str, list[tuple]] = {table: [] for table in COLUMNS}

    services = data["services"]
    unique([s["code"] for s in services], "service code")
    visit_skills = {s["required_skill"] for s in services if s["required_skill"]}
    for s in services:
        check(s["part_type"] is None or s["part_type"] in PART_TYPES, f"service {s['code']}: unknown part_type {s['part_type']}")
        check(not s["requires_visit"] or bool(s["required_skill"]), f"service {s['code']}: a visit needs a required_skill")
        rows["service_catalog"].append((
            seed_id("service_catalog", s["code"]), s["code"], s["name"], s["part_type"],
            money(s["labour_fee"]), s["requires_visit"], s["required_skill"],
        ))

    unique([m["email"] for m in data["staff"]], "staff email")
    for m in data["staff"]:
        check(m["email"] == m["email"].strip().lower(), f"staff email {m['email']} must be lowercase")
        check(m["role"] in ROLES, f"staff {m['email']}: unknown role {m['role']}")
        skills = m.get("skills", [])
        if m["role"] == "technician":
            check(bool(m.get("city")) and bool(skills), f"technician {m['email']} needs a city and skills")
            check(set(skills) <= visit_skills, f"technician {m['email']}: skills {sorted(set(skills) - visit_skills)} match no service")
        rows["staff_users"].append((
            seed_id("staff_users", m["email"]), m["name"], m["email"], password_hash, m["role"], skills, m.get("city"),
        ))

    models = {m["model_number"]: m for m in data["models"]}
    unique([m["model_number"] for m in data["models"]], "model number")
    for m in data["models"]:
        check(m["category"] in MODEL_CATEGORIES, f"model {m['model_number']}: unknown category {m['category']}")
        rows["product_models"].append((
            seed_id("product_models", m["model_number"]), m["model_number"], m["brand"], m["name"],
            m["category"], json.dumps(m["specs"]), m["warranty_months"],
        ))

    unique([c["email"] for c in data["customers"]], "customer email")
    unique([d["serial_number"] for c in data["customers"] for d in c["devices"]], "serial number")
    for c in data["customers"]:
        customer_id = seed_id("customers", c["email"])
        rows["customers"].append((customer_id, c["full_name"], c["email"]))
        for d in c["devices"]:
            model = models.get(d["model_number"])
            check(model is not None, f"device {d['serial_number']}: unknown model {d['model_number']}")
            # §7.1: a serial is "<model number>-<6 characters>".
            check(
                re.fullmatch(re.escape(d["model_number"]) + r"-[A-Z0-9]{6}", d["serial_number"]) is not None,
                f"serial {d['serial_number']} is not {d['model_number']}-<6 characters>",
            )
            purchased = date.fromisoformat(d["purchase_date"])
            rows["products"].append((
                seed_id("products", d["serial_number"]), d["serial_number"], seed_id("product_models", d["model_number"]),
                d["color"], json.dumps(d["config"]), purchased, add_months(purchased, model["warranty_months"]), customer_id,
            ))

    unique([p["sku"] for p in data["parts"]], "part SKU")
    for p in data["parts"]:
        check(p["part_type"] in PART_TYPES, f"part {p['sku']}: unknown part_type {p['part_type']}")
        check(bool(p["fits"]), f"part {p['sku']} fits no model")
        part_id = seed_id("parts", p["sku"])
        rows["parts"].append((part_id, p["sku"], p["name"], p["part_type"], money(p["unit_price"]), json.dumps(p["specs"])))
        unique(p["fits"], f"model in {p['sku']}.fits")
        for model_number in p["fits"]:
            check(model_number in models, f"part {p['sku']} fits unknown model {model_number}")
            rows["part_compatibility"].append((part_id, seed_id("product_models", model_number)))

    unique([p["key"] for p in data["playbooks"]], "playbook key")
    for p in data["playbooks"]:
        check(p["issue_type"] in ISSUE_TYPES, f"playbook {p['key']}: unknown issue_type {p['issue_type']}")
        # kb_playbooks.category is the product category: model_id null applies to every model in it (§8.1).
        check(p["category"] in MODEL_CATEGORIES, f"playbook {p['key']}: unknown category {p['category']}")
        check(bool(p["steps"]), f"playbook {p['key']} has no steps")
        for step in p["steps"]:
            check(set(step) == {"step", "expected", "resolves_if"}, f"playbook {p['key']}: a step needs step, expected, resolves_if")
        model_number = p.get("model_number")
        check(model_number is None or model_number in models, f"playbook {p['key']}: unknown model {model_number}")
        rows["kb_playbooks"].append((
            seed_id("kb_playbooks", p["key"]), p["issue_type"], p["category"],
            seed_id("product_models", model_number) if model_number else None, p["title"], json.dumps(p["steps"]),
        ))

    return rows


def hash_password(password: str) -> str:
    # Same scheme as backend/app/core/security.py. bcrypt only reads the first 72 bytes.
    if len(password.encode()) > 72:
        raise SeedDataError("SEED_STAFF_PASSWORD is longer than 72 bytes, which bcrypt can't hash")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def app_tables() -> list[str]:
    return re.findall(r"^CREATE TABLE (\w+)", SCHEMA.read_text(encoding="utf-8"), flags=re.MULTILINE)


def insert_sql(table: str) -> str:
    columns = COLUMNS[table]
    values = ", ".join(f"${i}::jsonb" if c in JSONB_COLUMNS else f"${i}" for i, c in enumerate(columns, start=1))
    return f'INSERT INTO "{table}" ({", ".join(columns)}) VALUES ({values})'


async def seed(conn: asyncpg.Connection, staff_password: str) -> dict[str, int]:
    """Wipe every app table and insert the seed data, in one transaction. Returns rows per table."""
    rows = build_rows(load_data(), hash_password(staff_password))
    tables = ", ".join(f'"{t}"' for t in app_tables())
    async with conn.transaction():
        await conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
        # ticket_seq feeds the default ticket number, not an identity column, so it needs its own restart.
        await conn.execute("ALTER SEQUENCE ticket_seq RESTART WITH 1")
        for table in COLUMNS:
            await conn.executemany(insert_sql(table), rows[table])
    return {table: len(rows[table]) for table in COLUMNS}


def env_value(name: str) -> str:
    # Same precedence as app/core/config.py: the environment wins over backend/.env.
    return os.environ.get(name) or dotenv_values(ENV_FILE).get(name) or ""


def warranty_label(until: date) -> str:
    return f"in warranty until {until}" if until >= date.today() else f"out of warranty since {until}"


async def main() -> None:
    url = env_value("DATABASE_URL").replace("postgresql+asyncpg://", "postgresql://", 1)
    if not url:
        sys.exit(f"DATABASE_URL is not set (environment or {ENV_FILE})")
    password = env_value("SEED_STAFF_PASSWORD")
    if not password:
        sys.exit("SEED_STAFF_PASSWORD is empty: set the shared demo password in backend/.env first (§13.2).")
    parts = urlsplit(url)
    print(f"Seeding {parts.hostname}:{parts.port or 5432}{parts.path} (every app row is wiped first)")

    # The Transaction pooler (port 6543) does not support prepared statements (§8).
    conn = await asyncpg.connect(url, **({"statement_cache_size": 0} if parts.port == 6543 else {}))
    try:
        counts = await seed(conn, password)
    finally:
        await conn.close()

    for table, count in counts.items():
        print(f"  {count:4d}  {table}")
    data = load_data()
    print("Staff logins (password: SEED_STAFF_PASSWORD):")
    for m in data["staff"]:
        print(f"  {m['role']:<10} {m['email']:<22} {m['name']}" + (f", {m['city']}" if m.get("city") else ""))
    models = {m["model_number"]: m for m in data["models"]}
    print("Demo customers:")
    for c in data["customers"]:
        for d in c["devices"]:
            model = models[d["model_number"]]
            until = add_months(date.fromisoformat(d["purchase_date"]), model["warranty_months"])
            print(f"  {c['demo_channel']:<9} {c['full_name']:<15} {d['serial_number']}  {model['name']}, {d['color']}, {warranty_label(until)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SeedDataError as exc:
        sys.exit(f"Seed data error: {exc}")
