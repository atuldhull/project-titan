"""The shared audit client — public surface of ``titan.audit`` (Vol 8 §10).

ONE writer for the immutable ``audit_events`` ledger: every subsystem records
through :class:`AuditClient` (no hand-rolled INSERTs), so the SEC-090 schema, the
SEC-092 redaction, the OTel trace_id link, and the SEC-091 hash chain are applied
in exactly one place. Leaves that cannot import ``titan.audit`` (the
module-independence contract) are wired at the composition root via a callback
(the same pattern the rate-limit/WS audit hooks use).

``record`` is deliberately NOT best-effort here — a caller that wants an audit
write to never fail the served action wraps it (like the composition-root hooks);
a caller that wants the audit atomic with its action passes its ``conn``.
"""

from __future__ import annotations

import datetime
import enum
import uuid
from collections.abc import Callable
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.audit import repository as repo
from titan.audit.repository import AppendedEvent
from titan.db import uuid7
from titan.redaction import runtime as redaction_runtime
from titan.redaction.engine import RedactionEngine, redact_structure

# --- the §10.2 event catalog ----------------------------------------------
# The canonical set of audit actions. The catalog acceptance test (SEC-090)
# iterates these and asserts each produces a schema-valid, chained row.


class AuditAction(enum.StrEnum):
    AUTH_LOGIN_SUCCEEDED = "auth.login.succeeded"
    AUTH_LOGIN_FAILED = "auth.login.failed"
    AUTH_MFA = "auth.mfa"
    AUTH_REFRESH = "auth.refresh"
    AUTH_REVOCATION = "auth.revocation"
    AUTH_REUSE_DETECTED = "auth.reuse_detected"
    AUTHZ_DENIED = "authz.denied"
    ROLE_MEMBER_CHANGED = "role.member_changed"
    SECRET_LIFECYCLE = "secret.lifecycle"
    TASK_LIFECYCLE = "task.lifecycle"
    TOOL_CALL = "tool.call"
    APPROVAL_MINT = "approval.mint"
    APPROVAL_CONSUME = "approval.consume"
    SANDBOX_CREATE = "sandbox.create"
    SANDBOX_DESTROY = "sandbox.destroy"
    SANDBOX_POLICY_EXCEPTION = "sandbox.policy_exception"
    DEPLOY = "deploy"
    PLUGIN_INSTALL = "plugin.install"
    PLUGIN_CONFIG = "plugin.config"
    MEMORY_PROMOTION = "memory.promotion"
    DATA_EXPORT = "data.export"
    DATA_DELETION = "data.deletion"
    SCAN_RESULT = "scan.result"
    REDACTION_HIT = "redaction.hit"
    RATELIMIT_BLOCK = "ratelimit.block"
    SSRF_BLOCK = "ssrf.block"
    INCIDENT_TOGGLE = "incident.toggle"
    # Vol 10 §2, Phase 83: a human approved an Evolution Proposal artifact
    # (prompt/routing/agent-spec change). Records actor, artifact hash, diff link
    # (TITAN-EVO-001c).
    EVOLUTION_APPROVE = "evolution.approve"

    # Vol 10 §5, Phase 85: a human mutated a Root-Cause Record — linked it to a
    # remediating EP or overrode its classification/severity/status (EVO-011/013).
    # A merged EP auto-remediates linked RCRs, so this is a trust-relevant curation
    # act and must leave a trail, exactly like an approval.
    EVOLUTION_RCR_UPDATE = "evolution.rcr_update"


