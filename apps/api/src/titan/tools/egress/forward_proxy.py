"""The authenticating, logging forward proxy (Phase 23; TITAN-SEC-043).

A hand-rolled asyncio HTTP proxy — the SOLE hop out of the sandbox's deny-all
internal network. It speaks the standard proxy protocol: ``CONNECT`` to tunnel
HTTPS (opaque, no TLS interception) and absolute-URI HTTP to forward plaintext.
Every request is authenticated against the per-task token, allowlist-checked, and
structured-logged with project/task attribution; a blocked host gets a 403 and a
``SANDBOX_VIOLATION`` audit row (a raw ``curl https://example.com`` that honors
the injected proxy env therefore FAILS and is AUDITED). Missing/invalid auth is
a 407.

No third-party proxy dependency: stdlib asyncio only.
"""

import asyncio
import base64
import contextlib

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.tools.egress.allowlist import Allowlist
from titan.tools.egress.audit import record_egress_violation
from titan.tools.egress.hosts import is_host_approved
from titan.tools.egress.tokens import TokenClaims, verify_task_token

logger = structlog.get_logger("titan.tools.egress.forward")

_CONNECT_TIMEOUT_S = 15.0
_IDLE_TIMEOUT_S = 300.0
_CHUNK = 64 * 1024


