"""POST /api/auth/login and GET /api/me: JWT auth for the seeded staff (ARCHITECTURE.md §10)."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings
from app.core.config import Settings
from app.core.db import get_session
from app.core.security import (
    AuthNotConfigured,
    burn_password_check,
    create_access_token,
    staff_id_from_token,
    verify_password,
)
from app.models import StaffUser
from app.schemas.auth import LoginIn, StaffOut, TokenOut

router = APIRouter()
bearer = HTTPBearer(auto_error=False)

WRONG_LOGIN = "Wrong email or password."
SIGN_IN_AGAIN = "Sign in again."
NOT_CONFIGURED = "Sign-in isn't set up: JWT_SECRET is empty in backend/.env."


def unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})


async def staff_for_token(session: AsyncSession, token: str, settings: Settings) -> StaffUser | None:
    """The staff member a valid token belongs to; None when the token or the user is gone.

    Raises AuthNotConfigured when JWT_SECRET is empty.
    """
    staff_id: uuid.UUID | None = staff_id_from_token(token, settings)
    if staff_id is None:
        return None
    return await session.get(StaffUser, staff_id)


async def get_current_staff(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> StaffUser:
    """Dependency for staff-only routes: the signed-in staff member, or 401."""
    if credentials is None:
        raise unauthorized(SIGN_IN_AGAIN)
    try:
        staff = await staff_for_token(session, credentials.credentials, settings)
    except AuthNotConfigured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED) from None
    if staff is None:
        raise unauthorized(SIGN_IN_AGAIN)
    return staff


@router.post(
    "/api/auth/login",
    response_model=TokenOut,
    responses={401: {"description": WRONG_LOGIN}, 503: {"description": "JWT_SECRET is not set"}},
)
async def login(
    body: LoginIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> TokenOut:
    if not settings.jwt_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED)
    email = body.email.strip().lower()
    staff = await session.scalar(select(StaffUser).where(func.lower(StaffUser.email) == email))
    # bcrypt is slow on purpose: run it off the event loop. An unknown email costs the same time.
    if staff is None:
        await asyncio.to_thread(burn_password_check, body.password)
        raise unauthorized(WRONG_LOGIN)
    if not await asyncio.to_thread(verify_password, body.password, staff.password_hash):
        raise unauthorized(WRONG_LOGIN)
    token, expires_at = create_access_token(staff.id, staff.role, settings)
    return TokenOut(access_token=token, expires_at=expires_at, staff=StaffOut.model_validate(staff))


@router.get("/api/me", response_model=StaffOut, responses={401: {"description": SIGN_IN_AGAIN}})
async def me(staff: StaffUser = Depends(get_current_staff)) -> StaffOut:
    return StaffOut.model_validate(staff)
