import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

StaffRole = Literal["agent", "technician", "warehouse", "admin"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class StaffUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    role: StaffRole
    avatar_url: str | None


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: StaffUserOut