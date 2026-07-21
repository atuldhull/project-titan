"""The write-only secrets API (TITAN-SEC-051).

Values can be created, rotated, and deleted — never read back. Every read path a
client might reach for a value returns 405/404: there is simply no endpoint that
returns a value, and ``GET /secrets/{name}/value`` exists ONLY to answer 405 so
the write-only contract is explicit and testable. Metadata (never a value) is
readable under the separate, carved-out ``secret:read`` scope.

RBAC: ``secret`` is in ``SENSITIVE_RESOURCES``, so neither ``secret:read`` nor
``secret:write`` is reachable by the ``*:read`` wildcard every role carries —
both must be granted explicitly (Owner/Admin/Maintainer per §4.2). Agents run as
``operator`` and never hold either.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from pydantic import BaseModel, Field, SecretStr

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires, RequiresAuth
from titan.identity.service import Principal
from titan.vault import service
from titan.vault.provider import DEFAULT_ROTATION_DAYS, SecretMetadata, SecretScope, SecretsStore

router = APIRouter(prefix="/api/v1", tags=["secrets"])

#: Org-administration surface — no project tenancy, so a missing permission is a
#: plain 403 (there is no object whose existence could leak).
_ManageSecret = Annotated[Principal, Depends(Requires("secret:write"))]
_ReadSecretMeta = Annotated[Principal, Depends(Requires("secret:read"))]
_AnyAuth = Annotated[Principal, Depends(RequiresAuth())]


class CreateSecretIn(BaseModel):
    name: str = Field(max_length=128)
    #: SecretStr so an accidental log/repr masks the value.
    value: SecretStr
    scope: SecretScope = SecretScope.ORG
    scope_ref: str | None = Field(default=None, max_length=200)
    rotation_days: int = Field(default=DEFAULT_ROTATION_DAYS, ge=1, le=3650)


class RotateSecretIn(BaseModel):
    value: SecretStr


class SecretMetadataOut(BaseModel):
    """Everything a read returns — metadata only, and the derived expiry (SEC-052)."""

    name: str
    scope: SecretScope
    scope_ref: str | None
    owner_id: str | None
    rotation_days: int
    created_at: datetime
    rotated_at: datetime
    updated_at: datetime
    due_at: datetime
    overdue: bool

    @classmethod
    def of(cls, meta: SecretMetadata, *, now: datetime) -> SecretMetadataOut:
        return cls(
            name=meta.name,
            scope=meta.scope,
            scope_ref=meta.scope_ref,
            owner_id=meta.owner_id,
            rotation_days=meta.rotation_days,
            created_at=meta.created_at,
            rotated_at=meta.rotated_at,
            updated_at=meta.updated_at,
            due_at=meta.due_at,
            overdue=meta.is_overdue(now=now),
        )


def _store(request: Request) -> SecretsStore:
    store: SecretsStore | None = getattr(request.app.state, "vault_store", None)
    if store is None:
        raise ProblemException(
            status=503,
            title="Secrets vault not configured",
            detail=(
                "No writable secrets vault is configured. Set TITAN_VAULT_AGE_KEY "
                "to an age identity so create/rotate/delete have a store to write."
            ),
            problem_type=problem_type("vault-unavailable"),
        )
    return store


def _owner_id(principal: Principal) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(principal.sub))
    except (ValueError, TypeError):
        return None


@router.post("/secrets", response_model=SecretMetadataOut, status_code=201)
async def create_secret(
    request: Request, principal: _ManageSecret, body: CreateSecretIn
) -> SecretMetadataOut:
    """Create a secret (write-only). The value is stored age-encrypted and never
    round-trips; the response is metadata only."""
    store = _store(request)
    now = datetime.now(tz=UTC)
    async with request.app.state.engine.begin() as conn:
        try:
            meta = await service.create_secret(
                conn,
                store,
                name=body.name,
                value=body.value.get_secret_value(),
                scope=body.scope,
                scope_ref=body.scope_ref,
                owner_id=_owner_id(principal),
                rotation_days=body.rotation_days,
                actor=str(principal.sub),
                now=now,
            )
        except service.InvalidSecretName as exc:
            raise ProblemException(
                status=400,
                title="Invalid secret name",
                detail="A secret name must be 1–128 chars of [A-Za-z0-9._-] and no '..'.",
                problem_type=problem_type("invalid-secret-name"),
            ) from exc
        except service.SecretExists as exc:
            raise ProblemException(
                status=409,
                title="Secret already exists",
                detail=f"A secret named {body.name!r} already exists; rotate it instead.",
                problem_type=problem_type("secret-exists"),
            ) from exc
    return SecretMetadataOut.of(meta, now=now)


@router.post("/secrets/{name}/rotate", response_model=SecretMetadataOut)
async def rotate_secret(
    request: Request, principal: _ManageSecret, name: str, body: RotateSecretIn
) -> SecretMetadataOut:
    """Replace a secret's value (write-only), resetting its rotation clock."""
    store = _store(request)
    now = datetime.now(tz=UTC)
    async with request.app.state.engine.begin() as conn:
        try:
            meta = await service.rotate_secret(
                conn,
                store,
                name=name,
                value=body.value.get_secret_value(),
                actor=str(principal.sub),
                now=now,
            )
        except service.InvalidSecretName as exc:
            raise _not_found(name) from exc
        except service.SecretNotFound as exc:
            raise _not_found(name) from exc
    return SecretMetadataOut.of(meta, now=now)


