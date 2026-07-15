"""Deterministic mock-LLM pipeline fixtures for the executable ``todo-tiny`` brief
(Vol 1 §9.1 "build a task-tracking web app").

These are the twelve handoff artifact bodies the runner submits to drive the H1→
H12 pipeline to a released state with NO real LLM calls — the same shape proven by
the Phase-51 end-to-end pipeline test, themed as the reference task-tracking app.
The ``ceo_vision`` stage ships TWO bodies: the first omits a feature success
criterion so gate G2 fails (proving the loop bites — the §7 first-draft-rejection
KPI), the second fixes it and advances.
"""

from typing import Any

#: H1 clarification: six essential questions. The 5-question budget drops the 6th
#: to a high-visibility, user-overridable assumption restated at release (H12).
CLARIFICATION_QUESTIONS: list[tuple[str, str]] = [
    (f"scope_{i}", f"How should the task app handle scope area {i}?") for i in range(6)
]

# H2: the ProductBrief. BAD omits F1.success_criterion → G2 fail; GOOD adds it.
BAD_BRIEF: dict[str, Any] = {
    "vision": "A local-first task-tracking web app.",
    "target_users": ["individuals", "small teams"],
    "features": [{"id": "F1", "name": "Tasks", "moscow": "must"}],
    "non_goals": ["no calendar sync"],
    "constraints": {"canonical_stack": True},
    "success_criteria": ["create a task in < 1s"],
    "assumptions": [],
}
BRIEF: dict[str, Any] = {
    **BAD_BRIEF,
    "features": [{"id": "F1", "name": "Tasks", "moscow": "must", "success_criterion": "create<1s"}],
}

BACKLOG: dict[str, Any] = {
    "stories": [
        {
            "id": "S1",
            "as_a": "user",
            "i_want": "to create a task",
            "so_that": "I can track my work",
            "acceptance_criteria": [
                {"given": "a title", "when": "I save", "then": "the task appears"}
            ],
            "priority": 1,
            "points": 3,
            "covers_features": ["F1"],
        }
    ]
}
PLAN: dict[str, Any] = {
    "sprints": [{"number": 1, "goal": "skeleton", "task_ids": ["T1"], "walking_skeleton": True}],
    "tasks": [
        {
            "id": "T1",
            "title": "create task endpoint",
            "estimate_days": 0.5,
            "assignee_role": "backend",
            "story_id": "S1",
            "sprint_number": 1,
        }
    ],
    "dependencies": [],
    "milestones": [],
}
ARCH: dict[str, Any] = {
    "component_diagram_ascii": "[router]->[uc]->[entity]",
    "components": [
        {"name": "TaskEntity", "layer": "entities", "depends_on": []},
        {"name": "CreateTask", "layer": "use_cases", "depends_on": ["TaskEntity"]},
        {"name": "TaskRouter", "layer": "interface_adapters", "depends_on": ["CreateTask"]},
    ],
    "api_contracts": [
        {
            "path": "/tasks",
            "method": "POST",
            "operation": {"responses": {"201": {"description": "created"}}},
        }
    ],
    "database": {
        "tables": [{"name": "tasks", "columns": ["id"], "indexes": []}],
        "migration_order": ["0001"],
    },
    "frontend": {"routes": [{"path": "/", "component": "Home"}], "state_management": "redux"},
    "adrs": [],
    "task_components": [{"task_id": "T1", "component": "CreateTask"}],
}
MANIFEST: dict[str, Any] = {
    "prs": [
        {
            "number": 1,
            "title": "create task",
            "task_id": "T1",
            "branch": "feat/task",
            "sha": "abc1234",
        }
    ],
    "changelog": "- create task",
    "lockfiles": ["uv.lock"],
    "migrations": ["0001"],
}
QA: dict[str, Any] = {
    "test_matrix": [{"suite": "e2e", "passed": 12, "failed": 0, "total": 12}],
    "defects": [],
    "flake_log": [],
    "performance_baseline": [{"name": "start", "value": 1.1, "unit": "s"}],
}
SEC: dict[str, Any] = {
    "sast": [],
    "dependency_audit": [],
    "secret_scan": {"clean": True},
    "owasp": [{"category": "A01", "status": "pass", "evidence": "n/a"}],
    "rbac_verified": True,
}
REVIEW: dict[str, Any] = {
    "verdict": "APPROVED_WITH_COMMENTS",
    "architecture_boundary_ok": True,
    "duplication_findings": [],
    "public_api_consistent": True,
    "changelog_accurate": True,
    "comments": ["minor duplication in the task util"],
}
DEPLOY: dict[str, Any] = {
    "image_digests": [{"image": "api", "digest": "sha256:x"}],
    "environment": "production",
    "applied_migrations": ["0001"],
    "smoke_tests": {"passed": 5, "failed": 0},
    "health_checks": "green",
    "rollback_pointer": "v1.0.0",
}
DOCS: dict[str, Any] = {
    "docs": [{"kind": "readme", "path": "README.md"}],
    "documented_endpoints": ["/tasks"],
    "adr_index": ["A1"],
    "quickstart_verified": True,
}
RELEASE: dict[str, Any] = {
    "approved": True,
    "summary": "ships",
    "success_criteria_met": [{"criterion": "create<1s", "met": True}],
    "traceability": [
        {
            "feature_id": "F1",
            "story_ids": ["S1"],
            "task_ids": ["T1"],
            "pr_numbers": [1],
            "test_ids": ["e2e-1"],
            "doc_refs": ["README.md"],
        }
    ],
    "assumptions_restated": ["scope area 5 handled by best judgment"],
}

#: The ordered stages after the (retried) ceo_vision brief — the deploy stage is
#: named so the runner can stamp time-to-first-deploy when it advances.
DEPLOY_STAGE = "devops_deploy"
STAGES_AFTER_BRIEF: list[tuple[str, dict[str, Any]]] = [
    ("pm_backlog", BACKLOG),
    ("pmgr_plan", PLAN),
    ("architect_design", ARCH),
    ("engineering", MANIFEST),
    ("qa", QA),
    ("security", SEC),
    ("code_review", REVIEW),
    (DEPLOY_STAGE, DEPLOY),
    ("documentation", DOCS),
    ("ceo_release", RELEASE),
]

__all__ = [
    "ARCH",
    "BACKLOG",
    "BAD_BRIEF",
    "BRIEF",
    "CLARIFICATION_QUESTIONS",
    "DEPLOY",
    "DEPLOY_STAGE",
    "DOCS",
    "MANIFEST",
    "PLAN",
    "QA",
    "RELEASE",
    "REVIEW",
    "SEC",
    "STAGES_AFTER_BRIEF",
]