class ForwardProxy:
    """One instance per proxy process; ``serve`` runs it until cancelled."""

    def __init__(
        self,
        *,
        secret: str,
        allowlist: Allowlist,
        engine: AsyncEngine,
        idle_timeout_s: float = _IDLE_TIMEOUT_S,
    ) -> None:
        self._secret = secret
        self._allowlist = allowlist
        self._engine = engine
        self._idle = idle_timeout_s

    async def serve(self, host: str, port: int) -> asyncio.Server:
        server = await asyncio.start_server(self._handle, host, port)
        logger.info("egress.forward.listening", host=host, port=port)
        return server

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30.0)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            await self._close(writer)
            return
        try:
            await self._dispatch(head, reader, writer)
        except Exception as exc:  # noqa: BLE001 — one bad client must not kill the proxy
            logger.warning("egress.forward.error", error=str(exc))
            await self._close(writer)

    async def _dispatch(
        self, head: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        lines = head.split(b"\r\n")
        request_line = lines[0].decode("latin-1")
        parts = request_line.split(" ")
        if len(parts) != 3:
            await self._respond(writer, 400, "Bad Request")
            return
        method, target, _ = parts
        headers = _parse_headers(lines[1:])

        claims = self._authenticate(headers)
        if claims is None:
            await self._respond(
                writer,
                407,
                "Proxy Authentication Required",
                extra='Proxy-Authenticate: Basic realm="titan"\r\n',
            )
            return

        if method == "CONNECT":
            await self._connect(target, claims, reader, writer)
        else:
            await self._forward(method, target, headers, head, reader, writer, claims)

    async def _permitted(self, host: str, claims: TokenClaims) -> bool:
        """Baseline allowlist (offline), then — only if that fails — the project's
        Admin-approved hosts (SEC-033). The DB read is the rare, off-baseline path."""
        if self._allowlist.is_allowed(host, claims):
            return True
        try:
            async with self._engine.connect() as conn:
                return await is_host_approved(conn, claims.project_id, host)
        except Exception:  # noqa: BLE001 — a DB hiccup must fail closed (deny)
            return False

    def _authenticate(self, headers: dict[str, str]) -> TokenClaims | None:
        raw = headers.get("proxy-authorization", "")
        if not raw.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(raw[6:].strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        user, _, token = decoded.partition(":")
        if user != "task" or not token:
            return None
        return verify_task_token(self._secret, token)

    async def _connect(
        self,
        target: str,
        claims: TokenClaims,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        host, port = _split_hostport(target, default_port=443)
        if not await self._permitted(host, claims):
            await self._deny("CONNECT", host, claims, writer)
            return
        try:
            upstream_r, upstream_w = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=_CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            logger.info("egress.forward.upstream_fail", host=host, error=str(exc))
            await self._respond(writer, 502, "Bad Gateway")
            return
        logger.info(
            "egress.forward.allow",
            method="CONNECT",
            host=host,
            port=port,
            project_id=str(claims.project_id),
            task_id=str(claims.task_id) if claims.task_id else None,
        )
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._tunnel(reader, writer, upstream_r, upstream_w, half=False)

    async def _forward(
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        head: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        claims: TokenClaims,
    ) -> None:
        if not target.lower().startswith("http://"):
            await self._respond(writer, 400, "Only absolute-form http:// or CONNECT is supported")
            return
        rest = target[len("http://") :]
        authority, _, path = rest.partition("/")
        host, port = _split_hostport(authority, default_port=80)
        if not await self._permitted(host, claims):
            await self._deny(method, host, claims, writer)
            return
        try:
            upstream_r, upstream_w = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=_CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            logger.info("egress.forward.upstream_fail", host=host, error=str(exc))
            await self._respond(writer, 502, "Bad Gateway")
            return
        logger.info(
            "egress.forward.allow",
            method=method,
            host=host,
            project_id=str(claims.project_id),
            task_id=str(claims.task_id) if claims.task_id else None,
        )
        # Rewrite absolute-form → origin-form, drop hop-by-hop/proxy headers.
        request = _rebuild_request(method, "/" + path, headers)
        upstream_w.write(request)
        await upstream_w.drain()
        body_len = int(headers.get("content-length", "0") or "0")
        if body_len > 0:
            await _pump_n(reader, upstream_w, body_len)
        # Only stream the response back (the request body, if any, is already sent).
        await self._tunnel(reader, writer, upstream_r, upstream_w, half=True)

    async def _deny(
        self, method: str, host: str, claims: TokenClaims, writer: asyncio.StreamWriter
    ) -> None:
        logger.warning(
            "egress.forward.deny",
            method=method,
            host=host,
            project_id=str(claims.project_id),
            task_id=str(claims.task_id) if claims.task_id else None,
        )
        # The egress is denied regardless; but a failed audit write MUST be loud,
        # not silently swallowed (SEC-043 requires the violation be audited).
        try:
            await record_egress_violation(self._engine, claims, method=method, host=host)
        except Exception as exc:  # noqa: BLE001 — surfaced, never hidden
            logger.error(
                "egress.forward.audit_write_failed",
                host=host,
                project_id=str(claims.project_id),
                error=str(exc),
            )
        await self._respond(writer, 403, f"egress to {host!r} is denied by policy")

    async def _tunnel(
        self,
        client_r: asyncio.StreamReader,
        client_w: asyncio.StreamWriter,
        upstream_r: asyncio.StreamReader,
        upstream_w: asyncio.StreamWriter,
        *,
        half: bool,
    ) -> None:
        # Splice upstream->client always; client->upstream only for a real
        # (bidirectional) CONNECT tunnel — an HTTP forward already sent its body.
        tasks = [asyncio.create_task(self._pipe(upstream_r, client_w))]
        if not half:
            tasks.append(asyncio.create_task(self._pipe(client_r, upstream_w)))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._close(upstream_w)
            await self._close(client_w)

    async def _pipe(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while True:
                chunk = await asyncio.wait_for(src.read(_CHUNK), timeout=self._idle)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()

    async def _respond(
        self, writer: asyncio.StreamWriter, code: int, message: str, *, extra: str = ""
    ) -> None:
        body = message.encode("utf-8")
        head = (
            f"HTTP/1.1 {code} {message}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"{extra}Connection: close\r\n\r\n"
        ).encode("latin-1")
        with contextlib.suppress(Exception):
            writer.write(head + body)
            await writer.drain()
        await self._close(writer)

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


def _parse_headers(raw_lines: list[bytes]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw_lines:
        if not line:
            continue
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


_HOP_BY_HOP = frozenset(
    {
        "proxy-authorization",
        "proxy-connection",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _rebuild_request(method: str, path: str, headers: dict[str, str]) -> bytes:
    lines = [f"{method} {path} HTTP/1.1"]
    for name, value in headers.items():
        if name in _HOP_BY_HOP:
            continue
        lines.append(f"{name}: {value}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _split_hostport(authority: str, *, default_port: int) -> tuple[str, int]:
    if authority.startswith("[") and "]" in authority:  # IPv6 literal
        host, _, rest = authority[1:].partition("]")
        port = int(rest.lstrip(":")) if rest.startswith(":") else default_port
        return host, port
    host, sep, port_s = authority.partition(":")
    return host, int(port_s) if sep and port_s.isdigit() else default_port


async def _pump_n(src: asyncio.StreamReader, dst: asyncio.StreamWriter, n: int) -> None:
    remaining = n
    while remaining > 0:
        chunk = await src.read(min(_CHUNK, remaining))
        if not chunk:
            break
        dst.write(chunk)
        await dst.drain()
        remaining -= len(chunk)


__all__ = ["ForwardProxy"]
