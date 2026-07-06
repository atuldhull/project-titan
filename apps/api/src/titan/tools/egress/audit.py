"""Egress audit — a denied egress attempt becomes a SANDBOX_VIOLATION row in the
hash-chained ``tool_audit`` ledger (Phase 23; reuses the Phase-18 taxonomy + table).

The forward proxy lives inside ``titan.tools`` so it appends via the same
``repository.append_audit`` the Tool Bus uses — the per-project advisory lock and
prev_hash linkage keep the chain race-free even though the writer is the proxy,
not the bus. A blocked host is recorded as one ``error`` row (there is no matching
``started`` row — the attempt never became a real tool call)."""

from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7
from titan.tools import repository as repo
from titan.tools.egress.tokens import TokenClaims

# The synthetic tool name egress denials are audited under (kept out of the
# real tool namespace: it is a network event, not an agent tool call).
EGRESS_TOOL = "net.egress"


async def record_egress_violation(
    engine: AsyncEngine, claims: TokenClaims, *, method: str, host: str
) -> None:
    """Append one attributed SANDBOX_VIOLATION audit row for a blocked host."""
    args = {"method": method, "host": host}
    agent_id = claims.agent_id or "sandbox"
    async with engine.begin() as conn:
        await repo.append_audit(
            conn,
            tool_call_id=f"egr_{uuid7().hex}",
            trace_id=uuid7().hex,
            project_id=claims.project_id,
            task_id=claims.task_id,
            agent_id=agent_id,
            tool=EGRESS_TOOL,
            args_hash=repo.canonical_hash(args),
            args_redacted=args,
            status="error",
            initiator_chain=[agent_id],
            error_code="SANDBOX_VIOLATION",
        )


__all__ = ["EGRESS_TOOL", "record_egress_violation"]
