"""Plugin Manager REST + webhook + UI-panel endpoints (Vol 7 §3.2/§3.3, TITAN-INT-011).

Lifecycle operations require the ``plugin:admin`` RBAC permission — granting a plugin
its declared capabilities is an operator act, never an agent's.

Two non-CRUD surfaces:

* **Webhooks** ``POST /api/v1/plugins/{id}/hooks/{path}`` — called by EXTERNAL systems,
  so there is no TITAN JWT; authentication is an HMAC-SHA256 signature over the RAW body
  verified against the plugin's named secret (the route is listed in PUBLIC_PATHS for
  exactly that reason). Unverified payloads are rejected 401 and logged.
* **UI panels** ``GET /plugin-ui/{id}/{path}`` — assets served from the plugin's own
  ``ui/`` directory by a GUARDED route, never a bare StaticFiles mount (which would
  bypass RBAC), with an explicit path-traversal check.
"""

import json
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.plugins import service
from titan.plugins.secrets import verify_hmac_sha256
from titan.plugins.service import PermissionDiff, PluginInstallOut, PluginOut

logger = structlog.get_logger("titan.plugins.router")

router = APIRouter(prefix="/api/v1", tags=["plugins"])
ui_router = APIRouter(tags=["plugins"])

_Admin = Annotated[Principal, Depends(Requires("plugin:admin"))]
_Reader = Annotated[Principal, Depends(Requires("plugin:read"))]

_MEDIA_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".md": "text/markdown",
}


class ConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any] = Field(default_factory=dict)


def _runtime(request: Request) -> tuple[Any, Any, frozenset[str], Any]:
    """(engine, supervisor, known_scopes, cipher) — 503 when the runtime is disabled."""
    state = request.app.state
    supervisor = getattr(state, "plugin_supervisor", None)
    if supervisor is None:
        raise ProblemException(
            status=503,
            title="Plugin runtime disabled",
            detail="the plugin runtime is not enabled on this deployment",
            problem_type=problem_type("unavailable"),
        )
    return state.engine, supervisor, state.plugin_scopes, state.plugin_cipher


@router.get("/plugins", response_model=list[PluginOut])
async def list_catalogue(request: Request, principal: _Reader) -> list[PluginOut]:
    """The discovered catalogue (AVAILABLE)."""
    async with request.app.state.engine.connect() as conn:
        return await service.list_catalogue(conn)


@router.get("/plugins/installs", response_model=list[PluginInstallOut])
async def list_installs(request: Request, principal: _Reader) -> list[PluginInstallOut]:
    async with request.app.state.engine.connect() as conn:
        return await service.list_installs(conn)


@router.post("/plugins:discover", response_model=list[PluginOut])
async def discover(request: Request, principal: _Admin) -> list[PluginOut]:
    engine, _sup, scopes, _cipher = _runtime(request)
    return await service.discover(engine, Path(request.app.state.plugins_dir), known_scopes=scopes)


@router.get("/plugins/{plugin_id}", response_model=PluginInstallOut)
async def get_install(request: Request, plugin_id: str, principal: _Reader) -> PluginInstallOut:
    _engine, supervisor, _scopes, _cipher = _runtime(request)
    async with request.app.state.engine.connect() as conn:
        return await service.get_install(conn, plugin_id, supervisor)


@router.post("/plugins/{plugin_id}/install", response_model=PluginInstallOut)
async def install(request: Request, plugin_id: str, principal: _Admin) -> PluginInstallOut:
    engine, _sup, scopes, _cipher = _runtime(request)
    return await service.install(engine, plugin_id, known_scopes=scopes)


@router.post("/plugins/{plugin_id}/enable", response_model=PluginInstallOut)
async def enable(request: Request, plugin_id: str, principal: _Admin) -> PluginInstallOut:
    """Grant the manifest's declared permissions and start the sandboxed worker."""
    engine, supervisor, scopes, _cipher = _runtime(request)
    return await service.enable(engine, supervisor, plugin_id, known_scopes=scopes)


@router.post("/plugins/{plugin_id}/disable", response_model=PluginInstallOut)
async def disable(request: Request, plugin_id: str, principal: _Admin) -> PluginInstallOut:
    engine, supervisor, _scopes, _cipher = _runtime(request)
    return await service.disable(engine, supervisor, plugin_id)


