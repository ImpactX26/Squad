"""dispatch MCP server (ARCHITECTURE.md §5.6) — :8106.

Technician visits: find the technician, book the job for a date, move it through its statuses.
No maps and no distance (§7.7): a technician covers a city (staff_users.city), matched to the
customer's address city in code with aliases, and the one with the least work gets the job.

A visit is booked for a date only, never a time slot: the technician phones the customer to agree
the time. create_job's capacity check and insert run under a per-technician, per-date lock, so two
bookings at once can't both take a technician's last place that day.

create_job, update_job_status and reject_job are in router.MODEL_FORBIDDEN_TOOLS: only the workflows
and the job API call them, in code (§4.2).
"""

import datetime as dt
import logging
import re
import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.core.config import get_settings
from mcp_servers import run
from mcp_servers.common import db, events

log = logging.getLogger(__name__)
mcp = MCPServer(name="dispatch", instructions=__doc__)

# The cities the team covers, and what customers also call them (§7.7). Matched as whole words.
CITY_ALIASES: dict[str, str] = {
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "bengalooru": "Bengaluru",
    "mumbai": "Mumbai", "bombay": "Mumbai",
    "delhi": "Delhi", "new delhi": "Delhi", "noida": "Delhi", "gurugram": "Delhi", "gurgaon": "Delhi",
}
STATUS_ORDER = ("assigned", "accepted", "en_route", "on_site", "completed")
OPEN_STATUSES = ("assigned", "accepted", "en_route", "on_site")


def canonical_city(name: str | None) -> str | None:
    """"bangalore", "Bengaluru, Karnataka", "New Delhi" -> "Bengaluru", "Bengaluru", "Delhi".

    A city with no alias is compared as itself (lower case, letters only), so a new city still
    matches a technician who covers exactly that name.
    """
    words = " ".join(re.sub(r"[^a-z]+", " ", (name or "").lower()).split())
    if not words:
        return None
    if words in CITY_ALIASES:
        return CITY_ALIASES[words]
    for alias in sorted(CITY_ALIASES, key=len, reverse=True):  # "new delhi" before "delhi"
        if re.search(rf"\b{alias}\b", words):
            return CITY_ALIASES[alias]
    return words


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def _refused(error: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message, **extra}


_JOB = """
SELECT j.id AS job_id, j.ticket_id, t.ticket_number, j.technician_id, u.name AS technician_name,
       j.address_id, j.service_code, j.part_id, p.sku AS part_sku, j.warehouse_id, j.scheduled_date,
       j.status, j.notes, j.completed_at, j.created_at
FROM service_jobs j
JOIN tickets t ON t.id = j.ticket_id
JOIN staff_users u ON u.id = j.technician_id
LEFT JOIN parts p ON p.id = j.part_id
"""


# ---------- §5.6 tools ----------


@mcp.tool()
async def find_technician(city: str, skill: str, date: str,
                          exclude_technician_ids: list[str] | None = None) -> dict[str, Any]:
    """The technician for a visit: the skill, available, room that date, in the customer's city, least work.

    Room = fewer than MAX_JOBS_PER_TECH_PER_DAY non-cancelled jobs that date. Least work = fewest
    open jobs, then fewest jobs that date, then by name. No distance (§7.7). exclude_technician_ids
    leaves those out (a job's rejecters, so nobody is offered the same job twice).
    """
    excluded = {str(t) for t in exclude_technician_ids or []}
    day = _date(date)
    if day is None:
        return {"found": False, "error": "invalid_date", "message": "date must be YYYY-MM-DD"}
    wanted = canonical_city(city)
    limit = get_settings().max_jobs_per_tech_per_day
    rows = await db.fetch(
        "SELECT u.id AS technician_id, u.name, u.email, u.phone, u.city, u.skills,"
        " (SELECT count(*) FROM service_jobs j WHERE j.technician_id = u.id"
        "   AND j.status = ANY($3::text[])) AS open_jobs,"
        " (SELECT count(*) FROM service_jobs j WHERE j.technician_id = u.id"
        "   AND j.scheduled_date = $2 AND j.status <> 'cancelled') AS jobs_that_day"
        " FROM staff_users u WHERE u.role = 'technician' AND u.is_available AND $1 = ANY(u.skills)",
        skill, day, list(OPEN_STATUSES))
    in_city = [r for r in db.rows_to_list(rows) if wanted and canonical_city(r["city"]) == wanted
               and str(r["technician_id"]) not in excluded]
    with_room = sorted((r for r in in_city if r["jobs_that_day"] < limit),
                       key=lambda r: (r["open_jobs"], r["jobs_that_day"], r["name"]))
    base = {"city": wanted, "skill": skill, "date": day.isoformat(), "max_jobs_per_day": limit,
            "candidates": [{k: r[k] for k in ("name", "open_jobs", "jobs_that_day")} for r in in_city]}
    if not with_room:
        reason = (f"every {skill} technician in {wanted} is full on {day.isoformat()}" if in_city
                  else f"no available {skill} technician in {city.strip() or 'that city'}")
        return {"found": False, **base, "reason": reason}
    best = with_room[0]
    best.pop("skills", None)
    return {"found": True, **base, "technician": best}


