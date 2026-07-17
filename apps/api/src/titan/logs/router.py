"""Log history REST endpoint (Vol 4 §6.7, TITAN-FRT-030). Private to the module.

**Why this is org-scoped and admin-only.** A log line has no owning project:
only ~13% of call sites pass one, and the Loki stream labels
(``namespace``/``service``/``level``) carry no tenancy at all. So there is no
honest way to serve a project-scoped slice of the logs — `?project_id=` is a
FILTER over the body, not an authorization boundary, and a line that never
bound a project is invisible to it rather than denied by it.

That makes system logs an org-wide operator surface, and they are guarded like
one: ``logs:read`` is carved into ``SENSITIVE_RESOURCES``, so the ``*:read``
wildcard that owner/admin/maintainer/operator/viewer all carry does **not**
reach it. Without that carve-out, minting this permission would have handed
every viewer — and every agent, which runs as ``operator`` — the full system
log firehose. It is granted explicitly to owner and admin only.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request

from titan.gateway.pagination import Page, decode_cursor
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.logs import service
from titan.logs.logql import LEVELS, LogFilters, LogQLError
from titan.logs.loki import MAX_PAGE, LokiQueryError, LokiUnavailable
from titan.logs.service import LogLine

router = APIRouter(prefix="/api/v1", tags=["logs"])

#: Org-scoped: no project_param, so a missing permission is a plain 403 and
#: never a 404 — there is no project whose existence could leak.
_ReadLogs = Annotated[Principal, Depends(Requires("logs:read"))]

#: The row-list convention (`MAX_LIMIT = 200`) is deliberately not reused here.
#: A log viewer tails at ~1,000 lines/s, so a 200-line ceiling would need five
#: round trips a second. The real ceiling is `MAX_PAGE` — one below Loki's own
#: `max_entries_limit`, which it enforces by REJECTING the query rather than
#: clamping it, and which the query's +1 has-more sentinel must fit under.
DEFAULT_LIMIT = 500


@router.get("/logs", response_model=Page[LogLine])
async def list_logs(  # noqa: PLR0913 - the filter bar is the API
    request: Request,
    principal: _ReadLogs,
    service_name: Annotated[
        list[str] | None,
        Query(
            alias="service",
            description="Compose service name (api, worker, …). Repeat for several.",
        ),
    ] = None,
    level: Annotated[
        list[str] | None,
        Query(description=f"One of {', '.join(LEVELS)}. Repeat for several."),
    ] = None,
    project_id: Annotated[str | None, Query()] = None,
    task_id: Annotated[str | None, Query()] = None,
    agent_id: Annotated[str | None, Query()] = None,
    request_id: Annotated[str | None, Query()] = None,
    trace_id: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query(description="Case-sensitive substring.")] = None,
    regex: Annotated[str | None, Query(description="RE2. No lookaround/backrefs.")] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    direction: Annotated[str, Query(pattern="^(backward|forward)$")] = "backward",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[LogLine]:
    """Structured log history from Loki.

    Backs the Logs Viewer's history fill, its filter bar and its full-history
    search (client-side filtering only ever searches the buffer). ``backward``
    (the default) walks newest-first into the past; ``forward`` resumes from a
    cursor and is what a tail polls.

    A filter on a JSON-body field (``project_id``, ``task_id``, ``agent_id``)
    matches only lines whose emitter bound that field. Most do not — the
    logging context binds a project on request- and turn-scoped work, and
    everything outside that scope is invisible to the filter rather than
    excluded by it. This is LogQL working as designed, and it looks exactly
    like a bug, so it is said out loud here and in the viewer's own UI.
    """
    querier = getattr(request.app.state, "loki", None)
    if querier is None:
        # The observability stack is Compose-profile-gated, so this is the
        # normal state of a default `docker compose up`, not a failure. Say
        # which knob turns it on rather than pretending the logs are empty.
        raise ProblemException(
            status=503,
            title="Log history is unavailable",
            detail=(
                "No Loki backend is configured. Set TITAN_LOKI_URL and start the "
                "observability profile (docker compose --profile obs up) to enable "
                "log history."
            ),
            problem_type=problem_type("loki-unavailable"),
        )

    try:
        filters = LogFilters(
            services=tuple(service_name or ()),
            levels=tuple(level or ()),
            project_id=project_id,
            task_id=task_id,
            agent_id=agent_id,
            request_id=request_id,
            trace_id=trace_id,
            search=search,
            regex=regex,
        )
        window = service.resolve_window(
            start=start,
            end=end,
            direction=direction,
            limit=limit,
            position=decode_cursor(cursor) if cursor else None,
            now=datetime.now(tz=UTC),
        )
    except (LogQLError, ValueError) as exc:
        raise ProblemException(
            status=422,
            title="Invalid log filter",
            detail=str(exc),
            problem_type=problem_type("invalid-log-filter"),
        ) from exc

    try:
        return await service.query_logs(querier, filters=filters, window=window)
    except LogQLError as exc:
        # The builder validates lazily — an unknown level or an RE2-incompatible
        # regex is only found when the query is rendered, which happens INSIDE
        # query_logs. Catching it only around resolve_window above turned every
        # one of those into a 500.
        raise ProblemException(
            status=422,
            title="Invalid log filter",
            detail=str(exc),
            problem_type=problem_type("invalid-log-filter"),
        ) from exc
    except LokiQueryError as exc:
        # Loki rejected the LogQL we built. That is our bug, not the caller's,
        # but the message names the offending filter and is worth surfacing.
        raise ProblemException(
            status=422,
            title="Invalid log query",
            detail=str(exc),
            problem_type=problem_type("invalid-log-filter"),
        ) from exc
    except LokiUnavailable as exc:
        raise ProblemException(
            status=503,
            title="Log history is unavailable",
            detail=f"The Loki backend could not be queried: {exc}",
            problem_type=problem_type("loki-unavailable"),
        ) from exc


def mount(app: FastAPI) -> None:
    app.include_router(router)
