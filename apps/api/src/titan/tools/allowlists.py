"""Agent-role tool allowlists — the Vol 8 §4.3 matrix as code.

The tool gateway rejects out-of-list calls regardless of what the model asks
for. Each role maps to (allow patterns, deny patterns); DENY WINS, and a role
absent from the matrix gets nothing — fail closed. Patterns are ``fnmatch``
globs over namespaced tool names. Name-level rules live here; parameter-level
rules (e.g. "git write outside test dirs" for QA, protected-branch push) are
TITAN-SEC-031 constraints on the individual tool specs.

Roles are the 14-role roster from ``titan.agents.service.AgentRole``.
"""

import fnmatch

_READ_REPO = ("fs.read", "fs.list", "fs.glob", "fs.grep", "git.log", "git.diff", "git.show")
_TASK_READ = ("task.get", "task.list")
_ALWAYS_DENY = ("secret.*", "cloud.*", "k8s.*", "deploy.*")

# role -> (allow, deny). Vol 8 §4.3 row by row.
_MATRIX: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # CEO / PM / Project Manager: task CRUD, messaging, docs read.
    # Denied: shell, git write, deploy, secrets.
    "ceo": (
        ("task.*", "message.*", "docs.read", "docs.list", "memory.search", "memory.get"),
        ("terminal.*", "git.*", "fs.write", "fs.patch", *_ALWAYS_DENY),
    ),
    # Architect / Research: repo read, web fetch, docs write. Denied: shell,
    # git push, deploy.
    "architect": (
        (*_READ_REPO, "web.*", "browser.*", "docs.*", "memory.*", *_TASK_READ, "message.*"),
        ("terminal.*", "git.commit", "git.push", *_ALWAYS_DENY),
    ),
    # Backend / Frontend / DB Engineer: sandbox shell, repo read/write on task
    # branch, test run. Denied: protected-branch push (param constraint),
    # deploy, plugins.
    "backend": (
        (
            "terminal.*",
            "fs.*",
            "git.*",
            "code.*",
            "docker.*",
            "web.*",
            "test.*",
            *_TASK_READ,
            "task.transition",
            "message.*",
        ),
        ("plugin.*", *_ALWAYS_DENY),
    ),
    # QA: sandbox shell, test run, repo read, browser E2E, web research, bug
    # report. Denied: git write outside test dirs (param constraint), deploy.
    "qa": (
        (
            "terminal.*",
            "code.*",
            "test.*",
            "browser.*",
            "web.*",
            *_READ_REPO,
            "git.commit",
            *_TASK_READ,
            "message.*",
        ),
        ("git.push", *_ALWAYS_DENY),
    ),
    # Security: scanners, repo read, report write. Denied: deploy, secret read.
    "security": (
        ("scanner.*", *_READ_REPO, "report.*", "docs.write", *_TASK_READ, "message.*"),
        ("terminal.*", *_ALWAYS_DENY),
    ),
    # Code Reviewer: repo read, PR review API. Denied: any code write, deploy.
    "reviewer": (
        (*_READ_REPO, "github.get_pr", "github.review_pr", *_TASK_READ, "message.*"),
        ("fs.write", "fs.patch", "git.commit", "git.push", "terminal.*", *_ALWAYS_DENY),
    ),
    # DevOps: IaC repo write, deploy tool (approval-gated by risk class).
    # Denied: secret plaintext read.
    "devops": (
        (
            "terminal.*",
            "fs.*",
            "git.*",
            "code.*",
            "docker.*",
            "deploy.*",
            *_TASK_READ,
            "message.*",
        ),
        ("secret.*",),
    ),
    # Memory / Documentation: memory & docs read/write in namespace. Denied:
    # shell, git, deploy.
    "docs": (
        ("docs.*", "memory.*", "fs.read", "fs.list", *_TASK_READ, "message.*"),
        ("terminal.*", "git.*", *_ALWAYS_DENY),
    ),
}
# Roster aliases sharing a §4.3 row.
_MATRIX["pm"] = _MATRIX["ceo"]
_MATRIX["project_manager"] = _MATRIX["ceo"]
_MATRIX["research"] = _MATRIX["architect"]
_MATRIX["frontend"] = _MATRIX["backend"]
_MATRIX["db"] = _MATRIX["backend"]
_MATRIX["memory"] = _MATRIX["docs"]


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def allowed_for_role(role: str, tool_name: str) -> bool:
    """True if the role's row allows the tool and no deny pattern hits.
    Unknown role → False (fail closed)."""
    row = _MATRIX.get(role)
    if row is None:
        return False
    allow, deny = row
    return _matches(tool_name, allow) and not _matches(tool_name, deny)


__all__ = ["allowed_for_role"]
