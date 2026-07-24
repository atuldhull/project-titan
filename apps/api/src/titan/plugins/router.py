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

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.plugins import manager, service
from titan.plugins.manifest import ManifestError
from titan.plugins.secrets import ENV_PREFIX, verify_hmac_sha256
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


def _invalid_spec(errors: list[str]) -> ProblemException:
    return ProblemException(
        status=422,
        title="Invalid MCP server",
        detail="; ".join(errors),
        problem_type=problem_type("validation"),
    )


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


@router.get("/plugins/{plugin_id}/mcp")
async def mcp_introspection(request: Request, plugin_id: str, principal: _Reader) -> dict[str, Any]:
    """For an enabled MCP server (§7): the negotiated protocol version, its discovered
    tools (registered as ``mcp.<id>.*``), its resources (read via ``mcp.<id>.read_resource``)
    and its prompts (surfaced to the prompt library, Vol 2)."""
    _engine, supervisor, _scopes, _cipher = _runtime(request)
    info: dict[str, Any] | None = supervisor.mcp_info(plugin_id)
    if info is None:
        raise ProblemException(
            status=404,
            title="Not an enabled MCP server",
            detail=f"plugin '{plugin_id}' is not an enabled MCP runtime",
            problem_type=problem_type("not-found"),
        )
    return info


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
    """Operator settings from ``config_schema``, encrypted at rest (SEC-063).

    A submitted value still equal to the mask means "unchanged": the form
    renders saved secrets as ``••••``, so persisting that verbatim would
    replace a working credential with four bullet characters and leave the
    plugin failing to authenticate against a config that looks correct.
    """
    engine, _sup, _scopes, cipher = _runtime(request)
    async with engine.connect() as conn:
        install = await service.get_install(conn, plugin_id)
        current = await service.get_config(conn, plugin_id, cipher=cipher)
    schema = manager.load_config_schema(install)
    merged = manager.strip_sentinels(body.config, current, manager.secret_keys_of(schema))
    return await service.configure(engine, plugin_id, merged, cipher=cipher)


@router.get("/plugins/{plugin_id}/config", response_model=manager.PluginConfigOut)
async def get_config(
    request: Request, plugin_id: str, principal: _Admin
) -> manager.PluginConfigOut:
    """The config form's data: the plugin's JSON Schema, its saved values with
    every write-only field masked, and the status of each manifest-declared
    secret (TITAN-FRT-035).

    `plugin:admin`, not `plugin:read`: even masked, a plugin's configuration
    names internal hosts, repositories and channels, and `plugin:read` is
    reachable by every agent.
    """
    _engine, _sup, _scopes, cipher = _runtime(request)
    async with request.app.state.engine.connect() as conn:
        install = await service.get_install(conn, plugin_id)
        saved = await service.get_config(conn, plugin_id, cipher=cipher)
    schema = manager.load_config_schema(install)
    # Declared-but-unreadable is NOT the same as "declares none". Without the
    # schema nothing identifies which keys are secret, so an empty key list
    # would return every stored credential verbatim. Fail closed: mask
    # everything and tell the dialog why it is read-only.
    unavailable = schema is None and manager.declares_config_schema(install)
    secret_keys = manager.secret_keys_of(schema)
    return manager.PluginConfigOut(
        plugin_id=plugin_id,
        config_schema=schema,
        schema_unavailable=unavailable,
        values=manager.redact_config(saved, secret_keys, mask_all=unavailable),
        secret_keys=secret_keys,
        secrets=await manager.secret_status(
            install.manifest, request.app.state.plugin_secrets, env_prefix=ENV_PREFIX
        ),
    )


@router.get("/plugins/{plugin_id}/health", response_model=manager.PluginHealthOut)
async def health(request: Request, plugin_id: str, principal: _Reader) -> manager.PluginHealthOut:
    """Whether the supervisor is actually holding this plugin up.

    There is no health COLUMN — ``plugin_installs`` records lifecycle state and
    an error counter, not liveness — so "healthy" is computed from the live
    supervisor rather than read back from a row that nothing updates.
    """
    _engine, supervisor, _scopes, _cipher = _runtime(request)
    async with request.app.state.engine.connect() as conn:
        install = await service.get_install(conn, plugin_id, supervisor)
    return manager.PluginHealthOut(
        plugin_id=plugin_id,
        state=install.state,
        running=plugin_id in supervisor.running(),
        tools=install.tools,
        error_count=install.error_count,
        last_error=install.last_error,
    )


@router.post("/plugins:preview-mcp", response_model=manager.McpPreviewOut)
async def preview_mcp(
    request: Request, body: manager.McpServerSpec, principal: _Admin
) -> manager.McpPreviewOut:
    """Connect to a candidate MCP server and list its tools WITHOUT installing.

    §6.12 wants the operator to see what a server offers before granting it
    anything. A failed connection is a 200 with ``ok=false`` and the reason —
    "your URL is wrong" is the answer to the question, not a server error.
    """
    _engine, supervisor, scopes, _cipher = _runtime(request)
    try:
        candidate = manager.validate_spec(body, known_scopes=scopes)
    except ManifestError as exc:
        raise _invalid_spec(exc.errors) from exc
    except ValueError as exc:
        raise _invalid_spec([str(exc)]) from exc
    try:
        version, info, tools = await supervisor.preview_mcp(
            candidate, candidate.permissions, plugin_dir=str(request.app.state.plugins_dir)
        )
    except Exception as exc:  # noqa: BLE001 — a failed probe is a reportable RESULT
        logger.info("plugin.mcp_preview_failed", plugin_id=body.plugin_id, error=str(exc))
        return manager.McpPreviewOut(ok=False, detail=str(exc)[:500])
    return manager.McpPreviewOut(
        ok=True,
        protocol_version=version,
        server_info=info,
        tools=manager.tool_previews(tools),
    )


