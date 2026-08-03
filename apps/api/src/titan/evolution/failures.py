"""Failure classification + Root-Cause Records (Vol 10 §5, TITAN-EVO-011/012/013).

The Memory Agent classifies a failure into the fixed F-* taxonomy
(evolution/taxonomy/failures.yaml) via an EP-gated registry prompt, then opens (or,
on recurrence, reopens) a :class:`RootCauseRecord`. A companion ``memory_records``
row (subtype='rcr') makes every RCR searchable in the Memory Explorer + retrievable
through the Vol-5 packet pipeline — no second memory pipeline, no Explorer change.
"""

import json
from pathlib import Path
from typing import Any

import structlog
import yaml

from titan.evolution import repository as repo
from titan.evolution.prompting import fence_untrusted
from titan.evolution.resolver import EvolutionResolver
from titan.llm.models import ChatRequest, Message, RequestMetadata
from titan.observability.metrics import EVOLUTION_OPS

logger = structlog.get_logger("titan.evolution.failures")


def load_taxonomy(repo_root: Path) -> list[str]:
    """The closed set of F-* codes from the pinned taxonomy."""
    raw = yaml.safe_load(
        (repo_root / "evolution" / "taxonomy" / "failures.yaml").read_text("utf-8")
    )
    return [str(c["code"]) for c in raw.get("codes", [])]


def parse_classification(text_value: str | None, valid_codes: list[str]) -> dict[str, Any]:
    """Parse the classifier JSON; fall back to F-UNK on any failure or an
    out-of-taxonomy code (the taxonomy is a closed set)."""
    fallback = {"primary": "F-UNK", "secondaries": [], "confidence": 0.0}
    if not text_value:
        return fallback
    try:
        start, end = text_value.index("{"), text_value.rindex("}") + 1
        data = json.loads(text_value[start:end])
    except (ValueError, json.JSONDecodeError):
        return fallback
    primary = str(data.get("primary", "")).strip()
    if primary not in valid_codes:
        return fallback
    secondaries = [c for c in data.get("secondaries", []) if c in valid_codes and c != primary]
    conf = data.get("confidence", 0.0)
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = 0.0
    return {"primary": primary, "secondaries": secondaries, "confidence": conf}


async def classify_failure(
    gateway: Any, resolver: EvolutionResolver, description: str
) -> dict[str, Any]:
    """Classify a failure into the F-* taxonomy via the Memory Agent. Never raises —
    a failure yields F-UNK (fail-safe), so classification never blocks the caller."""
    valid = load_taxonomy(resolver.repo_root)
    try:
        system_text, prompt_sha = resolver.resolve_standalone("failure_classifier")
        req = ChatRequest(
            messages=[
                Message(role="system", content=system_text),
                Message(role="user", content=f"FAILURE:\n{fence_untrusted(description)}"),
            ],
            max_tokens=256,
            metadata=RequestMetadata(
                agent_id="agent.memory", task_id=None, project_id=None, prompt_sha=prompt_sha
            ),
        )
        resp = await gateway.chat(req)
        result = parse_classification(resp.text, valid)
        EVOLUTION_OPS.labels("classify", "ok").inc()
        return result
    except Exception:  # noqa: BLE001 — classification must never block failure handling
        logger.warning("failure.classify_failed", exc_info=True)
        EVOLUTION_OPS.labels("classify", "failed").inc()
        return {"primary": "F-UNK", "secondaries": [], "confidence": 0.0}


async def _write_rcr_memory(engine: Any, rcr: dict[str, Any]) -> None:
    """Companion memory_records row (subtype='rcr') so the RCR is searchable in the
    Memory Explorer + retrievable via the packet pipeline. Org scope via a SYSTEM
    actor (a non-'agent.' principal bypasses the TITAN-BRN-024 org-scope guard)."""
    from titan.memory import service as memory

    body = (
        f"# RCR {rcr['failure_code']} ({rcr['severity']})\n\n{rcr['summary']}\n\n"
        f"Five whys: {json.dumps(rcr['five_whys'])}\n\n"
        f"Remediation: {json.dumps(rcr['remediation'])}"
    )
    batch = memory.MemoryEventBatch(
        source_agent="agent.memory",
        task_id=None,
        events=[
            memory.MemoryEvent(
                kind="semantic",
                scope="org",
                title=f"RCR: {rcr['summary'][:120]}",
                body_md=body,
                metadata={
                    "record_subtype": "rcr",
                    "rcr_id": rcr["id"],
                    "failure_code": rcr["failure_code"],
                    "severity": rcr["severity"],
                    "signature": rcr["signature"],
                },
                importance=0.8,
            )
        ],
    )
    async with engine.begin() as conn:
        await memory.write_events(
            conn, batch, source_agent="system.evolution", actor_agent="system.evolution"
        )


async def open_or_reopen_rcr(
    engine: Any,
    *,
    failure_code: str,
    severity: str,
    signature: str,
    summary: str,
    task_ids: list[Any] | None = None,
    five_whys: list[Any] | None = None,
    contributing: dict[str, Any] | None = None,
    remediation: dict[str, Any] | None = None,
    ep: str = "",
) -> dict[str, Any] | None:
    """Open an RCR, or REOPEN a remediated one on recurrence of the same signature
    (EVO-013). Returns the RCR dict (new), {'reopened': id} (a remediated RCR whose EP
    proved ineffective), {'accepted_risk': id} (a recurrence of a deliberately accepted
    risk — recorded, not reopened), or None if an open RCR already exists. Writes the
    companion memory record on a new RCR."""
    task_ids = task_ids or []
    first_task = task_ids[0] if task_ids else None
    # Recurrence of a REMEDIATED signature reopens it (the EP was ineffective).
    reopened = await repo.reopen_rcr_by_signature(engine, signature, add_task_id=first_task)
    if reopened is not None:
        logger.warning("rcr.reopened_on_recurrence", signature=signature, rcr_id=reopened)
        EVOLUTION_OPS.labels("rcr", "ok").inc()
        return {"reopened": reopened}
    # Recurrence of an ACCEPTED-RISK signature is expected — record the task, no churn.
    accepted = await repo.touch_accepted_risk_rcr(engine, signature, add_task_id=first_task)
    if accepted is not None:
        EVOLUTION_OPS.labels("rcr", "skipped").inc()
        return {"accepted_risk": accepted}
    row = await repo.insert_rcr(
        engine,
        failure_code=failure_code,
        severity=severity,
        task_ids=task_ids,
        signature=signature,
        summary=summary,
        five_whys=five_whys or [],
        contributing=contributing or {},
        remediation=remediation or {},
        ep=ep,
    )
    if row is None:
        EVOLUTION_OPS.labels("rcr", "skipped").inc()
        return None  # an open RCR for this signature already exists
    try:
        await _write_rcr_memory(engine, row)
    except Exception:  # noqa: BLE001 — Explorer companion is best-effort
        logger.warning("rcr.memory_write_failed", rcr_id=row["id"], exc_info=True)
    EVOLUTION_OPS.labels("rcr", "ok").inc()
    return row


async def annotate_merged_ep(engine: Any, ep: str) -> int:
    """A merged EP annotates the RCRs it remediates (EVO-013). Returns the count."""
    return len(await repo.annotate_rcrs_for_merged_ep(engine, ep))


__all__ = [
    "annotate_merged_ep",
    "classify_failure",
    "load_taxonomy",
    "open_or_reopen_rcr",
    "parse_classification",
]
