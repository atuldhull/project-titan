"""The structured message protocol (Vol 2 §6, TITAN-AGT-015).

Round-trips each of the eight normative types against its §6.2 example shape, and
proves the envelope is strict: a ninth type, an extra payload field, or a missing
required field all raise (and the caller logs) — the ADR-AGT-04 guarantee that
agent chatter is machine-checkable. Pure unit tests: no DB, no Redis.
"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from titan.messaging.protocol import (
    KIND_TO_TYPE,
    TYPE_TO_KIND,
    AgentMessage,
    parse_agent_message,
    validate_payload,
)

_PID = uuid.uuid4()


def _envelope(message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": message_type,
        "project_id": str(_PID),
        "from_agent": "pm_project",
        "to_agent": "eng_backend",
        "payload": payload,
    }


# The §6.2 example payloads, with real UUIDs where the spec writes "uuid".
EXAMPLES: dict[str, dict[str, Any]] = {
    "TaskAssignment": {
        "title": "Implement POST /api/v1/orders",
        "description": "Create order endpoint per contract C-114.",
        "acceptance_criteria": ["201 + body per contract", "401 unauthenticated"],
        "story_id": str(uuid.uuid4()),
        "depends_on": [str(uuid.uuid4())],
        "estimate_points": 3,
        "deadline": "2026-07-05T12:00:00Z",
    },
    "ProgressReport": {
        "status": "in_progress",
        "percent_complete": 60,
        "summary": "Endpoint done; writing tests.",
        "blockers": [{"description": "Contract C-114 missing error codes", "needs": "architect"}],
        "artifacts": ["branch:feat/orders-endpoint"],
    },
    "ReviewRequest": {
        "artifact_type": "pull_request",
        "artifact_ref": "pr:42",
        "context_task_id": str(uuid.uuid4()),
        "checklist": ["correctness", "tests", "standards"],
        "verdict": None,
    },
    "ArchitectureDiscussion": {
        "thread_id": str(uuid.uuid4()),
        "question": "Sync or async order-payment integration?",
        "position": "Async via Redis Streams outbox.",
        "arguments": ["decouples payment latency", "replayable"],
        "responding_to": None,
        "decision_deadline": "2026-07-05T09:00:00Z",
    },
    "BugReport": {
        "severity": "high",
        "security": False,
        "summary": "Order total ignores discounts",
        "reproduction_steps": ["POST /orders with coupon X", "observe total"],
        "expected": "total = 90.00",
        "actual": "total = 100.00",
        "suspected_component": "pricing service",
        "evidence": ["test:test_discount_applied FAILED"],
    },
    "DecisionLog": {
        "decision": "Use cursor pagination for order listing",
        "context": "Offset pagination degrades past 100k rows.",
        "alternatives": [{"option": "offset", "rejected_because": "O(n) skip cost"}],
        "trade_offs": "No random page access.",
        "authority": "decides_alone",
        "reversible": True,
    },
    "MeetingSummary": {
        "ceremony": "sprint_planning",
        "participants": ["ceo", "pm_product", "architect"],
        "decisions": [str(uuid.uuid4())],
        "action_items": [{"owner": "pm_project", "action": "cut story S-9", "due": "2026-07-05"}],
        "summary": "Sprint 3 scoped to checkout flow.",
    },
    "ConflictResolution": {
        "conflict_id": str(uuid.uuid4()),
        "parties": ["eng_backend", "qa"],
        "subject": "Is BUG-77 release-blocking?",
        "positions": [
            {"agent": "qa", "position": "blocking", "evidence": ["fails AC-3 of story S-4"]}
        ],
        "round": 1,
        "arbiter": None,
        "resolution": None,
    },
}


def test_examples_cover_all_eight_types() -> None:
    assert set(EXAMPLES) == set(TYPE_TO_KIND)


@pytest.mark.parametrize("message_type", sorted(EXAMPLES))
def test_round_trip_serialization(message_type: str) -> None:
    """Build → dump → re-validate reproduces the envelope + payload exactly."""
    msg = AgentMessage.model_validate(_envelope(message_type, EXAMPLES[message_type]))
    wire = msg.model_dump(mode="json")
    again = AgentMessage.model_validate(wire)
    assert again.model_dump(mode="json") == wire
    assert again.type == message_type
    assert again.kind == TYPE_TO_KIND[message_type]
    # The payload validates against its strong model and preserves fields.
    typed = msg.typed_payload()
    assert typed.model_dump(mode="json", exclude_none=False)


def test_type_kind_maps_are_inverse() -> None:
    assert {KIND_TO_TYPE[v]: v for v in KIND_TO_TYPE} == TYPE_TO_KIND
    assert len(TYPE_TO_KIND) == 8


def test_ninth_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentMessage.model_validate(_envelope("Gossip", {}))


def test_unknown_payload_field_is_rejected() -> None:
    payload = {**EXAMPLES["BugReport"], "surprise": 1}
    with pytest.raises(ValidationError):
        AgentMessage.model_validate(_envelope("BugReport", payload))


def test_missing_required_payload_field_is_rejected() -> None:
    payload = dict(EXAMPLES["ProgressReport"])
    del payload["status"]
    with pytest.raises(ValidationError):
        AgentMessage.model_validate(_envelope("ProgressReport", payload))


def test_unknown_envelope_field_is_rejected() -> None:
    env = _envelope("BugReport", EXAMPLES["BugReport"])
    env["spoofed"] = True
    with pytest.raises(ValidationError):
        AgentMessage.model_validate(env)


def test_bad_priority_rejected() -> None:
    env = _envelope("BugReport", EXAMPLES["BugReport"])
    env["priority"] = "urgent"  # not one of critical|high|normal|low
    with pytest.raises(ValidationError):
        AgentMessage.model_validate(env)


def test_review_verdict_reply_shape() -> None:
    """A gatekeeper reply fills verdict and reuses the correlation_id (§6.2)."""
    corr = uuid.uuid4()
    request = AgentMessage.model_validate(
        {**_envelope("ReviewRequest", EXAMPLES["ReviewRequest"]), "correlation_id": str(corr)}
    )
    reply_payload = {
        **EXAMPLES["ReviewRequest"],
        "verdict": {
            "decision": "veto",
            "comments": [
                {
                    "location": "src/orders.py:88",
                    "severity": "major",
                    "comment": "SQL injection",
                    "standard_ref": "VOL8-ERR-004",
                }
            ],
        },
    }
    reply = AgentMessage.model_validate(
        {
            **_envelope("ReviewRequest", reply_payload),
            "correlation_id": str(corr),
            "from_agent": "security",
            "to_agent": "eng_backend",
        }
    )
    assert reply.correlation_id == request.correlation_id
    assert reply.typed_payload().model_dump()["verdict"]["decision"] == "veto"


def test_validate_payload_helper_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        validate_payload("NotAType", {})


def test_created_at_is_stamped_by_default() -> None:
    """The envelope always carries a creation time (§6.1) — even when the caller
    omits it — so the copy delivered over Redis is never null."""
    msg = AgentMessage.model_validate(_envelope("BugReport", EXAMPLES["BugReport"]))
    assert msg.created_at is not None
    assert msg.model_dump(mode="json")["created_at"] is not None


def test_bug_report_requires_reproduction_steps() -> None:
    """§6.2: BugReport has mandatory reproduction."""
    payload = {**EXAMPLES["BugReport"], "reproduction_steps": []}
    with pytest.raises(ValidationError):
        AgentMessage.model_validate(_envelope("BugReport", payload))


def test_parse_agent_message_logs_rejection_without_leaking_content() -> None:
    """parse_agent_message LOGS a strict rejection (TITAN-AGT-015 'and is
    logged') but records only field locations/types — never the offending values
    (TITAN-OPS-016: no message content in logs)."""
    secret = "SENSITIVE-BUG-DETAIL-should-not-be-logged"
    bad = _envelope(
        "BugReport", {**EXAMPLES["BugReport"], "summary": secret, "leaked_field": secret}
    )
    with capture_logs() as logs, pytest.raises(ValidationError):
        parse_agent_message(bad)
    rejections = [entry for entry in logs if entry.get("event") == "agent_message.rejected"]
    assert len(rejections) == 1
    assert rejections[0]["message_type"] == "BugReport"
    # The whole log entry must not contain the offending payload value anywhere.
    assert secret not in repr(rejections[0])


def test_parse_agent_message_returns_valid_message() -> None:
    with capture_logs() as logs:
        msg = parse_agent_message(_envelope("BugReport", EXAMPLES["BugReport"]))
    assert msg.type == "BugReport"
    assert not [e for e in logs if e.get("event") == "agent_message.rejected"]