@mcp.tool()
async def create_job(ticket_id: str, technician_id: str, address_id: str, service_code: str,
                     part_id: str | None, date: str) -> dict[str, Any]:
    """Book a technician visit for a date (no time slot; §7.7) + job.assigned + a timeline event.

    Refused when the ticket already has an open job, the technician lacks the skill or is full that
    date, the address isn't the customer's, or a part-based service has no part reserved for this
    ticket (a job never goes out without its part). The capacity check and the insert run under a
    lock per technician and date.
    """
    tid, uid_, aid = _uuid(ticket_id), _uuid(technician_id), _uuid(address_id)
    pid = _uuid(part_id) if part_id else None
    day = _date(date)
    if not (tid and uid_ and aid) or (part_id and pid is None):
        return _refused("invalid_id", "ticket_id, technician_id, address_id (and part_id) must be UUIDs")
    if day is None:
        return _refused("invalid_date", "date must be YYYY-MM-DD")
    limit = get_settings().max_jobs_per_tech_per_day
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('dispatch:' || $1 || ':' || $2))",
                               uid_, day.isoformat())
            tech = await conn.fetchrow(
                "SELECT id, name, role, skills, is_available FROM staff_users WHERE id = $1", uid_)
            if tech is None or tech["role"] != "technician":
                return _refused("not_a_technician", "no such technician")
            if not tech["is_available"]:
                return _refused("technician_unavailable", f"{tech['name']} is not available")
            ticket = await conn.fetchrow(
                "SELECT id, ticket_number, customer_id, status FROM tickets WHERE id = $1 FOR UPDATE", tid)
            if ticket is None:
                return _refused("ticket_not_found", "no such ticket")
            if ticket["status"] in ("resolved", "closed"):
                return _refused("ticket_closed", f"the ticket is {ticket['status']}")
            existing = await conn.fetchrow(
                "SELECT id, status FROM service_jobs WHERE ticket_id = $1 AND status = ANY($2::text[]) LIMIT 1",
                tid, list(OPEN_STATUSES))
            if existing is not None:
                return _refused("job_exists", "the ticket already has an open job",
                                job_id=str(existing["id"]), status=existing["status"])
            if not await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM addresses WHERE id = $1 AND customer_id = $2)",
                    aid, ticket["customer_id"]):
                return _refused("address_not_found", "no such address for the ticket's customer")
            service = await conn.fetchrow(
                "SELECT code, name, part_type, requires_visit, required_skill FROM service_catalog"
                " WHERE code = upper($1)", service_code.strip())
            if service is None:
                return _refused("unknown_service", f"no service {service_code!r}")
            if not service["requires_visit"]:
                return _refused("no_visit", f"{service['code']} is shipped, not a visit")
            if service["required_skill"] and service["required_skill"] not in tech["skills"]:
                return _refused("missing_skill", f"{tech['name']} doesn't do {service['required_skill']}")

            warehouse_id = None
            if service["part_type"]:
                if pid is None:
                    return _refused("part_required", f"{service['code']} needs a {service['part_type']} part")
                held = await conn.fetchrow(
                    "SELECT m.warehouse_id, SUM(m.change) AS held FROM inventory_movements m"
                    " JOIN parts p ON p.id = m.part_id"
                    " WHERE m.ticket_id = $1 AND m.part_id = $2 AND p.part_type = $3"
                    "   AND m.kind IN ('reserve', 'release', 'consume')"
                    " GROUP BY m.warehouse_id HAVING SUM(m.change) > 0 ORDER BY held DESC LIMIT 1",
                    tid, pid, service["part_type"])
                if held is None:
                    return _refused("no_reservation", "reserve the part for this ticket first")
                warehouse_id = held["warehouse_id"]
            elif pid is not None:
                return _refused("no_part_needed", f"{service['code']} uses no part")

            booked = await conn.fetchval(
                "SELECT count(*) FROM service_jobs WHERE technician_id = $1 AND scheduled_date = $2"
                " AND status <> 'cancelled'", uid_, day)
            if booked >= limit:
                return _refused("technician_full", f"{tech['name']} already has {booked} jobs on {day.isoformat()}")

            job_id = await conn.fetchval(
                "INSERT INTO service_jobs (ticket_id, technician_id, address_id, service_code, part_id,"
                " warehouse_id, scheduled_date) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
                tid, uid_, aid, service["code"], pid, warehouse_id, day)
            job = await conn.fetchrow(_JOB + " WHERE j.id = $1", job_id)
            await conn.execute(
                "INSERT INTO ticket_events (ticket_id, type, payload, actor) VALUES ($1, 'job_assigned', $2, 'system')",
                tid, {"job_id": str(job_id), "technician_id": uid_, "technician_name": tech["name"],
                      "scheduled_date": day.isoformat(), "service_code": service["code"], "part_sku": job["part_sku"]})
    out = {"ok": True, **db.row_to_dict(job)}
    await events.publish("job.assigned", {
        "job_id": out["job_id"], "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"],
        "technician_id": out["technician_id"], "technician_name": out["technician_name"],
        "scheduled_date": out["scheduled_date"], "service_code": out["service_code"]})
    return out


