"""Technician job endpoints (ARCHITECTURE.md §10, §7.7): the technician portal's API.

Reads come straight from the database. The writes, a status change and a technician's rejection, go
through dispatch.update_job_status and dispatch.reject_job (tools no model is ever offered,
router.MODEL_FORBIDDEN_TOOLS); the workflows react to their job.status_changed / job.completed /
job.rejected events (customer messages, the part consumed or released, the ticket resolved, the job
reassigned).

Who may do what, checked here in code:
- a technician sees and moves only their own jobs (403 for anyone else's);
- agents and admins may read any job; an admin may cancel one, and nothing else;
- only the assigned technician may reject their job, and only while it is "assigned".
"""

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain.mcp_hub import McpToolError, hub
from app.core.db import get_session
from app.core.security import CurrentUser
from app.payments.invoice import address_text, maps_links
from app.payments.money import IST
from app.schemas.jobs import JobListResponse, JobOut, JobPatch, JobRejected, JobReject

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["jobs"])

_JOBS = """
SELECT j.id, j.ticket_id, j.status, j.scheduled_date, j.service_code, j.notes, j.completed_at, j.created_at,
       j.technician_id, u.name AS technician_name, u.phone AS technician_phone,
       t.ticket_number, t.title AS issue, sc.name AS service_name,
       c.full_name, c.phone, c.email,
       a.line1, a.line2, a.city, a.state, a.postal_code, a.location_url,
       pr.serial_number, pr.warranty_until, pm.name AS model_name, pm.model_number, pm.category,
       p.id AS part_id, p.sku AS part_sku, p.name AS part_name, w.name AS warehouse_name,
       pay.invoice_number
FROM service_jobs j
JOIN tickets t               ON t.id = j.ticket_id
JOIN customers c             ON c.id = t.customer_id
JOIN addresses a             ON a.id = j.address_id
JOIN staff_users u           ON u.id = j.technician_id
LEFT JOIN service_catalog sc ON sc.code = j.service_code
LEFT JOIN products pr        ON pr.id = t.product_id
LEFT JOIN product_models pm  ON pm.id = pr.model_id
LEFT JOIN parts p            ON p.id = j.part_id
LEFT JOIN warehouses w       ON w.id = j.warehouse_id
LEFT JOIN LATERAL (
    SELECT invoice_number FROM payments
    WHERE ticket_id = j.ticket_id AND service_code = j.service_code AND status = 'paid'
    ORDER BY paid_at DESC LIMIT 1) pay ON TRUE
"""

_TRIED = text("""
SELECT ticket_id, step, result, notes FROM diagnostic_steps
WHERE ticket_id = ANY(CAST(:tickets AS uuid[])) AND result <> 'pending' ORDER BY ticket_id, position
""")

# dispatch.update_job_status's refusals, as HTTP.
_REFUSALS: dict[str, tuple[int, str]] = {
    "not_found": (status.HTTP_404_NOT_FOUND, "No such job."),
    "invalid_id": (status.HTTP_404_NOT_FOUND, "No such job."),
    "job_closed": (status.HTTP_409_CONFLICT, "This job is already closed."),
    "not_forward": (status.HTTP_409_CONFLICT, "A job only moves forward."),
    "invalid_status": (status.HTTP_422_UNPROCESSABLE_CONTENT, "That isn't a job status."),
}


async def _jobs(session: AsyncSession, where: str, params: dict[str, Any]) -> list[JobOut]:
    rows = (await session.execute(text(_JOBS + where), params)).mappings().all()
    if not rows:
        return []
    tried: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for step in (await session.execute(_TRIED, {"tickets": [str(r["ticket_id"]) for r in rows]})).mappings():
        tried[str(step["ticket_id"])].append({"step": step["step"], "result": step["result"], "notes": step["notes"]})
    return [job_out(dict(row), tried[str(row["ticket_id"])]) for row in rows]


def job_out(row: dict[str, Any], tried: list[dict[str, Any]]) -> JobOut:
    address = {k: row[k] for k in ("line1", "line2", "city", "state", "postal_code", "location_url")}
    return JobOut(
        id=row["id"], ticket_id=row["ticket_id"], ticket_number=row["ticket_number"], status=row["status"],
        scheduled_date=row["scheduled_date"], service_code=row["service_code"], service_name=row["service_name"],
        issue=row["issue"], notes=row["notes"], completed_at=row["completed_at"], created_at=row["created_at"],
        technician_id=row["technician_id"], technician_name=row["technician_name"],
        technician_phone=row["technician_phone"],
        customer={"full_name": row["full_name"], "phone": row["phone"], "email": row["email"]},
        address={**address, "text": address_text(address), "maps_links": maps_links(address)},
        device={"model_name": row["model_name"], "model_number": row["model_number"],
                "serial_number": row["serial_number"], "category": row["category"],
                "warranty_until": row["warranty_until"]} if row["serial_number"] else None,
        part={"id": row["part_id"], "sku": row["part_sku"], "name": row["part_name"],
              "warehouse_name": row["warehouse_name"]} if row["part_id"] else None,
        tried=tried,
        billing="paid" if row["invoice_number"] else "warranty",
        invoice_number=row["invoice_number"],
    )


async def load_job(session: AsyncSession, job_id: uuid.UUID) -> JobOut | None:
    jobs = await _jobs(session, " WHERE j.id = :id", {"id": job_id})
    return jobs[0] if jobs else None


async def ticket_job(session: AsyncSession, ticket_id: uuid.UUID) -> JobOut | None:
    """The ticket's latest job, for GET /api/tickets/{id}."""
    jobs = await _jobs(session, " WHERE j.ticket_id = :id ORDER BY j.created_at DESC LIMIT 1", {"id": ticket_id})
    return jobs[0] if jobs else None


