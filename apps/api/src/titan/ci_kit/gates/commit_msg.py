"""TITAN-WFL-027 — Conventional Commits, enforced at the ``commit-msg`` hook.

§6.2 fixes the commit form (``feat(billing): generate invoice PDFs``) and §6.2
also fixes the branch form (``feat/TSK-142-invoice-pdf``). The hook rejects a
bad message at commit time so the agent gets the feedback in the loop that
produced it, and CI re-checks the same rule on the PR's commits so a hook that
was never installed cannot let a bad message through.

Merge, revert and fixup commits are exempt: git generates those messages itself,
and a gate that rejects git's own output is a gate that blocks the merge queue.
"""

import argparse
import re
import sys
from pathlib import Path

from titan.ci_kit.gates.outcome import Finding, GateOutcome, failed, passed

GATE = "commit_msg"
WFL_ID = "TITAN-WFL-027"

# The Conventional Commits types. `feat` and `fix` carry semver meaning; the rest
# are the conventional set, which §6.2's example (`feat(billing): ...`) assumes.
TYPES: tuple[str, ...] = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)

MAX_SUBJECT = 72

SUBJECT = re.compile(
    r"^(?P<type>" + "|".join(TYPES) + r")"
    r"(?:\((?P<scope>[a-z0-9][a-z0-9._-]*)\))?"
    r"(?P<breaking>!)?"
    r": (?P<description>.+)$"
)

# git writes these itself; rejecting them would block merges and reverts.
_EXEMPT = re.compile(r"^(merge |revert |fixup!|squash!)", re.IGNORECASE)

FIX = (
    "use Conventional Commits: <type>(<optional-scope>): <description>, "
    f"e.g. 'feat(billing): generate invoice PDFs' (types: {', '.join(TYPES)}) — §6.2"
)


def check_message(message: str, path: str = "COMMIT_EDITMSG") -> GateOutcome:
    """Validate one commit message. Pure — this is the gate's whole logic."""
    # Comment lines are git's template scaffolding, not the author's message.
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    subject = lines[0].strip() if lines else ""

    def fail(message_text: str) -> GateOutcome:
        return failed(
            GATE,
            WFL_ID,
            (Finding(wfl_id=WFL_ID, path=path, line=1, message=message_text, fix=FIX),),
        )

    if not subject:
        return fail("empty commit message")
    if _EXEMPT.match(subject):
        return passed(GATE, WFL_ID)

    match = SUBJECT.match(subject)
    if match is None:
        return fail(f"{subject!r} is not a Conventional Commit subject")
    if len(subject) > MAX_SUBJECT:
        return fail(f"subject is {len(subject)} chars; §6.2 caps it at {MAX_SUBJECT}")

    description = match.group("description")
    if description != description.strip():
        return fail("description has leading or trailing whitespace")
    if description.endswith("."):
        return fail("description must not end with a period")

    # A blank line must separate subject from body — git and every changelog
    # generator treat line 2 as the body, so a non-blank line 2 silently
    # corrupts the parsed subject.
    if len(lines) > 1 and lines[1].strip():
        return fail("a blank line must separate the subject from the body")

    return passed(GATE, WFL_ID)


def check(commit_msg_file: Path) -> GateOutcome:
    return check_message(
        commit_msg_file.read_text(encoding="utf-8"),
        path=commit_msg_file.name,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{WFL_ID}: Conventional Commits")
    parser.add_argument(
        "commit_msg_file",
        type=Path,
        help="path to the commit message file (git passes this to the commit-msg hook)",
    )
    args = parser.parse_args(argv)
    outcome = check(args.commit_msg_file)
    print(outcome.render())
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
