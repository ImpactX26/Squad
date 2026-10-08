"""Request and response models for the technician job endpoints (ARCHITECTURE.md §10, §7.7).

A job carries everything the technician portal shows (§7.7 step 3): the customer, the address as text
with its maps links, the device, the issue, the part to carry, what was already tried, and whether
the repair is paid or free under warranty.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

JobStatus = Literal["assigned", "accepted", "en_route", "on_site", "completed", "cancelled"]


class JobCustomer(BaseModel):
    full_name: str | None
    phone: str | None
    email: str | None


class MapsLink(BaseModel):
    label: str = Field(description='"Customer\'s map link", "Google Maps" or "Apple Maps"')
    url: str


class JobAddress(BaseModel):
    line1: str
    line2: str | None
    city: str
    state: str | None
    postal_code: str | None
    text: str = Field(description="The whole address on one line")
    location_url: str | None = Field(description="The maps link the customer pasted, if any (§7.6)")
    maps_links: list[MapsLink] = Field(
        description="The customer's own link if saved, otherwise Google Maps and Apple Maps searches for the text")


class JobDevice(BaseModel):
    model_name: str
    model_number: str
    serial_number: str
    category: str
    warranty_until: date | None


class JobPart(BaseModel):
    id: uuid.UUID
    sku: str
    name: str
    warehouse_name: str | None = Field(description="Where it is reserved")


class JobTried(BaseModel):
    step: str
    result: Literal["worked", "failed", "skipped"]
    notes: str | None


class JobOut(BaseModel):
    """One technician job, with what the portal shows (§7.7)."""

    id: uuid.UUID
    ticket_id: uuid.UUID
    ticket_number: str
    status: JobStatus
    scheduled_date: date = Field(description="A date only: the technician phones the customer to agree the time (§7.7)")
    service_code: str
    service_name: str | None
    issue: str = Field(description="The ticket's title")
    notes: str | None
    completed_at: datetime | None
    created_at: datetime
    technician_id: uuid.UUID
    technician_name: str | None
    technician_phone: str | None
    customer: JobCustomer
    address: JobAddress
    device: JobDevice | None
    part: JobPart | None
    tried: list[JobTried] = Field(description="Diagnostic steps already tried on the ticket, and how they went")
    billing: Literal["paid", "warranty"] = Field(description="Paid by the customer, or free under warranty")
    invoice_number: str | None = Field(description="The paid invoice, when billing is paid")


class JobListResponse(BaseModel):
    jobs: list[JobOut]


class JobPatch(BaseModel):
    status: Literal["accepted", "en_route", "on_site", "completed", "cancelled"]
    notes: str | None = Field(default=None, max_length=1000)


class JobReject(BaseModel):
    reason: str = Field(min_length=3, max_length=300, description="Why the technician can't take the job")


class JobRejected(BaseModel):
    ok: bool
    status: Literal["rejected"] = Field(description="The job is closed; another technician is being found")