from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bis.api.deps import current_user
from bis.api.schemas import UserOut
from bis.db import get_db
from bis.models import User
from bis.security import ROLE_SCOPES, create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(func.lower(User.email) == body.email.strip().lower()))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Неверный email или пароль")
    return TokenOut(access_token=create_access_token(user.id, user.organization_id, user.role),
                    user=UserOut.model_validate(user))


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"user": UserOut.model_validate(user), "scopes": sorted(ROLE_SCOPES.get(user.role, []))}