@mcp.tool()
async def update_job_status(job_id: str, status: str, notes: str | None = None) -> dict[str, Any]:
    """Move a job forward: assigned -> accepted -> en_route -> on_site -> completed (§7.7).

    Forward only; cancelled from any state except completed. Writes a timeline event and emits
    job.status_changed, and job.completed when it is completed.
    """
    jid = _uuid(job_id)
    if jid is None:
        return _refused("invalid_id", "job_id must be a UUID")
    if status not in (*STATUS_ORDER, "cancelled") or status == "assigned":
        return _refused("invalid_status", "status must be accepted, en_route, on_site, completed or cancelled")
    notes = " ".join((notes or "").split())[:1000] or None
    async with db.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(_JOB + " WHERE j.id = $1 FOR UPDATE OF j", jid)
            if job is None:
                return _refused("not_found", "no such job")
            current = job["status"]
            if current in ("completed", "cancelled"):
                return _refused("job_closed", f"the job is already {current}", status=current)
            if status != "cancelled" and STATUS_ORDER.index(status) <= STATUS_ORDER.index(current):
                return _refused("not_forward", f"a job that is {current} can't go back to {status}", status=current)
            await conn.execute(
                "UPDATE service_jobs SET status = $2, notes = COALESCE($3, notes),"
                " completed_at = CASE WHEN $2 = 'completed' THEN now() ELSE completed_at END WHERE id = $1",
                jid, status, notes)
            # A technician moves their own job; only an admin cancels (app/api/jobs.py).
            actor = "system" if status == "cancelled" else str(job["technician_id"])
            await conn.execute(
                "INSERT INTO ticket_events (ticket_id, type, payload, actor) VALUES ($1, 'job_status_changed', $2, $3)",
                job["ticket_id"], {"job_id": jid, "from": current, "status": status, "notes": notes,
                                   "technician_name": job["technician_name"]}, actor)
            await conn.execute("UPDATE tickets SET updated_at = now() WHERE id = $1", job["ticket_id"])
            updated = await conn.fetchrow(_JOB + " WHERE j.id = $1", jid)
    out = {"ok": True, "previous": current, **db.row_to_dict(updated)}
    event = {"job_id": jid, "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"],
             "technician_id": out["technician_id"], "status": status, "previous": current, "notes": notes}
    await events.publish("job.status_changed", event)
    if status == "completed":
        await events.publish("job.completed", event)
    return out


REJECT_REASON = (3, 300)