@router.post("/plugins:add-mcp", response_model=PluginOut, status_code=201)
async def add_mcp(request: Request, body: manager.McpServerSpec, principal: _Admin) -> PluginOut:
    """Add an MCP server by URL or command (§6.12).

    Materialises it as an ordinary plugin directory and re-runs discovery, so
    the server then installs, enables, quarantines and upgrades through the
    Phase-52 lifecycle rather than acquiring a parallel one of its own. It
    lands as AVAILABLE — adding is not granting.
    """
    engine, _sup, scopes, _cipher = _runtime(request)
    try:
        manager.validate_spec(body, known_scopes=scopes)
    except ManifestError as exc:
        raise _invalid_spec(exc.errors) from exc
    except ValueError as exc:
        raise _invalid_spec([str(exc)]) from exc

    plugins_dir = Path(request.app.state.plugins_dir)
    try:
        await asyncio.to_thread(manager.write_mcp_manifest, plugins_dir, body)
    except FileExistsError as exc:
        raise ProblemException(
            status=409,
            title="Already exists",
            detail=str(exc),
            problem_type=problem_type("conflict"),
        ) from exc
    except OSError as exc:
        raise ProblemException(
            status=500,
            title="Could not write plugin",
            detail=str(exc),
            problem_type=problem_type("internal"),
        ) from exc

    discovered = await service.discover(engine, plugins_dir, known_scopes=scopes)
    added = next((p for p in discovered if p.plugin_id == body.plugin_id), None)
    if added is None:
        raise ProblemException(
            status=500,
            title="Discovery failed",
            detail=f"'{body.plugin_id}' was written but did not discover",
            problem_type=problem_type("internal"),
        )
    return added


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


_TIMESTAMP_HEADERS = ("x-titan-timestamp", "x-slack-request-timestamp", "x-signature-timestamp")


def _reject_stale_timestamp(request: Request, skew_seconds: int) -> None:
    """Reject a delivery whose signed timestamp is older than the window (SEC-075).

    Applies only when a timestamp header is present (GitHub's body-only HMAC
    carries none, so its freshness rests on replay dedup). A malformed timestamp
    is rejected 401 rather than ignored — fail closed on the parse."""
    for header in _TIMESTAMP_HEADERS:
        raw = request.headers.get(header)
        if raw is None:
            continue
        try:
            sent = int(float(raw))
        except ValueError as exc:
            raise ProblemException(
                status=401,
                title="Invalid timestamp",
                detail="the webhook timestamp is malformed",
                problem_type=problem_type("unauthorized"),
            ) from exc
        if abs(time.time() - sent) > skew_seconds:
            raise ProblemException(
                status=401,
                title="Stale delivery",
                detail="the webhook timestamp is outside the accepted window",
                problem_type=problem_type("unauthorized"),
            )
        return


async def _reject_replay(redis: Any, plugin_id: str, signature: str, ttl_seconds: int) -> None:
    """Reject a replayed delivery (SEC-075), deduped on the HMAC SIGNATURE — the
    one value an attacker cannot forge or rotate (unlike the unsigned delivery-id
    headers). A replay reuses the exact signature → same key → rejected. Fails
    OPEN on a Redis outage: a cache blip must not reject genuine webhooks — the
    HMAC still authenticates them."""
    key = f"webhook:seen:{plugin_id}:{hashlib.sha256(signature.encode()).hexdigest()}"
    try:
        first_seen = await redis.set(key, "1", nx=True, ex=max(ttl_seconds, 60))
    except Exception:  # pragma: no cover - fail-open path
        logger.warning("plugin.webhook_dedup_unavailable", plugin_id=plugin_id)
        return
    if not first_seen:
        logger.warning("plugin.webhook_replay", plugin_id=plugin_id)
        raise ProblemException(
            status=401,
            title="Replayed delivery",
            detail="this webhook delivery has already been processed",
            problem_type=problem_type("unauthorized"),
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
    # SEC-075 freshness. Two orthogonal checks after the signature verifies:
    #
    #  * Replay dedup (all schemes): a replayed delivery reuses the exact same
    #    signature, so a first-seen marker keyed on the delivery id (GitHub sends
    #    a unique X-GitHub-Delivery; else the signature digest) rejects the
    #    second copy. This is the ONLY freshness guarantee GitHub's body-only
    #    HMAC can carry — it signs no timestamp.
    #  * Timestamp skew (schemes that send a signed timestamp): reject deliveries
    #    older than the configured window. An UNSIGNED timestamp would be
    #    forgeable, but the only attack that enables — replaying with a fresh
    #    timestamp — is independently caught by the replay dedup above.
    settings = request.app.state.settings
    _reject_stale_timestamp(request, settings.webhook_max_skew_seconds)
    # Dedup key is the SIGNATURE — the ONLY value the attacker cannot forge (it
    # is HMAC(secret, body) and they lack the secret). Deliberately NOT the
    # delivery-id headers: those are unsigned and client-controlled, so keying on
    # them let an attacker rotate x-github-delivery to defeat dedup while
    # replaying a captured signed body (Phase-70 review, CRITICAL). Two genuinely
    # distinct deliveries always differ in body → differ in signature; a GitHub
    # re-delivery reuses the exact signature and is correctly deduped.
    await _reject_replay(
        request.app.state.redis, plugin_id, signature, settings.webhook_replay_ttl_seconds
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
