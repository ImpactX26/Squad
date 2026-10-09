"""Seed the ServiceMesh database (ARCHITECTURE.md §8.2).

From the repo root (apply the schema first with db/apply_schema.py):
    uv run --project backend python db/seed/seed.py             # wipe every row, then seed
    uv run --project backend python db/seed/seed.py --dry-run   # build all rows and print counts; no DB access

The same code is the fast path behind POST /api/dev/reset-demo (`reseed`): embeddings come from
backend/.cache/seed_embeddings.json (keyed by model + text, so the model only runs for new text),
and the whole write is one transaction of three statements -- truncate, one INSERT per table fed
from a single JSON parameter, and the ticket sequence -- instead of a round trip per row.

Idempotent: a fixed RNG seed and uuid5 ids derived from natural keys give the same
rows and the same ids on every run, so demo links survive "Reset demo". Dates are
relative to today, so warranty status stays the same whenever you seed.
"""

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import BACKEND_DIR, get_settings  # noqa: E402
from app.models import (  # noqa: E402
    Address,
    Conversation,
    Customer,
    CustomerIdentity,
    DiagnosticStep,
    Inventory,
    KbPlaybook,
    Message,
    Part,
    PartCompatibility,
    Product,
    ProductModel,
    ServiceCatalog,
    StaffUser,
    Ticket,
    TicketEvent,
    Warehouse,
)

DATA = Path(__file__).resolve().parent / "data"
SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"
EMBEDDING_CACHE = BACKEND_DIR / ".cache" / "seed_embeddings.json"  # backend/.cache is git-ignored
ID_NAMESPACE = uuid.UUID("3d4f9a6e-2b1c-4e8a-9f0d-5a7b6c8e1d20")
RNG_SEED = 20261001
UNITS_PER_MODEL = 24
SERIAL_ALPHABET = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O
PRIORITIES = ["low", "medium", "high", "urgent"]
STREETS = ["Lake View Apartments", "Green Park Residency", "Sunrise Towers", "Palm Grove", "Silver Oak Enclave",
           "Orchid Heights", "Cedar Court", "Riverside Residency"]

# Insert order respects foreign keys.
INSERT_ORDER = [
    StaffUser, Customer, CustomerIdentity, Address,
    ProductModel, Product, Part, PartCompatibility, ServiceCatalog,
    Warehouse, Inventory, KbPlaybook,
    Ticket, Conversation, Message, TicketEvent, DiagnosticStep,
]


def uid(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(ID_NAMESPACE, f"{kind}:{key}")


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    days_in_month = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, days_in_month))


