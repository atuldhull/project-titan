"""Bind the ids that make a log line findable (Vol 9 §6.3, Phase 62).

Vol 9 §6.3 says every line carries ``project_id``, ``agent_role`` and
``task_id``, and :mod:`titan.observability.logging`'s own docstring promised
they would be "bound by later phases". They were not: until Phase 62 the only
``bind_contextvars`` call in the whole backend was the gateway middleware's
``request_id``, so those fields appeared on the ~13% of log calls that passed
them by hand and nowhere else.

That is fine while nothing queries the logs. It stops being fine the moment
there is a log VIEWER with a project filter: ``| json | project_id="x"`` drops
every line that never had the field, so an unbound field does not make the
filter empty — it makes it *lie*, silently hiding the lines you came for.

So this module exists to bind those ids once, at the two seams where they are
actually known:

* **the request** — :class:`titan.gateway.rbac.Requires` and
  ``authorize_in_project`` have already resolved and authorized a project by
  the time they return, and the middleware clears contextvars per request, so
  binding there is scoped correctly and needs no unbind.
* **the agent turn** — :class:`titan.runtime.worker.RuntimeWorker` owns a
  project and an agent for the whole turn, but it is a long-lived loop rather
  than a request, so it must bind and *restore* (see :func:`turn_context`).

``agent_role`` is deliberately not among them: it does not exist anywhere in
this codebase. The roster identity an agent actually carries is ``agent_id``
(Vol 9 §6.3's field list is aspirational on that point, as on `attrs` and
`message`).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import structlog


def bind_log_project(project_id: uuid.UUID) -> None:
    """Tag every subsequent log line in this request with its project.

    Request-scoped: :class:`titan.gateway.middleware.RequestContextMiddleware`
    calls ``clear_contextvars()`` at the start of each request, so this needs
    no matching unbind and cannot leak into the next request on the same task.
    """
    structlog.contextvars.bind_contextvars(project_id=str(project_id))


@contextmanager
def turn_context(
    project_id: uuid.UUID, agent_id: str, task_id: uuid.UUID | None = None
) -> Iterator[None]:
    """Tag every log line emitted during one agent turn.

    Unlike the request path there is no per-iteration ``clear_contextvars`` in
    the worker: it is one long-lived asyncio task processing delivery after
    delivery. A bare ``bind_contextvars`` here would leak the first task's ids
    onto every later turn — including turns for other projects — which is worse
    than not binding at all, because the log viewer would confidently attribute
    them to the wrong project. ``bound_contextvars`` restores the previous
    values on exit, so each turn sees only its own.
    """
    values: dict[str, str] = {"project_id": str(project_id), "agent_id": agent_id}
    if task_id is not None:
        values["task_id"] = str(task_id)
    with structlog.contextvars.bound_contextvars(**values):
        yield
