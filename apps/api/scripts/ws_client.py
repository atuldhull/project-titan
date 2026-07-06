"""Minimal WebSocket test client (Phase 24 DoD, TITAN-BCK-007).

A dependency-light CLI that connects to ``/ws``, subscribes to a project (and the
always-on shell topics), replies to server pings, and prints every frame it
receives. It is the walking-skeleton client the DoD scenarios drive from the
terminal — the production dashboard client lands with the Vol 4 data layer.

    uv run python scripts/ws_client.py \
        --url ws://localhost:8000/ws --token "$JWT" --project "$PID" [--last-seq N]

``--collect N`` exits after N frames (for scripted assertions); the default
streams until interrupted. Two instances against the same project, plus a
``task.updated`` transition, demonstrate the ≤ 250 ms two-client fan-out.
"""

import argparse
import asyncio
import contextlib
import json
from typing import Any

import websockets


async def _run(args: argparse.Namespace) -> int:
    uri = f"{args.url}?token={args.token}"
    async with websockets.connect(uri, max_size=2**22) as ws:
        subscribe: dict[str, Any] = {"type": "subscribe", "topics": args.topics}
        if args.project:
            subscribe["project_id"] = args.project
        if args.last_seq is not None:
            subscribe["last_seq"] = args.last_seq
        await ws.send(json.dumps(subscribe))

        seen = 0
        async for raw in ws:
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "ping":  # keep the connection alive
                await ws.send(json.dumps({"type": "pong"}))
                continue
            print(json.dumps(frame))
            seen += 1
            if args.collect and seen >= args.collect:
                return 0
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="TITAN WebSocket test client")
    parser.add_argument("--url", default="ws://localhost:8000/ws")
    parser.add_argument("--token", required=True, help="RS256 access JWT")
    parser.add_argument("--project", default="", help="project_id to subscribe to")
    parser.add_argument(
        "--topics", nargs="*", default=[], help="project topic filter (e.g. tasks runs)"
    )
    parser.add_argument("--last-seq", type=int, default=None, help="reconnect cursor")
    parser.add_argument("--collect", type=int, default=0, help="exit after N frames (0 = stream)")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