def _may_read(user: Any, job: JobOut) -> bool:
    return user.role in ("agent", "admin") or (user.role == "technician" and job.technician_id == user.id)


@router.get("/jobs/mine", response_model=JobListResponse)
async def my_jobs(user: CurrentUser, session: Annotated[AsyncSession, Depends(get_session)]) -> JobListResponse:
    """The signed-in technician's jobs: today and upcoming, plus any still open from an earlier day.
    A job they rejected leaves their list (it is someone else's now)."""
    today = datetime.now(UTC).astimezone(IST).date()
    jobs = await _jobs(session, (
        " WHERE j.technician_id = :me AND (j.scheduled_date >= :today"
        "   OR j.status NOT IN ('completed', 'cancelled'))"
        " AND NOT EXISTS (SELECT 1 FROM ticket_events e WHERE e.ticket_id = j.ticket_id AND e.type = 'job_rejected'"
        "                 AND e.payload->>'job_id' = CAST(j.id AS text))"
        " ORDER BY j.scheduled_date, j.created_at"), {"me": user.id, "today": today})
    return JobListResponse(jobs=jobs)


@router.get("/jobs/{job_id}", response_model=JobOut, responses={403: {"description": "Not your job"},
                                                                 404: {"description": "No such job"}})
async def get_job(job_id: uuid.UUID, user: CurrentUser,
                  session: Annotated[AsyncSession, Depends(get_session)]) -> JobOut:
    """One job: for its own technician, agents, or admins."""
    job = await load_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    if not _may_read(user, job):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This isn't your job.")
    return job


@router.patch("/jobs/{job_id}", response_model=JobOut, responses={
    403: {"description": "Not your job, or an admin doing anything but cancel"}, 404: {"description": "No such job"},
    409: {"description": "Not a forward move, or the job is closed"}, 503: {"description": "Dispatch unavailable"}})
async def patch_job(job_id: uuid.UUID, body: JobPatch, user: CurrentUser,
                    session: Annotated[AsyncSession, Depends(get_session)]) -> JobOut:
    """A technician moves their own job forward (with a note); an admin may cancel one (§7.7)."""
    job = await load_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    if user.role == "technician":
        if job.technician_id != user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This isn't your job.")
        if body.status == "cancelled":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only an admin can cancel a job.")
    elif user.role == "admin":
        if body.status != "cancelled":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="An admin can cancel a job; its technician moves it forward.")
    else:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the job's technician or an admin.")

    notes = body.notes
    if body.status == "cancelled" and user.role == "admin":
        notes = f"Cancelled by {user.name}" + (f": {body.notes}" if body.notes else "")
    # The login check left a transaction open on this session; don't hold it across the tool call.
    await session.rollback()
    try:
        result = await hub.call_tool("dispatch__update_job_status",
                                     {"job_id": str(job_id), "status": body.status, "notes": notes})
    except McpToolError as e:
        log.error("update_job_status unavailable: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Dispatch is unavailable right now. Try again in a minute.") from e
    if not isinstance(result, dict):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Dispatch gave an unexpected answer.")
    if not result.get("ok"):
        code, detail = _REFUSALS.get(str(result.get("error")), (
            status.HTTP_409_CONFLICT, str(result.get("message") or "That status change isn't allowed.")))
        raise HTTPException(status_code=code, detail=result.get("message") or detail)
    updated = await load_job(session, job_id)
    if updated is None:  # deleted between the write and the read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    return updated


# dispatch.reject_job's refusals, as HTTP.
_REJECT_REFUSALS: dict[str, tuple[int, str]] = {
    "not_found": (status.HTTP_404_NOT_FOUND, "No such job."),
    "invalid_id": (status.HTTP_404_NOT_FOUND, "No such job."),
    "not_yours": (status.HTTP_403_FORBIDDEN, "This isn't your job."),
    "not_assigned": (status.HTTP_409_CONFLICT, "You've already accepted this job; ask an admin to cancel it."),
    "job_closed": (status.HTTP_409_CONFLICT, "This job is already closed."),
    "reason_required": (status.HTTP_422_UNPROCESSABLE_CONTENT, "Say why, in 3 to 300 characters."),
}


@router.post("/jobs/{job_id}/reject", response_model=JobRejected, responses={
    403: {"description": "Not the job's technician"}, 404: {"description": "No such job"},
    409: {"description": "The job isn't assigned any more"}, 422: {"description": "No reason, or too short"},
    503: {"description": "Dispatch unavailable"}})
async def reject_job(job_id: uuid.UUID, body: JobReject, user: CurrentUser,
                     session: Annotated[AsyncSession, Depends(get_session)]) -> JobRejected:
    """The assigned technician can't take this job (§7.7): dispatch.reject_job closes it with their reason,
    and the job.rejected workflow finds another technician in the background, the part still reserved."""
    job = await load_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    if user.role != "technician" or job.technician_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the job's own technician can reject it.")
    technician_id = str(user.id)
    await session.rollback()  # don't hold the login check's transaction across the tool call
    try:
        result = await hub.call_tool("dispatch__reject_job",
                                     {"job_id": str(job_id), "technician_id": technician_id, "reason": body.reason})
    except McpToolError as e:
        log.error("reject_job unavailable: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Dispatch is unavailable right now. Try again in a minute.") from e
    if not isinstance(result, dict):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Dispatch gave an unexpected answer.")
    if not result.get("ok"):
        code, detail = _REJECT_REFUSALS.get(str(result.get("error")), (
            status.HTTP_409_CONFLICT, str(result.get("message") or "This job can't be rejected.")))
        raise HTTPException(status_code=code, detail=detail)
    return JobRejected(ok=True, status="rejected")