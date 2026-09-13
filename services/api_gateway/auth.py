"""Authentication and authorization for the control plane.

API keys are the credential (the VS Code extension is a machine client, not a
browser session). Keys are shown once at creation and stored only as a salted
SHA-256 hash, so a database leak does not hand over working credentials.

Authorization is role-based, driven by ``configs/security.yaml`` — the same RBAC
table the tool layer consults, so an "engineer" cannot do through the API what
they could not do through an agent.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select

from configs.settings import get_settings
from packages.aiqa_types.models import new_id
from packages.security.guard import get_rbac
from services.observability.db import session_scope
from services.observability.models import ApiKeyRow, OrgRow, UserRow

KEY_PREFIX = "aiqa_"


def hash_key(raw_key: str) -> str:
    """Salted hash. The salt is the deployment secret, so hashes are not portable."""
    secret = get_settings().secret_key.encode("utf-8")
    return hmac.new(secret, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_key() -> str:
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


@dataclass
class Principal:
    """The authenticated caller."""

    user_id: str
    org_id: str
    role: str
    email: str = ""
    key_id: str = ""

    def can(self, permission: str) -> bool:
        return get_rbac().can(self.role, permission)

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{self.role}' lacks permission '{permission}'",
            )


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
def _extract_key(authorization: str | None, x_api_key: str | None) -> str:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "apikey"):
            return parts[1].strip()
        return authorization.strip()
    return ""


async def get_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    raw_key = _extract_key(authorization, x_api_key)
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key (send X-API-Key or Authorization: Bearer <key>)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    digest = hash_key(raw_key)
    with session_scope() as session:
        row = session.execute(select(ApiKeyRow).where(ApiKeyRow.key_hash == digest)).scalar_one_or_none()
        if row is None or not row.active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or revoked API key")
        if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key has expired")

        row.last_used_at = datetime.now(timezone.utc)
        user = session.get(UserRow, row.user_id)
        return Principal(
            user_id=row.user_id,
            org_id=row.org_id,
            role=row.role,
            email=user.email if user else "",
            key_id=row.id,
        )


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def requires(permission: str):
    """Route dependency enforcing one RBAC permission."""

    async def _dependency(principal: CurrentPrincipal) -> Principal:
        principal.require(permission)
        return principal

    return Depends(_dependency)


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
def bootstrap_admin() -> dict[str, str]:
    """Ensure a default org/user/API key exists so a fresh install is usable.

    Idempotent: if the configured bootstrap key already resolves, nothing changes.
    """
    settings = get_settings()
    raw_key = settings.bootstrap_api_key
    digest = hash_key(raw_key)

    with session_scope() as session:
        existing = session.execute(select(ApiKeyRow).where(ApiKeyRow.key_hash == digest)).scalar_one_or_none()
        if existing is not None:
            return {"org_id": existing.org_id, "user_id": existing.user_id, "api_key": raw_key, "created": "false"}

        org = session.execute(select(OrgRow).limit(1)).scalar_one_or_none()
        if org is None:
            org = OrgRow(
                id=new_id("org"),
                name="Default Organization",
                daily_cost_limit_usd=settings.daily_cost_limit_usd,
                monthly_cost_limit_usd=settings.monthly_cost_limit_usd,
            )
            session.add(org)

        user = session.execute(
            select(UserRow).where(UserRow.org_id == org.id, UserRow.role == "admin")
        ).scalar_one_or_none()
        if user is None:
            user = UserRow(
                id=new_id("usr"), org_id=org.id, email="admin@localhost",
                display_name="Local Admin", role="admin",
            )
            session.add(user)

        session.add(
            ApiKeyRow(
                id=new_id("key"), org_id=org.id, user_id=user.id, name="bootstrap",
                key_hash=digest, key_prefix=raw_key[:12], role="admin",
            )
        )
        return {"org_id": org.id, "user_id": user.id, "api_key": raw_key, "created": "true"}


def create_api_key(org_id: str, user_id: str, name: str, role: str = "engineer") -> dict[str, str]:
    """Issue a new key. The raw value is returned once and never stored."""
    raw_key = generate_key()
    with session_scope() as session:
        session.add(
            ApiKeyRow(
                id=new_id("key"), org_id=org_id, user_id=user_id, name=name,
                key_hash=hash_key(raw_key), key_prefix=raw_key[:12], role=role,
            )
        )
    return {"api_key": raw_key, "name": name, "role": role}


def revoke_api_key(key_id: str) -> bool:
    with session_scope() as session:
        row = session.get(ApiKeyRow, key_id)
        if row is None:
            return False
        row.active = False
        return True