def weighted(rng: random.Random, weights: dict[str, int]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def schema_tables() -> list[str]:
    return re.findall(r"^CREATE TABLE (\w+)", SCHEMA_SQL.read_text(encoding="utf-8"), re.M)


def build(now: datetime, staff_password_hash: str, embed) -> dict[type, list[dict]]:
    rng = random.Random(RNG_SEED)
    today = now.date()
    rows: dict[type, list[dict]] = {m: [] for m in INSERT_ORDER}

    # ---------- Staff ----------
    ops = load("operations.json")
    agents: list[uuid.UUID] = []
    for s in ops["staff"]:
        sid = uid("staff", s["email"])
        if s["role"] in ("agent", "admin"):
            agents.append(sid)
        rows[StaffUser].append({
            "id": sid, "name": s["name"], "email": s["email"], "password_hash": staff_password_hash,
            "role": s["role"], "phone": s.get("phone"), "skills": s.get("skills", []),
            "city": s.get("city"), "is_available": True, "avatar_url": None,
        })

    # ---------- Catalog ----------
    models = load("product_models.json")
    model_by_number = {m["model_number"]: m for m in models}
    for m in models:
        rows[ProductModel].append({
            "id": uid("model", m["model_number"]), "model_number": m["model_number"], "brand": m["brand"],
            "name": m["name"], "category": m["category"], "specs": m["specs"],
            "warranty_months": m["warranty_months"], "image_url": None,
        })

    parts = load("parts.json")
    for p in parts:
        rows[Part].append({
            "id": uid("part", p["sku"]), "sku": p["sku"], "name": p["name"], "part_type": p["part_type"],
            "unit_price": p["unit_price"], "specs": p["specs"],
        })
        for mn in p["models"]:
            if mn not in model_by_number:
                raise ValueError(f"parts.json: {p['sku']} lists unknown model {mn}")
            rows[PartCompatibility].append({"part_id": uid("part", p["sku"]), "model_id": uid("model", mn)})

    for s in ops["service_catalog"]:
        rows[ServiceCatalog].append({"id": uid("service", s["code"]), **s})

    # ---------- Inventory ----------
    inv = ops["inventory"]
    overrides = {(o["sku"], o["warehouse"]): o["qty_on_hand"] for o in inv["overrides"]}
    # An override may set its own threshold (the configurable low-stock level, per part and warehouse).
    thresholds = {(o["sku"], o["warehouse"]): o["reorder_threshold"] for o in inv["overrides"] if "reorder_threshold" in o}
    lo, hi = inv["random_qty_range"]
    if lo <= inv["default_reorder_threshold"]:
        raise ValueError("random_qty_range must start above the reorder threshold, or restock alerts would be due at seed time")
    for w in ops["warehouses"]:
        rows[Warehouse].append({"id": uid("warehouse", w["name"]), **w})
    for p in parts:
        for w in ops["warehouses"]:
            rows[Inventory].append({
                "id": uid("inventory", f"{p['sku']}@{w['name']}"),
                "part_id": uid("part", p["sku"]), "warehouse_id": uid("warehouse", w["name"]),
                "qty_on_hand": overrides.get((p["sku"], w["name"]), rng.randint(lo, hi)), "qty_reserved": 0,
                "reorder_threshold": thresholds.get((p["sku"], w["name"]), inv["default_reorder_threshold"]),
                "reorder_qty": inv["default_reorder_qty"],
            })

    # ---------- Customers + addresses ----------
    cust = load("customers.json")
    cities = cust["cities"]
    customers: list[dict] = []  # working records: id, email, full_name, city, demo product (if any)
    for c in cust["demo_story"]:
        customers.append({**c, "demo": True})
    for c in cust["others"]:
        first, last = c["full_name"].lower().split(" ", 1)
        email = f"{first}.{last.replace(' ', '')}@example.com"
        phone = f"+91 9{rng.randint(1000, 9999)} {rng.randint(10000, 99999)}"
        customers.append({**c, "email": email, "phone": phone, "demo": False})

    for c in customers:
        c["id"] = uid("customer", c["email"])
        city = cities[c["city"]]
        rows[Customer].append({"id": c["id"], "full_name": c["full_name"], "email": c["email"], "phone": c["phone"]})
        rows[Address].append({
            "id": uid("address", c["email"]), "customer_id": c["id"],
            "line1": f"Flat {rng.randint(101, 1204)}, {rng.choice(STREETS)}",
            "line2": rng.choice(city["localities"]), "city": c["city"], "state": city["state"],
            "postal_code": f"{city['postal_prefix']}{rng.randint(1, 99):03d}",
            "location_url": None, "is_default": True,
        })

    # ---------- Products (one physical unit each) ----------
    serials: set[str] = set()
    units_by_model: dict[str, list[dict]] = {m["model_number"]: [] for m in models}

    def new_unit(mn: str, serial: str, color: str, config: dict, purchased_days_ago: int, owner: uuid.UUID | None) -> dict:
        if serial in serials:
            raise ValueError(f"duplicate serial {serial}")
        serials.add(serial)
        purchase = today - timedelta(days=purchased_days_ago)
        unit = {
            "id": uid("product", serial), "serial_number": serial, "model_id": uid("model", mn),
            "color": color, "config": config, "purchase_date": purchase,
            "warranty_until": add_months(purchase, model_by_number[mn]["warranty_months"]), "customer_id": owner,
        }
        units_by_model[mn].append(unit)
        return unit

    for c in customers:
        if c["demo"]:
            p = c["product"]
            new_unit(p["model_number"], p["serial_number"], p["color"], p["config"], p["purchased_days_ago"], c["id"])

    for m in models:
        mn = m["model_number"]
        while len(units_by_model[mn]) < UNITS_PER_MODEL:
            serial = f"{mn}-" + "".join(rng.choice(SERIAL_ALPHABET) for _ in range(6))
            if serial not in serials:
                new_unit(mn, serial, rng.choice(m["colors"]), rng.choice(m["configs"]), rng.randint(20, 1000), None)

    # Non-demo customers register a laptop, often headphones, sometimes a desktop.
    by_category = {cat: [mn for mn, m in model_by_number.items() if m["category"] == cat]
                   for cat in ("laptop", "desktop", "headphones")}
    owned: list[tuple[dict, dict]] = []  # (customer, unit)

    def claim(c: dict, category: str) -> None:
        free = [u for mn in by_category[category] for u in units_by_model[mn] if u["customer_id"] is None]
        unit = rng.choice(free)
        unit["customer_id"] = c["id"]
        owned.append((c, unit))

    for c in customers:
        if c["demo"]:
            continue
        claim(c, "laptop")
        if rng.random() < 0.5:
            claim(c, "headphones")
        if rng.random() < 0.2:
            claim(c, "desktop")

    for mn in units_by_model:
        rows[Product].extend(units_by_model[mn])

    # ---------- Playbooks ----------
    playbooks = load("playbooks.json")["playbooks"]
    playbook_by_key = {(pb["issue_type"], pb["category"]): pb for pb in playbooks}
    pb_vectors = embed([f"{pb['title']}. " + " ".join(s["step"] for s in pb["steps"]) for pb in playbooks])
    for pb, vec in zip(playbooks, pb_vectors):
        rows[KbPlaybook].append({
            "id": uid("playbook", f"{pb['category']}:{pb['issue_type']}:{pb['title']}"),
            "issue_type": pb["issue_type"], "category": pb["category"], "model_id": None,
            "title": pb["title"], "steps": pb["steps"], "embedding": vec,
        })

    # ---------- Historical tickets ----------
    spec = load("tickets.json")
    category_of_unit = {u["id"]: model_by_number[mn]["category"] for mn, us in units_by_model.items() for u in us}
    drafts = []
    for _ in range(spec["count"]):
        t = rng.choice(spec["templates"])
        candidates = [(c, u) for c, u in owned if category_of_unit[u["id"]] == t["product_category"]]
        if not candidates:
            raise ValueError(f"no customer owns a {t['product_category']} for template {t['titles'][0]}")
        c, unit = rng.choice(candidates)
        drafts.append({
            "template": t, "customer": c, "unit": unit,
            "created_at": now - timedelta(days=rng.uniform(0.2, spec["lookback_days"])),
            "channel": weighted(rng, spec["channel_weights"]),
            "status": weighted(rng, spec["status_weights"]),
            "priority": weighted(rng, spec["priority_weights"]),
            "title": rng.choice(t["titles"]),
            "description": rng.choice(t["descriptions"]),
            "followups": rng.randint(1, 3) if rng.random() < spec["followup_chance"] else 0,
        })
    drafts.sort(key=lambda d: d["created_at"])
    t_vectors = embed([f"{d['title']}\n{d['description']}" for d in drafts])

    thread_seq = Counter()
    identities: dict[tuple[str, uuid.UUID], str] = {}
    for c in customers:  # the email demo customer is already known by address
        if c["demo"] and c["channel"] == "email":
            identities[("email", c["id"])] = c["email"]

    for n, (d, vec) in enumerate(zip(drafts, t_vectors), start=1):
        t, c, unit, channel, status = d["template"], d["customer"], d["unit"], d["channel"], d["status"]
        created = d["created_at"]
        number = f"SR-{created.year}-{n:05d}"
        tid = uid("ticket", number)
        agent = rng.choice(agents) if status != "new" else None

        base = PRIORITIES.index(d["priority"])
        bumped = min(base + d["followups"] // 2, len(PRIORITIES) - 1)  # §7.2: +1 level per 2 follow-ups
        resolved_at = min(created + timedelta(days=rng.uniform(0.5, 6)), now) if status in ("resolved", "closed") else None
        last_activity = resolved_at or min(created + timedelta(hours=rng.uniform(1, 30)), now)

        rows[Ticket].append({
            "id": tid, "ticket_number": number, "customer_id": c["id"], "product_id": unit["id"],
            "source_channel": channel, "category": t["category"], "issue_type": t["issue_type"],
            "title": d["title"], "description": d["description"], "ai_summary": t["summary"],
            "status": status, "priority": PRIORITIES[bumped], "duplicate_count": d["followups"],
            "flags": ["out_of_warranty"] if unit["warranty_until"] < created.date() else [],
            "assigned_agent_id": agent, "embedding": vec,
            "created_at": created, "updated_at": last_activity, "resolved_at": resolved_at,
        })

        # Identity + conversation on the channel the customer used
        key = (channel, c["id"])
        if key not in identities:
            thread_seq[channel] += 1
            identities[key] = c["email"] if channel == "email" else f"seed-{channel}-user-{thread_seq[channel]}"
        thread_id = f"<seed-{number}@example.com>" if channel == "email" else f"seed-{channel}-thread-{number}"
        cid = uid("conversation", number)
        rows[Conversation].append({
            "id": cid, "customer_id": c["id"], "channel": channel, "external_thread_id": thread_id,
            "ticket_id": tid, "context": {}, "last_message_at": last_activity,
        })

        first_name = c["full_name"].split(" ")[0]
        timeline = [
            ("customer", None, d["description"], created),
            ("ai", None, f"Thanks, {first_name}. We've logged this as {number}. "
                         "A support specialist will review it shortly and keep you updated here.",
             created + timedelta(minutes=1)),
        ]
        for i in range(d["followups"]):
            timeline.append(("customer", None, "Any update on this? It's still happening.",
                             created + timedelta(hours=6 * (i + 1))))
        if agent:
            timeline.append(("agent", agent, spec["agent_replies"][
                "resolved" if status in ("resolved", "closed") else status], last_activity))
        for k, (sender, staff_id, body, at) in enumerate(timeline):
            rows[Message].append({
                "id": uid("message", f"{number}:{k}"), "conversation_id": cid, "ticket_id": tid,
                "sender_type": sender, "sender_staff_id": staff_id, "channel": channel, "body": body,
                "body_original": None, "is_internal_note": False, "attachments": [],
                "external_message_id": None, "created_at": min(at, now),
            })

        events = [("created", {"source_channel": channel}, "ai", created)]
        for i in range(d["followups"]):
            events.append(("followup", {"channel": channel}, "customer", created + timedelta(hours=6 * (i + 1))))
        if bumped != base:
            events.append(("priority_raised", {"from": d["priority"], "to": PRIORITIES[bumped]}, "system",
                           created + timedelta(hours=6 * d["followups"])))
        if status != "new":
            events.append(("status_changed", {"from": "new", "to": status}, str(agent), last_activity))
        for k, (etype, payload, actor, at) in enumerate(events):
            rows[TicketEvent].append({
                "id": uid("event", f"{number}:{k}"), "ticket_id": tid, "type": etype,
                "payload": payload, "actor": actor, "created_at": min(at, now),
            })

        # Diagnostics: what was tried and what worked
        steps = playbook_by_key[(t["issue_type"], t["product_category"])]["steps"]
        if status in ("resolved", "closed"):
            worked_at = rng.randint(0, len(steps) - 1)
            results = ["failed"] * worked_at + ["worked"]
        else:
            tried = 0 if status == "new" else rng.randint(1, min(2, len(steps) - 1))
            results = ["failed"] * tried + ["pending"] * (len(steps) - tried)
        for pos, result in enumerate(results, start=1):
            rows[DiagnosticStep].append({
                "id": uid("diagnostic", f"{number}:{pos}"), "ticket_id": tid, "position": pos,
                "step": steps[pos - 1]["step"], "suggested_by": "playbook", "result": result,
                "notes": None, "updated_at": last_activity,
            })

    for (channel, customer_id), external in identities.items():
        rows[CustomerIdentity].append({
            "id": uid("identity", f"{channel}:{external}"), "customer_id": customer_id, "channel": channel,
            "external_user_id": external, "display_name": None,
        })

    return rows


def cached_embed(texts: list[str]) -> list[list[float]]:
    """embed_texts with a file cache keyed by model + text, so unchanged seed text never reaches the model.

    JSON round-trips Python floats exactly, so a cached vector equals the one the model returned.
    The model (and fastembed's import) is only loaded for texts that are not cached yet.
    """
    model = get_settings().embedding_model
    keys = [hashlib.sha256(f"{model}\0{t}".encode()).hexdigest() for t in texts]
    try:
        cache: dict[str, list[float]] = json.loads(EMBEDDING_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    missing = [i for i, k in enumerate(keys) if k not in cache]
    if missing:
        from app.brain.embeddings import embed_texts

        for i, vector in zip(missing, embed_texts([texts[i] for i in missing])):
            cache[keys[i]] = vector
        EMBEDDING_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EMBEDDING_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return [cache[k] for k in keys]


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} for the seed insert")


def insert_all_sql(rows: dict[type, list[dict]]) -> tuple[str, dict]:
    """One INSERT per table inside a single statement, fed by two parameters. Returns (sql, params).

    `:doc` is every row as JSON, read back with `jsonb_populate_recordset`, which casts each value
    with the column's own input function (uuid, timestamptz, text[], jsonb...). The explicit column
    list leaves unnamed columns to their defaults, exactly as inserting the dicts through
    SQLAlchemy did. Embeddings are the bulk of the bytes, and as JSON text they are five times
    bigger and parsed one number at a time, so they travel in `:emb`, one flat float4[], and are
    cut back into vectors by position. The INSERTs are data-modifying CTEs of one statement, so
    foreign keys are checked once, after all of them.
    """
    document: dict[str, list[dict]] = {}
    floats: list[float] = []
    ctes = []
    for n, model in enumerate(INSERT_ORDER):
        table = model.__table__.name
        if not rows[model]:
            continue
        columns = list(rows[model][0])
        if any(list(r) != columns for r in rows[model]):
            raise ValueError(f"{table}: seed rows do not all have the same columns")
        select = [f"r.{c}" for c in columns if c != "embedding"]
        insert = [c for c in columns if c != "embedding"]
        if "embedding" in columns:
            dim = len(rows[model][0]["embedding"])
            if any(len(r["embedding"]) != dim for r in rows[model]):
                raise ValueError(f"{table}: embeddings differ in length")
            start = len(floats)
            for r in rows[model]:
                floats.extend(r["embedding"])
            # Postgres arrays are 1-based: row k (r.ordinality) owns elements start+(k-1)*dim+1 .. start+k*dim.
            lo, hi = f"{start + 1} + (r.ordinality - 1) * {dim}", f"{start} + r.ordinality * {dim}"
            select.append(f"CAST((CAST(:emb AS float4[]))[{lo}:{hi}] AS vector)")
            insert.append("embedding")
        document[table] = [{k: v for k, v in r.items() if k != "embedding"} for r in rows[model]]
        ctes.append(
            f"i{n} AS (INSERT INTO {table} ({', '.join(insert)}) SELECT {', '.join(select)} "
            f"FROM jsonb_populate_recordset(NULL::{table}, CAST(:doc AS jsonb) -> '{table}') "
            f"WITH ORDINALITY AS r RETURNING 1)"
        )
    sql = "WITH " + ", ".join(ctes) + " SELECT setval('ticket_seq', :n)"
    return sql, {"doc": json.dumps(document, default=_json_default, separators=(",", ":")), "emb": floats}


async def write(rows: dict[type, list[dict]], ticket_count: int, engine=None) -> dict[str, float]:
    """Truncate and refill every table in one transaction. Returns the seconds each step took.

    `engine` is the app's own engine when called from the API (left open); the script makes and
    disposes its own.
    """
    from sqlalchemy import text

    own_engine = engine is None
    if own_engine:
        from app.core.db import engine
    timings: dict[str, float] = {}
    t = time.perf_counter()
    sql, params = insert_all_sql(rows)
    timings["serialise"] = time.perf_counter() - t
    try:
        async with engine.begin() as conn:
            t = time.perf_counter()
            await conn.execute(text(f"TRUNCATE TABLE {', '.join(schema_tables())} RESTART IDENTITY CASCADE"))
            timings["truncate"] = time.perf_counter() - t
            t = time.perf_counter()
            # The next live ticket continues after the seeded ones (the setval at the end).
            await conn.execute(text(sql), {**params, "n": ticket_count})
            timings["insert"] = time.perf_counter() - t
            t = time.perf_counter()
        timings["commit"] = time.perf_counter() - t
    finally:
        if own_engine:
            await engine.dispose()
    return timings


async def reseed(engine=None) -> dict:
    """Wipe and re-seed the database. The script and POST /api/dev/reset-demo both run exactly this.

    Returns the rows, today's date, the seconds each phase took and the total.
    """
    from app.core.security import hash_password

    settings = get_settings()
    if not settings.seed_staff_password:
        raise RuntimeError("SEED_STAFF_PASSWORD is empty in backend/.env, so there is no staff password to seed")
    started = time.perf_counter()
    now = datetime.now(UTC)
    # bcrypt, the embedding cache and the row building are CPU-bound: keep them off the event loop.
    rows = await asyncio.to_thread(
        lambda: build(now, hash_password(settings.seed_staff_password), cached_embed))
    build_seconds = time.perf_counter() - started
    timings = await write(rows, len(rows[Ticket]), engine)
    return {
        "rows": rows,
        "today": now.date(),
        "phases": {"build": build_seconds, **timings},
        "seconds": time.perf_counter() - started,
    }


def print_demo_serials(rows: dict[type, list[dict]], today: date) -> None:
    models = {m["id"]: m["name"] for m in rows[ProductModel]}
    units = {p["serial_number"]: p for p in rows[Product]}
    print("\nDemo-story customers (known serials, no open tickets):")
    for c in load("customers.json")["demo_story"]:
        unit = units[c["product"]["serial_number"]]
        ends = unit["warranty_until"]
        warranty = f"warranty until {ends}" if ends >= today else f"warranty ended {ends}"
        print(f"  {c['channel']:<9} {c['full_name']:<13} {unit['serial_number']:<12} "
              f"{models[unit['model_id']]}, {unit['color']} ({warranty})")


def print_summary(rows: dict[type, list[dict]]) -> None:
    for model in INSERT_ORDER:
        print(f"  {model.__tablename__:<22} {len(rows[model]):>5}")
    tickets = rows[Ticket]
    print("  ticket status:   ", dict(Counter(t["status"] for t in tickets)))
    print("  ticket channel:  ", dict(Counter(t["source_channel"] for t in tickets)))
    print("  with follow-ups: ", sum(1 for t in tickets if t["duplicate_count"]))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="build rows and print counts without touching the DB")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.seed_staff_password:
        sys.exit("SEED_STAFF_PASSWORD is empty in backend/.env. Set it before seeding (it's the staff login password).")

    if args.dry_run:
        from app.core.security import hash_password

        started = time.perf_counter()
        now = datetime.now(UTC)
        rows = build(now, hash_password(settings.seed_staff_password), cached_embed)
        print(f"Built rows in {time.perf_counter() - started:.1f}s")
        print_summary(rows)
        print("Dry run: database not touched.")
        print_demo_serials(rows, now.date())
        return

    from sqlalchemy.engine import make_url

    print(f"Target database host: {make_url(settings.database_url).host}")
    done = await reseed()
    print_summary(done["rows"])
    phases = ", ".join(f"{name} {seconds:.2f}s" for name, seconds in done["phases"].items())
    print(f"Seeded in {done['seconds']:.1f}s ({phases})")
    print_demo_serials(done["rows"], done["today"])


if __name__ == "__main__":
    asyncio.run(main())