class Outcome(enum.StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class AuditEventOut(BaseModel):
    id: uuid.UUID
    seq: int
    ts: datetime.datetime
    actor: str
    org: uuid.UUID | None
    project_id: uuid.UUID | None
    task_id: uuid.UUID | None
    action: str
    resource: str
    outcome: str
    source_ip: str | None
    trace_id: str | None
    detail: dict[str, Any]
    event_hash: str


def current_trace_id() -> str | None:
    """The active OTel trace_id (032x hex), or None outside a span — links an
    audit row to its distributed trace (SEC-090)."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


Clock = Callable[[], datetime.datetime]


class AuditClient:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        redactor: RedactionEngine | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._redactor = redactor or redaction_runtime.default_engine()
        self._clock = clock or (lambda: datetime.datetime.now(datetime.UTC))

    async def record(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        org: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        source_ip: str | None = None,
        trace_id: str | None = None,
        detail: dict[str, Any] | None = None,
        conn: AsyncConnection | None = None,
    ) -> AppendedEvent:
        """Append one SEC-090 audit event. ``detail`` is redacted (SEC-092) so no
        secret value/prompt/code body is ever stored; pass references/digests.
        Pass ``conn`` to make the audit atomic with the caller's action."""
        # SEC-092: redact the whole detail structure — references/digests only.
        safe_detail: dict[str, Any] = redact_structure(
            detail or {}, engine=self._redactor, sink="audit"
        )
        resolved_trace = trace_id if trace_id is not None else current_trace_id()
        kwargs: dict[str, Any] = dict(
            event_id=uuid7(),
            ts=self._clock(),
            actor=actor,
            org=org,
            project_id=project_id,
            task_id=task_id,
            action=str(action),
            resource=resource,
            outcome=str(outcome),
            source_ip=source_ip,
            trace_id=resolved_trace,
            detail=safe_detail,
        )
        if conn is not None:
            return await repo.append_event(conn, **kwargs)
        async with self._engine.begin() as own:
            return await repo.append_event(own, **kwargs)


# --- chain verification (SEC-091) -----------------------------------------


async def verify_chain(engine: AsyncEngine) -> int | None:
    """Walk the whole audit chain; return the seq of the first broken row, else
    None. The daily verification job alerts when this is not None."""
    async with engine.connect() as conn:
        return await repo.verify_chain(conn)


# --- sealing + retention (SEC-093) ----------------------------------------

# Injected at the composition root so titan.audit needs neither the vault (age)
# nor an object-storage client: encrypt(plaintext)->ciphertext, archive(ref,bytes).
Sealer = Callable[[bytes], bytes]
Archiver = Callable[[str, bytes], Any]


class SealReport(BaseModel):
    days_sealed: int
    events_sealed: int


async def seal_days(
    engine: AsyncEngine, *, encrypt: Sealer, archive: Archiver, now: datetime.datetime
) -> SealReport:
    """Seal every un-sealed past UTC day to an age-encrypted JSONL segment in
    object storage and record it in ``audit_seals`` (never the current day)."""
    import hashlib

    today = now.astimezone(datetime.UTC).date()
    async with engine.connect() as conn:
        days = await repo.unsealed_days_before(conn, today)
    events_sealed = 0
    for day in days:
        async with engine.connect() as conn:
            rows = await repo.events_for_day(conn, day)
        if not rows:
            continue
        # Canonical JSONL of the day's events (the event_hash makes each tamper-evident).
        lines = [repo.canonical_json(repo._row_content(r)) for r in rows]
        plaintext = ("\n".join(lines) + "\n").encode("utf-8")
        digest = hashlib.sha256(plaintext).hexdigest()
        ref = f"audit/{day.isoformat()}.jsonl.age"
        archive(ref, encrypt(plaintext))
        async with engine.begin() as conn:
            await repo.record_seal(
                conn,
                seal_id=uuid7(),
                day=day,
                segment_ref=ref,
                event_count=len(rows),
                max_seq=int(rows[-1].seq),
                digest=digest,
                # rows are seq-ordered, so rows[-1] is the max_seq row — the chain
                # tip verify_chain anchors the next online row to.
                tip_hash=rows[-1].event_hash,
            )
        events_sealed += len(rows)
    return SealReport(days_sealed=len(days), events_sealed=events_sealed)


async def apply_retention(
    engine: AsyncEngine, *, now: datetime.datetime, online_days: int = 400
) -> int:
    """Delete online audit rows older than ``online_days`` that have been sealed —
    the SEC-093 archival boundary. Returns the number removed. Sealed segments
    remain in the archive (2-year retention lives there)."""
    cutoff = now.astimezone(datetime.UTC) - datetime.timedelta(days=online_days)
    async with engine.begin() as conn:
        return await repo.delete_sealed_before(conn, cutoff)


def build_audit_client(engine: AsyncEngine) -> AuditClient:
    return AuditClient(engine)


__all__ = [
    "AuditAction",
    "AuditClient",
    "AuditEventOut",
    "Archiver",
    "Outcome",
    "SealReport",
    "Sealer",
    "apply_retention",
    "build_audit_client",
    "current_trace_id",
    "seal_days",
    "verify_chain",
]
