"""Mounts the ``/ws`` WebSocket route (ADR-009).

Kept apart from :mod:`titan.realtime.endpoint` so ``main.py`` mounts it the same
way it mounts the REST routers (``realtime_router.mount(app)``).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, WebSocket

from titan.identity.service import Principal
from titan.realtime.endpoint import WebSocketAuthGuard, serve_connection

router = APIRouter()
_guard = WebSocketAuthGuard()


@router.websocket("/ws")
async def websocket_route(
    websocket: WebSocket,
    principal: Annotated[Principal, Depends(_guard)],
    token: Annotated[str, Query()] = "",
) -> None:
    """`wss://…/ws?token=<jwt>` — the multiplexed realtime channel (TITAN-BCK-007).

    The ``_guard`` dependency authenticates the handshake (and marks the route
    protected for the route-guard lint); ``serve_connection`` owns the rest.
    """
    await serve_connection(websocket, principal, token)


def mount(app: FastAPI) -> None:
    app.include_router(router)


__all__ = ["mount", "router"]
