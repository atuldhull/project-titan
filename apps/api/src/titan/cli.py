"""The `titan` operator CLI.

Phase 35 ships the first subcommand — ``titan memory rebuild-index``
(TITAN-BRN-004): fully reconstruct every Qdrant collection from PostgreSQL.
Later phases hang their admin commands off the same entrypoint.

Wiring mirrors the API process: engine + Qdrant HTTP client from
:mod:`titan.clients`, embeddings through the provider-agnostic LLM gateway
(TITAN-BRN-006) — the CLI knows no provider SDK either.
"""

import argparse
import asyncio
import json
import sys

from titan import clients
from titan.llm.service import build_gateway
from titan.memory import service as memory
from titan.observability.logging import configure_logging
from titan.settings import get_settings


async def _rebuild_index() -> int:
    settings = get_settings()
    engine = clients.create_engine(settings)
    qdrant_http = clients.create_http_client(settings)
    gateway = build_gateway(settings, engine=engine)
    try:
        report = await memory.rebuild_index(engine, memory.QdrantIndex(qdrant_http), gateway)
    finally:
        await gateway.aclose()
        await qdrant_http.aclose()
        await engine.dispose()
    print(json.dumps(report.model_dump(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="titan", description="TITAN operator CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    memory_cmd = subcommands.add_parser("memory", help="Memory Service maintenance (Vol 5)")
    memory_sub = memory_cmd.add_subparsers(dest="memory_command", required=True)
    memory_sub.add_parser(
        "rebuild-index",
        help="Reconstruct all Qdrant collections from PostgreSQL (TITAN-BRN-004)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings().log_level, service="titan-cli")
    if args.command == "memory" and args.memory_command == "rebuild-index":
        return asyncio.run(_rebuild_index())
    return 2  # unreachable: argparse enforces the subcommand set


if __name__ == "__main__":  # pragma: no cover — process entrypoint
    sys.exit(main())
