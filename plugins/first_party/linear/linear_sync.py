"""The two-way-sync ALGORITHM (Vol 7 §6.4, TITAN-INT-043) — pure + fixture-testable. Identical
to the Jira connector's jira_sync (duplicated because connectors are isolated: no cross-connector
imports). TITAN task states map to a tracker's workflow states via an operator ``state_map``;
``resolve_status`` implements the §6.4 conflict rule (last-writer-wins when one side moved; a
SIMULTANEOUS transition to different states is flagged ``conflict`` for the PM agent, never
silently overwritten). The orchestration that feeds it needs a core sync engine — see README.
"""

from dataclasses import dataclass

TITAN_STATES = ("todo", "in_progress", "review", "done", "blocked")


def titan_to_tracker(titan_state: str, state_map: dict[str, str]) -> str | None:
    return state_map.get(titan_state)


def tracker_to_titan(tracker_status: str, state_map: dict[str, str]) -> str | None:
    want = str(tracker_status).strip().lower()
    for titan_state, mapped in state_map.items():
        if str(mapped).strip().lower() == want:
            return titan_state
    return None


@dataclass(frozen=True)
class Decision:
    action: str  # in_parity | apply_to_titan | apply_to_tracker | conflict
    titan_status: str | None = None
    tracker_status: str | None = None
    reason: str = ""


def resolve_status(
    *,
    titan_status: str,
    tracker_status: str,
    last_synced_titan: str,
    last_synced_tracker: str,
    state_map: dict[str, str],
) -> Decision:
    mapped_tracker = tracker_to_titan(tracker_status, state_map)
    if mapped_tracker == titan_status:
        return Decision("in_parity", titan_status=titan_status, tracker_status=tracker_status)

    titan_moved = titan_status != last_synced_titan
    tracker_moved = tracker_status != last_synced_tracker

    if titan_moved and tracker_moved:
        return Decision(
            "conflict",
            titan_status=titan_status,
            tracker_status=tracker_status,
            reason="both sides transitioned since last sync",
        )
    if tracker_moved:
        return Decision(
            "apply_to_titan", titan_status=mapped_tracker, tracker_status=tracker_status
        )
    if titan_moved:
        return Decision(
            "apply_to_tracker",
            titan_status=titan_status,
            tracker_status=titan_to_tracker(titan_status, state_map),
        )
    return Decision("in_parity", titan_status=titan_status, tracker_status=tracker_status)
