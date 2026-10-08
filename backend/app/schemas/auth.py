import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

StaffRole = Literal["agent", "technician", "warehouse", "admin"]


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class StaffOut(BaseModel):
    """A signed-in staff member, as /api/me and the login response return them."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    role: StaffRole
    city: str | None
    skills: list[str]
    is_available: bool
    avatar_url: str | None


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    staff: StaffOut
