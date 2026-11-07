"""Зависимости API: БД, текущий пользователь, проверка прав, контекст агента."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from bis.agents.base import AgentContext
from bis.db import get_db
from bis.llm.gateway import get_gateway
from bis.models import User
from bis.rag.embeddings import get_embedder
from bis.security import decode_access_token, has_scope
from bis.services.storage import get_storage

bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")
    try:
        payload = decode_access_token(creds.credentials)
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Недействительный токен") from e
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Пользователь не найден или заблокирован")
    return user


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
