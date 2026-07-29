"""``python -m titan.tools.egress`` — the egress service process (Phase 23).

Runs BOTH the forward proxy (the sandbox's sole egress hop) and the fetch-gateway
(research proxy + pypi/npm caching mirror) in one process, mirroring the worker's
resilient-gather pattern. Reachable from the sandbox's internal network (forward
proxy) and from the tool host (gateway); it holds the only real internet egress.
"""

import asyncio
import contextlib

import httpx
import structlog
import uvicorn

from titan import clients
from titan.audit import service as audit_service
from titan.observability.logging import configure_logging
from titan.settings import Settings, get_settings
from titan.tools.egress.dependency_gate import GateDecision, MetadataResolver
from titan.tools.egress.service import (
    build_forward_proxy,
    build_gateway,
    build_search_backend,
)

logger = structlog.get_logger("titan.tools.egress")


def _mirror_hosts(settings: Settings) -> frozenset[str]:
    hosts = set()
    for url in (settings.sandbox_pip_index_url, settings.sandbox_npm_registry_url):
        if url:
            host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            if host:
                hosts.add(host.lower())
    return frozenset(hosts)


async def run(settings: Settings) -> None:  # pragma: no cover — live orchestration
    engine = clients.create_engine(settings)
    # A plain outbound client (NOT clients.create_http_client, which is base-URLed
    # at Qdrant) — the gateway fetches arbitrary research URLs + the real indexes.
    egress_client = httpx.AsyncClient(timeout=30.0, headers={"user-agent": "titan-egress/1.0"})
    role = settings.egress_role
    run_forward = role in ("forward", "both")
    run_gateway = role in ("gateway", "both")
    tasks = []
    forward_server = None
    if run_forward:
        proxy = build_forward_proxy(
            engine, secret=settings.egress_proxy_secret, mirror_hosts=_mirror_hosts(settings)
        )
        forward_server = await proxy.serve(
            settings.egress_forward_host, settings.egress_forward_port
        )
        tasks.append(forward_server.serve_forever())
    if run_gateway:
        # SEC-104: the dependency gate + its install-decision audit sink. Every
        # decision (allow OR block) is recorded to the immutable audit ledger
        # (Phase 73) — the mirror has no project context, and audit_events'
        # project_id is nullable, so it is the right sink (not tool_audit).
        audit = audit_service.build_audit_client(engine)

        async def _install_decision(name: str, ecosystem: str, decision: GateDecision) -> None:
            # Best-effort: audit.record() is deliberately NOT best-effort (it does a
            # real hash-chain append that can raise on a DB blip / tip contention),
            # so an audit failure must NOT turn a legitimate install into a 500 or
            # a block into a 500 that also loses the record. Swallow it here, like
            # the composition-root rate-limit hook in main.py.
            with contextlib.suppress(Exception):
                await audit.record(
                    actor="egress-mirror",
                    action=audit_service.AuditAction.SCAN_RESULT,
                    resource=f"{ecosystem}:{name}",
                    outcome=audit_service.Outcome.SUCCESS
                    if decision.allowed
                    else audit_service.Outcome.DENIED,
                    detail={
                        "reason": decision.reason.value,
                        "detail": decision.detail,
                        "typosquat_of": decision.typosquat_of,
                    },
                )

        gateway = build_gateway(
            client=egress_client,
            internal_token=settings.research_gateway_token,
            search_backend=build_search_backend(
                settings.web_search_backend, searxng_url=settings.searxng_url, client=egress_client
            ),
            pypi_upstream=settings.egress_pypi_upstream,
            npm_upstream=settings.egress_npm_upstream,
            dependency_resolver=(
                MetadataResolver(egress_client) if settings.dependency_gate_enabled else None
            ),
            dependency_allowlist=frozenset(settings.dependency_allowlist),
            on_install_decision=_install_decision,
        )
        tasks.append(
            uvicorn.Server(
                uvicorn.Config(
                    gateway,
                    host=settings.egress_gateway_host,
                    port=settings.egress_gateway_port,
                    log_level="warning",
                )
            ).serve()
        )
    logger.info("egress.start", role=role, search_backend=settings.web_search_backend)
    try:
        await asyncio.gather(*tasks)
    finally:
        if forward_server is not None:
            forward_server.close()
            with contextlib.suppress(Exception):
                await forward_server.wait_closed()
        await egress_client.aclose()
        await engine.dispose()


def main() -> None:  # pragma: no cover — process entrypoint
    settings = get_settings()
    configure_logging(settings.log_level, service="titan-egress")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
