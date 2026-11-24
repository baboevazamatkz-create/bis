"""Зависимости API: БД, текущий пользователь, проверка прав, контекст агента."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.agents.base import AgentContext
from bis.config import PILOT_BIND
from bis.db import get_db
from bis.llm.gateway import get_gateway
from bis.models import User
from bis.rag.embeddings import get_embedder
from bis.security import decode_access_token, has_scope
from bis.services.storage import get_storage

bearer = HTTPBearer(auto_error=False)


def auto_login_user(db: Session, request: Request) -> User | None:
    """В личном пилоте открываем сессию последнего активного администратора."""
    if not getattr(request.app.state, "local_auto_login", False):
        return None
    if request.client is None or request.client.host != PILOT_BIND:
        return None
    server = request.scope.get("server")
    if not server or server[0] != PILOT_BIND:
        return None
    # Loopback alone does not protect against browser requests from another site or a reverse proxy.
    host = request.headers.get("host", "").lower()
    port = f":{server[1]}" if server[1] != 80 else ""
    if host not in {f"localhost{port}", f"{PILOT_BIND}{port}"}:
        return None
    if request.headers.get("origin") not in (None, f"http://{host}"):
        return None
    if any(name == "forwarded" or name.startswith("x-forwarded-") for name in request.headers):
        return None
    return db.scalar(select(User).where(User.is_active.is_(True), User.role == "admin").order_by(User.created_at.desc()).limit(1))


def current_user(
    request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    user = None
    if creds is not None:
        try:
            user = db.get(User, uuid.UUID(decode_access_token(creds.credentials)["sub"]))
        except (jwt.PyJWTError, KeyError, ValueError):
            user = None
    if user is not None and user.is_active:
        _source_context(db, user, request)
        return user
    # старый или чужой токен в браузере не должен возвращать форму входа, пока вход отключён
    if auto := auto_login_user(db, request):
        _source_context(db, auto, request)
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


def agent_context(db: Session, user: User, *, query_only: bool = False) -> AgentContext:
    from bis.plugins.sdk import get_registry

    reg = get_registry()
    from bis.services.library_access import bind_context

    scope = db.info.get("source_context") or bind_context(db, user.organization_id)
    from bis.llm.gateway import LLMGateway
    from bis.rag.embeddings import get_query_embedder

    ctx = AgentContext(
        db=db, org_id=user.organization_id, user=user,
        gateway=LLMGateway(entries={}) if query_only else get_gateway(),
        embedder=get_query_embedder() if query_only else get_embedder(),
        storage=get_storage(), agents=reg.agents, tool_registry=reg.tools,
    )
    if scope.object_id:
        ctx.focus["object"] = str(scope.object_id)
    return ctx


def _source_context(db, user, request):
    from bis.services.library_access import bind_context

    try:
        bind_context(db, user.organization_id, request.headers.get("X-BIS-Project"))
    except (ValueError, LookupError) as exc:
        raise HTTPException(400, "Некорректный или недоступный проект") from exc