@mcp.tool()
async def reject_job(job_id: str, technician_id: str, reason: str) -> dict[str, Any]:
    """The assigned technician can't take this job (§7.7): it closes as cancelled, with their reason.

    Only a job still "assigned", and only its own technician (job_closed / not_assigned / not_yours).
    The part stays reserved for the ticket. Emits job.rejected, never job.status_changed: the
    cancellation workflow (which releases the part) must not run; the rejection workflow reassigns it.
    """
    jid, tid = _uuid(job_id), _uuid(technician_id)
    if jid is None or tid is None:
        return _refused("invalid_id", "job_id and technician_id must be UUIDs")
    text = " ".join((reason or "").split())
    if not REJECT_REASON[0] <= len(text) <= REJECT_REASON[1]:
        return _refused("reason_required", "say why, in 3 to 300 characters")
    async with db.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(_JOB + " WHERE j.id = $1 FOR UPDATE OF j", jid)
            if job is None:
                return _refused("not_found", "no such job")
            if str(job["technician_id"]) != tid:
                return _refused("not_yours", "only the assigned technician can reject this job")
            if job["status"] in ("completed", "cancelled"):
                return _refused("job_closed", f"the job is already {job['status']}", status=job["status"])
            if job["status"] != "assigned":
                return _refused("not_assigned", f"the job is {job['status']}; only an assigned job can be rejected",
                                status=job["status"])
            notes = f"Rejected by {job['technician_name']}: {text}"
            await conn.execute("UPDATE service_jobs SET status = 'cancelled', notes = $2 WHERE id = $1", jid, notes)
            await conn.execute(
                "INSERT INTO ticket_events (ticket_id, type, payload, actor) VALUES ($1, 'job_rejected', $2, $3)",
                job["ticket_id"], {"job_id": jid, "technician_id": tid, "technician_name": job["technician_name"],
                                   "reason": text, "scheduled_date": job["scheduled_date"].isoformat(),
                                   "note": notes}, tid)
            ticket = await conn.fetchrow(
                "UPDATE tickets SET updated_at = now() WHERE id = $1 RETURNING ticket_number, status, priority",
                job["ticket_id"])
            rejected = await conn.fetchrow(_JOB + " WHERE j.id = $1", jid)
    out = {"ok": True, "previous": "assigned", **db.row_to_dict(rejected)}
    await events.publish("job.rejected", {
        "job_id": jid, "ticket_id": out["ticket_id"], "ticket_number": out["ticket_number"], "technician_id": tid,
        "technician_name": out["technician_name"], "reason": text, "scheduled_date": out["scheduled_date"]})
    await events.publish("ticket.updated", {
        "ticket_id": out["ticket_id"], "ticket_number": ticket["ticket_number"], "status": ticket["status"],
        "priority": ticket["priority"], "note": notes})
    return out


@mcp.tool()
async def list_technicians(city: str | None = None, skill: str | None = None) -> dict[str, Any]:
    """Every technician, or those in a city / with a skill: city, skills, availability, open jobs. Read-only."""
    wanted = canonical_city(city) if city and city.strip() else None
    rows = await db.fetch(
        "SELECT u.id AS technician_id, u.name, u.city, u.skills, u.is_available,"
        " (SELECT count(*) FROM service_jobs j WHERE j.technician_id = u.id"
        "   AND j.status = ANY($2::text[])) AS open_jobs"
        " FROM staff_users u WHERE u.role = 'technician' AND ($1::text IS NULL OR $1 = ANY(u.skills))"
        " ORDER BY u.city, u.name", skill.strip() if skill and skill.strip() else None, list(OPEN_STATUSES))
    techs = [r for r in db.rows_to_list(rows) if wanted is None or canonical_city(r["city"]) == wanted]
    return {"city": wanted, "skill": skill, "count": len(techs), "technicians": techs}


@mcp.tool()
async def get_jobs(technician_id: str) -> dict[str, Any]:
    """A technician's jobs: open ones first, then by date."""
    tid = _uuid(technician_id)
    if tid is None:
        return {"jobs": [], "error": "invalid_id"}
    rows = await db.fetch(
        _JOB + " WHERE j.technician_id = $1"
        " ORDER BY (j.status IN ('completed', 'cancelled')), j.scheduled_date, j.created_at", tid)
    return {"technician_id": tid, "jobs": db.rows_to_list(rows)}


if __name__ == "__main__":
    run(mcp, "dispatch")