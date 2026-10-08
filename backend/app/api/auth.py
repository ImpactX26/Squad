from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import CurrentUser, create_access_token, verify_dummy_password, verify_password
from app.models import StaffUser
from app.schemas.auth import LoginRequest, LoginResponse, StaffUserOut

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, session: Annotated[AsyncSession, Depends(get_session)]) -> LoginResponse:
    user = await session.scalar(select(StaffUser).where(func.lower(StaffUser.email) == body.email.lower()))
    if user is None:
        verify_dummy_password(body.password)
        ok = False
    else:
        ok = verify_password(body.password, user.password_hash)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    return LoginResponse(
        access_token=create_access_token(user.id, user.role),
        user=StaffUserOut.model_validate(user),
    )


@router.get("/me", response_model=StaffUserOut)
async def me(user: CurrentUser) -> StaffUserOut:
    return StaffUserOut.model_validate(user)