@router.delete("/secrets/{name}", status_code=204)
async def delete_secret(request: Request, principal: _ManageSecret, name: str) -> Response:
    """Delete a secret (metadata + stored value)."""
    store = _store(request)
    async with request.app.state.engine.begin() as conn:
        try:
            await service.delete_secret(conn, store, name=name, actor=str(principal.sub))
        except service.InvalidSecretName as exc:
            raise _not_found(name) from exc
        except service.SecretNotFound as exc:
            raise _not_found(name) from exc
    return Response(status_code=204)


@router.get("/secrets", response_model=list[SecretMetadataOut])
async def list_secrets(request: Request, principal: _ReadSecretMeta) -> list[SecretMetadataOut]:
    """List secret metadata (never values), including expiry state (SEC-052)."""
    now = datetime.now(tz=UTC)
    async with request.app.state.engine.connect() as conn:
        metas = await service.list_secret_metadata(conn)
    return [SecretMetadataOut.of(m, now=now) for m in metas]


@router.get("/secrets/{name}", response_model=SecretMetadataOut)
async def get_secret(request: Request, principal: _ReadSecretMeta, name: str) -> SecretMetadataOut:
    """Metadata for one secret (never its value)."""
    now = datetime.now(tz=UTC)
    async with request.app.state.engine.connect() as conn:
        meta = await service.get_secret_metadata(conn, name)
    if meta is None:
        raise _not_found(name)
    return SecretMetadataOut.of(meta, now=now)


@router.get("/secrets/{name}/value")
async def read_secret_value(principal: _AnyAuth, name: str) -> Response:
    """There is no such thing (TITAN-SEC-051). A value is NEVER read back through
    any API or UI — this route exists only to answer 405 explicitly rather than a
    bare 404, so the write-only contract is discoverable."""
    raise ProblemException(
        status=405,
        title="Secrets are write-only",
        detail="Secret values are never read back via any API or UI (TITAN-SEC-051).",
        problem_type=problem_type("secret-write-only"),
        headers={"Allow": "POST, DELETE"},
    )


def _not_found(name: str) -> ProblemException:
    # 404 (never 403) so a probe cannot enumerate which names exist.
    return ProblemException(
        status=404,
        title="Not found",
        detail=f"No such secret {name!r}.",
        problem_type=problem_type("not-found"),
    )


def mount(app: FastAPI) -> None:
    app.include_router(router)


__all__ = ["SecretMetadataOut", "mount", "router"]
