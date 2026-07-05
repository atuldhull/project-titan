"""Idempotency-Key replay for mutating requests (§4.1, §7: idem:{key}, 24 h).

First request with a key stores its final response in Redis; replays return
the cached response with ``Idempotency-Replayed: true``. A concurrent
duplicate while the first is still in flight gets 409. Bodies are cached
base64-encoded (they may be gzip bytes — this middleware sits outside
compression per §9.1), and replay preserves the contract headers
(Content-Type, Content-Encoding, ETag, Location).

If the handler raises, the in-flight marker is deleted so a retry re-runs
the handler; the 60 s marker TTL is the backstop if the process dies before
cleanup.
"""

import base64
import hashlib
import json

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from titan.gateway.problems import PROBLEM_CONTENT_TYPE

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"
TTL_SECONDS = 24 * 60 * 60
_IN_FLIGHT = "__in_flight__"
_MUTATING = {"POST", "PATCH", "PUT", "DELETE"}
_REPLAYED_HEADERS = ("content-type", "content-encoding", "etag", "location")


def _redis_key(request: Request, key: str) -> str:
    # Scope the key to method+path so one client key can't replay across
    # endpoints; principal scoping joins in Phase 8.
    digest = hashlib.sha256(f"{request.method}:{request.url.path}:{key}".encode()).hexdigest()
    return f"idem:{digest}"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if request.method not in _MUTATING or not key:
            return await call_next(request)

        redis = request.app.state.redis
        redis_key = _redis_key(request, key)

        # NX marks this key in-flight atomically; losing the race means a
        # concurrent duplicate — do not run the handler twice.
        acquired = await redis.set(redis_key, _IN_FLIGHT, nx=True, ex=60)
        if not acquired:
            stored = await redis.get(redis_key)
            if stored == _IN_FLIGHT:
                return JSONResponse(
                    status_code=409,
                    media_type=PROBLEM_CONTENT_TYPE,
                    content={
                        "type": "https://titan.dev/errors/idempotency-in-flight",
                        "title": "Request already in flight",
                        "status": 409,
                        "detail": "A request with this Idempotency-Key is still processing.",
                        "instance": str(request.url.path),
                    },
                )
            cached = json.loads(stored)
            replay_headers = {k: v for k, v in cached["headers"].items() if v is not None}
            replay_headers[REPLAYED_HEADER] = "true"
            return Response(
                content=base64.b64decode(cached["body"]),
                status_code=cached["status"],
                headers=replay_headers,
            )

        try:
            response = await call_next(request)
            body = b""
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                body += chunk if isinstance(chunk, bytes) else chunk.encode()
        except Exception:
            # A raised exception must not poison the key: clear it so a
            # retry re-runs the handler (TTL covers process death).
            await redis.delete(redis_key)
            raise

        if response.status_code < 500:
            await redis.set(
                redis_key,
                json.dumps(
                    {
                        "status": response.status_code,
                        "headers": {name: response.headers.get(name) for name in _REPLAYED_HEADERS},
                        "body": base64.b64encode(body).decode(),
                    }
                ),
                ex=TTL_SECONDS,
            )
        else:
            # Never replay a returned 5xx either.
            await redis.delete(redis_key)

        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
