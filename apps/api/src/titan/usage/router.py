"""LLM usage/cost REST endpoint (Vol 4 §6.10, TITAN-FRT-033). Private to the module.

**Why this is readable by every role.** `usage:read` is deliberately NOT carved
into `SENSITIVE_RESOURCES`, so the `*:read` wildcard that viewer, operator,
maintainer and admin all carry does reach it. That is the opposite of the call
made for `logs:read` in Phase 62, and the difference is what the data is: this
returns aggregate cost and token counts per provider/model/agent, and never a
prompt, a completion, a secret or an audit row. Vol 8 §4.2 carves out "the audit
log and secret plaintext"; a spend total is neither. §6.10 gates only the alert
Ack at admin+, and gates the screen itself at nothing.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.usage import service
from titan.usage.service import Bucket, GroupBy, UsageSummary

router = APIRouter(prefix="/api/v1", tags=["usage"])

#: Org-scoped: the platform view has no project, so a missing permission is a
#: plain 403 and never a 404 — there is no project whose existence could leak.
_ReadUsage = Annotated[Principal, Depends(Requires("usage:read"))]


@router.get("/usage", response_model=UsageSummary)
async def get_usage(
    request: Request,
    principal: _ReadUsage,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    group_by: Annotated[str, Query(pattern="^(provider|model|agent|project|alias)$")] = "provider",
    bucket: Annotated[str | None, Query(pattern="^(minute|hour|day)$")] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> UsageSummary:
    """Aggregate LLM cost and tokens over a range (TITAN-FRT-033).

    Backs Monitoring's spend tile, its cost-by-provider/agent chart and its
    time series. Defaults to the last 24 hours grouped by provider.

    A note on what is in here, because an empty chart is otherwise alarming: the
    ledger only has rows for calls that were actually made, and on a default
    stack the live executor and CEO intake are both off (`live_executor` and
    `ceo_intake_enabled` default to False), so the only writer is memory-ingest
    embeddings — with no project and no agent. A quiet ledger is the honest
    answer, not a bug.

    Grouping by `project` returns raw uuids: `llm_usage.project_id` has no FK
    and grouping by `agent` returns the gateway's routing key (`agent.ceo`),
    which is not an agents-table id either. Neither resolves to a name here.
    """
    try:
        resolved_start, resolved_end = service.resolve_range(start, end, datetime.now(tz=UTC))
    except ValueError as exc:
        raise ProblemException(
            status=422,
            title="Invalid range",
            detail=str(exc),
            problem_type=problem_type("invalid-range"),
        ) from exc

    async with request.app.state.engine.connect() as conn:
        return await service.summarize(
            conn,
            start=resolved_start,
            end=resolved_end,
            # Narrowed by the Query pattern above, which is the real validator.
            group_by=group_by,  # type: ignore[arg-type]
            project_id=project_id,
            bucket=bucket,  # type: ignore[arg-type]
        )


def mount(app: FastAPI) -> None:
    app.include_router(router)


__all__ = ["Bucket", "GroupBy", "mount", "router"]
