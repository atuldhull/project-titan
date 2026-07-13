"""Task-branch naming for agent git writes (TITAN-SEC-031, Vol 6 §6.2).

SEC-031 confines agent git writes to *task branches* — its example is
``titan/task-*``. Vol 6 §6.2 names the generated project's own convention:
``type/task-id-slug`` (e.g. ``feat/TSK-142-invoice-pdf``) with Conventional-Commit
types. Both forms are accepted as task branches here. What is ALWAYS rejected —
so the SEC-031 guarantee (an agent write never lands on a protected branch, and a
branch argument can never smuggle a second ref) is preserved — is:

* protected branches (``main``/``master``/``develop``/``release``/…), and
* any ref-smuggling: a refspec (``a:b``), a force (``+``), a ref expression
  (``^`` ``~`` ``@{``), or whitespace.
"""

import re

# Conventional-Commit types (Vol 6 §6.2 ``type/...``).
_CONVENTIONAL_TYPES = (
    "feat",
    "fix",
    "chore",
    "docs",
    "refactor",
    "test",
    "ci",
    "build",
    "perf",
    "style",
    "revert",
)
_TITAN_TASK_PREFIX = "titan/task-"
# ``type/slug`` — the slug is a non-empty run of branch-safe characters.
_CONVENTIONAL_RE = re.compile(
    r"^(?:" + "|".join(_CONVENTIONAL_TYPES) + r")/[A-Za-z0-9][A-Za-z0-9._/-]*$"
)
# Characters that could redirect a write off the branch: refspec ``:``, force
# ``+``, ref expressions ``^ ~ @{``, and whitespace.
_UNSAFE_CHARS = frozenset(":+^~ \t\n")
# Protected branches an agent may never write to. The accept patterns already
# exclude these, but naming them makes the SEC-031 intent explicit and closes a
# ``titan/task-`` prefix that happened to equal a protected name.
_PROTECTED = frozenset(
    {"main", "master", "develop", "trunk", "production", "prod", "release", "hotfix", "head"}
)


def is_task_branch(name: str) -> bool:
    """True when ``name`` is a writable task branch — either the platform
    ``titan/task-*`` form or a Vol 6 §6.2 Conventional ``type/task-id-slug``
    branch — and never a protected branch or a ref-smuggling expression."""
    if not name or "@{" in name or any(ch in _UNSAFE_CHARS for ch in name):
        return False
    if name.lower() in _PROTECTED:
        return False
    if name.startswith(_TITAN_TASK_PREFIX):
        return True
    return bool(_CONVENTIONAL_RE.match(name))


__all__ = ["is_task_branch"]
