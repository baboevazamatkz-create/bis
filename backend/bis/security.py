"""Пароли, JWT, роли и права (RBAC)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from bis.config import get_settings

# Роли и разрешённые области действий (scopes). Инструменты агентов объявляют требуемые scopes.
ROLE_SCOPES: dict[str, set[str]] = {
    "admin": {"*"},
    "manager": {"knowledge:read", "registry:read", "documents:read", "documents:write", "documents:approve",
                "users:read", "audit:read"},
    "pto": {"knowledge:read", "knowledge:write", "registry:read", "registry:write", "documents:read", "documents:write"},
    "foreman": {"knowledge:read", "registry:read", "documents:read", "documents:write"},
    "supply": {"knowledge:read", "registry:read", "documents:read", "documents:write"},
    "lawyer": {"knowledge:read", "knowledge:write", "registry:read", "registry:write", "documents:read", "documents:write"},
    "secretary": {"knowledge:read", "registry:read", "documents:read", "documents:write"},
    "viewer": {"knowledge:read", "registry:read", "documents:read"},
}
ROLES = tuple(ROLE_SCOPES)


def has_scope(role: str, scope: str) -> bool:
    scopes = ROLE_SCOPES.get(role, set())
    if "*" in scopes or scope in scopes:
        return True
    area = scope.split(":", 1)[0]
    return f"{area}:*" in scopes


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "org": str(org_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=s.access_token_ttl_minutes),
    }
    return jwt.encode(payload, s.secret_key, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
