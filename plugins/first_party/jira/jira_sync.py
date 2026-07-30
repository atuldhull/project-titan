"""The two-way-sync ALGORITHM (Vol 7 §6.4, TITAN-INT-043) — pure + fixture-testable.

TITAN task states map to a tracker's workflow states via an operator-configured ``state_map``
(``{titan_state: tracker_status}``). ``resolve_status`` implements the §6.4 conflict rule:
last-writer-wins when only one side moved since the last successful sync, but a SIMULTANEOUS
transition to DIFFERENT states (both sides moved and disagree) is flagged ``conflict`` for the
PM agent — never silently overwritten.

This is the part of INT-043 an out-of-process connector CAN own. The orchestration that feeds it
— persisting the per-project mapping + last-synced marker, and reading live TITAN task state —
needs a core sync engine or extra plugin capabilities (see the connector README); the connector
has only secret/publish/fetch.
"""

from dataclasses import dataclass

TITAN_STATES = ("todo", "in_progress", "review", "done", "blocked")


def titan_to_tracker(titan_state: str, state_map: dict[str, str]) -> str | None:
    return state_map.get(titan_state)


def tracker_to_titan(tracker_status: str, state_map: dict[str, str]) -> str | None:
    """Reverse the map (case-insensitive on the tracker side, since Jira status names are
    human-entered). First TITAN state whose mapped status matches, or None if unmapped."""
    want = str(tracker_status).strip().lower()
    for titan_state, mapped in state_map.items():
        if str(mapped).strip().lower() == want:
            return titan_state
    return None


@dataclass(frozen=True)
class Decision:
    #: in_parity | apply_to_titan | apply_to_tracker | conflict
    action: str
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
    """Reconcile a status difference between TITAN and the tracker given the last-synced
    baseline (the values that were equal at the previous successful sync)."""
    mapped_tracker = tracker_to_titan(tracker_status, state_map)
    if mapped_tracker == titan_status:
        return Decision("in_parity", titan_status=titan_status, tracker_status=tracker_status)

    titan_moved = titan_status != last_synced_titan
    tracker_moved = tracker_status != last_synced_tracker

    if titan_moved and tracker_moved:
        # Both sides transitioned since the last sync, to states that don't agree → the §6.4
        # simultaneous-transition conflict. Flag it; the PM agent resolves (never overwrite).
        return Decision(
            "conflict",
            titan_status=titan_status,
            tracker_status=tracker_status,
            reason="both sides transitioned since last sync",
        )
    if tracker_moved:  # only the tracker moved → last writer is the tracker: apply to TITAN
        return Decision(
            "apply_to_titan", titan_status=mapped_tracker, tracker_status=tracker_status
        )
    if titan_moved:  # only TITAN moved → mirror it to the tracker
        return Decision(
            "apply_to_tracker",
            titan_status=titan_status,
            tracker_status=titan_to_tracker(titan_status, state_map),
        )
    # Neither moved yet they disagree: the last-synced baseline was itself inconsistent (e.g. an
    # unmapped status). Nothing to do here — surfaced as in_parity so a real move drives the next
    # decision rather than churning.
    return Decision("in_parity", titan_status=titan_status, tracker_status=tracker_status)
