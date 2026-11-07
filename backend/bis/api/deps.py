"""Зависимости API: БД, текущий пользователь, проверка прав, контекст агента."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.agents.base import AgentContext
from bis.db import get_db
from bis.llm.gateway import get_gateway
from bis.models import User
from bis.rag.embeddings import get_embedder
from bis.security import decode_access_token, has_scope
from bis.services.storage import get_storage

bearer = HTTPBearer(auto_error=False)


def auto_login_user(db: Session) -> User | None:
    """Вход без пароля на время разработки (BIS_AUTO_LOGIN): всегда последний созданный администратор."""
    from bis.config import get_settings

    if not get_settings().auto_login:
        return None
    return db.scalar(select(User).where(User.is_active.is_(True), User.role == "admin").order_by(User.created_at.desc()).limit(1))


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    user = None
    if creds is not None:
        try:
            user = db.get(User, uuid.UUID(decode_access_token(creds.credentials)["sub"]))
        except (jwt.PyJWTError, KeyError, ValueError):
            user = None
    if user is not None and user.is_active:
        return user
    # старый или чужой токен в браузере не должен возвращать форму входа, пока вход отключён
    if auto := auto_login_user(db):
        return auto
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Недействительный токен или пользователь заблокирован")


def require(scope: str) -> Callable[..., User]:
    def dep(user: User = Depends(current_user)) -> User:
        if not has_scope(user.role, scope):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Недостаточно прав: {scope}")
        return user

    return dep


def agent_context(db: Session, user: User) -> AgentContext:
    from bis.plugins.sdk import get_registry

    reg = get_registry()
    return AgentContext(
        db=db, org_id=user.organization_id, user=user, gateway=get_gateway(), embedder=get_embedder(),
        storage=get_storage(), agents=reg.agents, tool_registry=reg.tools,
    )
