"""Public service interface of the `projects` module (TITAN-BCK-004).

Responsibility: project lifecycle and roadmap data.
Owns: projects, milestones, sprints.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7
from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.gateway.dto import RejectNullFields
from titan.gateway.pagination import Keyset, Page
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.projects import repository as repo

# --- DTOs ------------------------------------------------------------------

_SLUG = r"^[a-z0-9][a-z0-9-]*$"
Mode = Literal["autonomous", "supervised"]
MilestoneStatus = Literal["open", "closed"]
SprintStatus = Literal["planned", "active", "closed"]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, pattern=_SLUG)
    display_name: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1)
    mode: Mode = "autonomous"
    llm_profile: str = Field(default="default", min_length=1, max_length=100)


class ProjectPatch(RejectNullFields):
    # No `name` (immutable slug) and no `status` — status moves through
    # :archive (and, later, the orchestrator), never a free-form PATCH. Every
    # field maps to a NOT NULL column, so all reject explicit null (→ 422).
    model_config = ConfigDict(extra="forbid")
    non_nullable = frozenset({"display_name", "brief", "mode", "llm_profile"})

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    brief: str | None = Field(default=None, min_length=1)
    mode: Mode | None = None
    llm_profile: str | None = Field(default=None, min_length=1, max_length=100)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    display_name: str
    brief: str
    status: str
    mode: str
    llm_profile: str
    created_by: uuid.UUID
    created_at: datetime
    version: int


class MilestoneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    due_at: datetime | None = None
    status: MilestoneStatus = "open"


class MilestonePatch(RejectNullFields):
    # due_at is genuinely nullable (a caller may clear it); title/status map to
    # NOT NULL columns and reject explicit null.
    model_config = ConfigDict(extra="forbid")
    non_nullable = frozenset({"title", "status"})

    title: str | None = Field(default=None, min_length=1, max_length=300)
    due_at: datetime | None = None
    status: MilestoneStatus | None = None


class MilestoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    due_at: datetime | None
    status: str
    created_at: datetime
    version: int


class SprintCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    goal: str | None = Field(default=None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: SprintStatus = "planned"


class SprintPatch(RejectNullFields):
    # goal/starts_at/ends_at are nullable (clearable); status is NOT NULL.
    model_config = ConfigDict(extra="forbid")
    non_nullable = frozenset({"status"})

    goal: str | None = Field(default=None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: SprintStatus | None = None


class SprintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    number: int
    goal: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    status: str
    created_at: datetime
    version: int


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409,
        title="Conflict",
        detail=detail,
        problem_type=problem_type("conflict"),
    )


async def _emit_sprint_updated(conn: AsyncConnection, sprint: SprintOut, actor: str) -> None:
    # §5 WebSocket taxonomy: planning changes surface as `sprint.updated`.
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {"type": "sprint.updated", "sprint": sprint.model_dump(mode="json")},
        actor=actor,
    )


# --- projects --------------------------------------------------------------


async def create_project(
    conn: AsyncConnection, data: ProjectCreate, created_by: uuid.UUID
) -> ProjectOut:
    """Create a Project from a free-text brief (TITAN-FND-001)."""
    try:
        row = await repo.insert_project(
            conn,
            project_id=uuid7(),
            name=data.name,
            display_name=data.display_name,
            brief=data.brief,
            mode=data.mode,
            llm_profile=data.llm_profile,
            created_by=created_by,
        )
    except IntegrityError as exc:
        raise _conflict(f"A project named '{data.name}' already exists.") from exc
    return ProjectOut.model_validate(row)


async def get_project(conn: AsyncConnection, project_id: uuid.UUID) -> ProjectOut:
    row = await repo.get_project(conn, project_id)
    if row is None:
        raise not_found("project")
    return ProjectOut.model_validate(row)


async def list_projects(
    conn: AsyncConnection, *, keyset: Keyset, status: str | None
) -> Page[ProjectOut]:
    rows = await repo.list_projects(conn, keyset=keyset, status=status)
    page, cursor = keyset.paginate(rows)
    return Page(items=[ProjectOut.model_validate(r) for r in page], next_cursor=cursor)


async def active_project_briefs(
    conn: AsyncConnection, *, exclude: uuid.UUID | None = None
) -> list[str]:
    """The brief text of every active (non-archived, non-released) project, minus
    ``exclude``. Gate G1 compares an incoming UserRequest against these to reject a
    duplicate of an already-active project (§2.3 H1)."""
    rows = await repo.active_project_briefs(conn, exclude=exclude)
    return [str(r.brief) for r in rows]


async def update_project(
    conn: AsyncConnection, project_id: uuid.UUID, patch: ProjectPatch, expected_version: int
) -> ProjectOut | None:
    """Apply a partial update under an optimistic-lock guard. Returns None if
    the version moved under us (caller maps that to 412)."""
    fields = patch.model_dump(exclude_unset=True)
    row = await repo.update_project(conn, project_id, expected_version, fields)
    return ProjectOut.model_validate(row) if row is not None else None


async def archive_project(conn: AsyncConnection, project_id: uuid.UUID) -> ProjectOut:
    """AIP-136 :archive — terminal status transition (no state machine yet)."""
    row = await repo.set_project_status(conn, project_id, "archived")
    if row is None:
        raise not_found("project")
    return ProjectOut.model_validate(row)


# --- milestones ------------------------------------------------------------


async def create_milestone(
    conn: AsyncConnection, project_id: uuid.UUID, data: MilestoneCreate
) -> MilestoneOut:
    row = await repo.insert_milestone(
        conn,
        milestone_id=uuid7(),
        project_id=project_id,
        title=data.title,
        due_at=data.due_at,
        status=data.status,
    )
    return MilestoneOut.model_validate(row)


async def get_milestone_in_project(
    conn: AsyncConnection, project_id: uuid.UUID, milestone_id: uuid.UUID
) -> MilestoneOut:
    row = await repo.get_milestone(conn, milestone_id)
    # Object-scope (TITAN-SEC-032): a milestone that is missing OR belongs to a
    # different project is indistinguishable — 404, never a cross-project leak.
    if row is None or row.project_id != project_id:
        raise not_found("milestone")
    return MilestoneOut.model_validate(row)


async def list_milestones(
    conn: AsyncConnection, *, project_id: uuid.UUID, keyset: Keyset
) -> Page[MilestoneOut]:
    rows = await repo.list_milestones(conn, project_id=project_id, keyset=keyset)
    page, cursor = keyset.paginate(rows)
    return Page(items=[MilestoneOut.model_validate(r) for r in page], next_cursor=cursor)


async def update_milestone(
    conn: AsyncConnection,
    milestone_id: uuid.UUID,
    patch: MilestonePatch,
    expected_version: int,
) -> MilestoneOut | None:
    fields = patch.model_dump(exclude_unset=True)
    row = await repo.update_milestone(conn, milestone_id, expected_version, fields)
    return MilestoneOut.model_validate(row) if row is not None else None


# --- sprints ---------------------------------------------------------------


async def create_sprint(
    conn: AsyncConnection, project_id: uuid.UUID, data: SprintCreate, actor: str
) -> SprintOut:
    try:
        row = await repo.insert_sprint(
            conn,
            sprint_id=uuid7(),
            project_id=project_id,
            number=data.number,
            goal=data.goal,
            starts_at=data.starts_at,
            ends_at=data.ends_at,
            status=data.status,
        )
    except IntegrityError as exc:
        raise _conflict(f"Sprint number {data.number} already exists for this project.") from exc
    sprint = SprintOut.model_validate(row)
    await _emit_sprint_updated(conn, sprint, actor)
    return sprint


async def get_sprint_in_project(
    conn: AsyncConnection, project_id: uuid.UUID, sprint_id: uuid.UUID
) -> SprintOut:
    row = await repo.get_sprint(conn, sprint_id)
    if row is None or row.project_id != project_id:
        raise not_found("sprint")
    return SprintOut.model_validate(row)


async def list_sprints(
    conn: AsyncConnection, *, project_id: uuid.UUID, keyset: Keyset
) -> Page[SprintOut]:
    rows = await repo.list_sprints(conn, project_id=project_id, keyset=keyset)
    page, cursor = keyset.paginate(rows)
    return Page(items=[SprintOut.model_validate(r) for r in page], next_cursor=cursor)


async def update_sprint(
    conn: AsyncConnection,
    sprint_id: uuid.UUID,
    patch: SprintPatch,
    expected_version: int,
    actor: str,
) -> SprintOut | None:
    fields = patch.model_dump(exclude_unset=True)
    row = await repo.update_sprint(conn, sprint_id, expected_version, fields)
    if row is None:
        return None
    sprint = SprintOut.model_validate(row)
    await _emit_sprint_updated(conn, sprint, actor)
    return sprint


__all__ = [
    "MilestoneCreate",
    "MilestoneOut",
    "MilestonePatch",
    "ProjectCreate",
    "ProjectOut",
    "ProjectPatch",
    "SprintCreate",
    "SprintOut",
    "SprintPatch",
    "archive_project",
    "create_milestone",
    "create_project",
    "create_sprint",
    "get_milestone_in_project",
    "get_project",
    "get_sprint_in_project",
    "list_milestones",
    "list_projects",
    "list_sprints",
    "update_milestone",
    "update_project",
    "update_sprint",
]