@router.post("/plugins/{plugin_id}/upgrade", response_model=PermissionDiff)
async def upgrade(request: Request, plugin_id: str, principal: _Admin) -> PermissionDiff:
    """Re-pin the on-disk manifest. A permission WIDENING leaves the plugin inactive
    pending re-approval (TITAN-INT-017); the diff says exactly what was added."""
    engine, supervisor, scopes, _cipher = _runtime(request)
    return await service.upgrade(engine, supervisor, plugin_id, known_scopes=scopes)


@router.delete("/plugins/{plugin_id}", response_model=PluginInstallOut)
async def remove(request: Request, plugin_id: str, principal: _Admin) -> PluginInstallOut:
    engine, supervisor, _scopes, _cipher = _runtime(request)
    return await service.remove(engine, supervisor, plugin_id)


@router.put("/plugins/{plugin_id}/config", response_model=PluginInstallOut)
async def configure(
    request: Request, plugin_id: str, body: ConfigIn, principal: _Admin
) -> PluginInstallOut:
    """Operator settings from ``config_schema``, encrypted at rest (SEC-063)."""
    engine, _sup, _scopes, cipher = _runtime(request)
    return await service.configure(engine, plugin_id, body.config, cipher=cipher)


# --- webhooks (HMAC-authenticated; deliberately no JWT) ----------------------


def _declared_hook(manifest: dict[str, Any], hook_path: str) -> dict[str, Any]:
    for hook in (manifest.get("contributes") or {}).get("webhooks") or []:
        if str(hook.get("path", "")).lstrip("/") == hook_path.lstrip("/"):
            return dict(hook)
    raise ProblemException(
        status=404,
        title="Unknown webhook",
        detail=f"no webhook is declared at '{hook_path}'",
        problem_type=problem_type("not-found"),
    )


@router.post("/plugins/{plugin_id}/hooks/{hook_path:path}")
async def webhook(request: Request, plugin_id: str, hook_path: str) -> dict[str, Any]:
    """Verify HMAC-SHA256 over the RAW body against the plugin's named secret, then
    deliver to the plugin. An unverified payload is rejected and logged."""
    engine, supervisor, _scopes, _cipher = _runtime(request)
    async with engine.connect() as conn:
        install = await service.get_install(conn, plugin_id)
    if install.state != service.STATE_ENABLED:
        raise ProblemException(
            status=409,
            title="Plugin not enabled",
            detail=f"plugin '{plugin_id}' is {install.state}",
            problem_type=problem_type("conflict"),
        )

    hook = _declared_hook(install.manifest, hook_path)
    body = await request.body()
    # Webhooks are always HMAC-SHA256 verified (the manifest offers no other mode): this
    # signature over the RAW body is the sole authentication, since the route is public
    # to the JWT layer (an external caller has no TITAN token).
    signature = request.headers.get("x-titan-signature") or request.headers.get(
        "x-hub-signature-256", ""
    )
    try:
        secret = await request.app.state.plugin_secrets.get(str(hook.get("secret") or ""))
    except KeyError:
        secret = ""
    if not verify_hmac_sha256(secret, body, signature):
        logger.warning("plugin.webhook_unverified", plugin_id=plugin_id, path=hook_path)
        raise ProblemException(
            status=401,
            title="Invalid signature",
            detail="the webhook signature did not verify",
            problem_type=problem_type("unauthorized"),
        )
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        payload = {"raw": body.decode("utf-8", "replace")}
    delivered: dict[str, Any] = await supervisor.deliver_webhook(plugin_id, hook_path, payload)
    return delivered


# --- UI panels (guarded static assets) --------------------------------------


@ui_router.get("/plugin-ui/{plugin_id}/{asset_path:path}")
async def plugin_ui(
    request: Request, plugin_id: str, asset_path: str, principal: _Reader
) -> Response:
    """Serve a plugin's UI asset from its own ``ui/`` directory."""
    async with request.app.state.engine.connect() as conn:
        install = await service.get_install(conn, plugin_id)
    root = (Path(install.plugin_dir) / "ui").resolve()
    target = (root / asset_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ProblemException(
            status=404,
            title="Not found",
            detail=f"'{asset_path}' is not an asset of plugin '{plugin_id}'",
            problem_type=problem_type("not-found"),
        )
    return Response(
        content=target.read_bytes(),
        media_type=_MEDIA_TYPES.get(target.suffix, "application/octet-stream"),
    )


def mount(app: FastAPI) -> None:
    app.include_router(router)
    app.include_router(ui_router)
