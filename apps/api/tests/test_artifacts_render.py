"""Pure deterministic renderers for the six §5 artifacts (Phase 51; TITAN-WFL-020).
No DB — each renderer is a pure function of a gathered snapshot; the key property is
determinism (byte-identical output for the same input), the foundation of the replay
DoD."""

from titan.artifacts import render


def test_renderers_are_deterministic_and_cover_all_six_kinds() -> None:
    assert set(render.ARTIFACT_KINDS) == {
        "roadmap",
        "kanban",
        "dependency_graph",
        "release_plan",
        "risk_analysis",
        "debt_report",
    }


def test_roadmap_marks_reached_milestones_and_sorts_priorities() -> None:
    data = {
        "current_milestone": "M2",
        "sprints": [
            {"number": 2, "goal": "features", "walking_skeleton": False, "task_ids": ["T3"]},
            {"number": 1, "goal": "skeleton", "walking_skeleton": True, "task_ids": ["T2", "T1"]},
        ],
        "priorities": [
            {"feature_id": "F2", "name": "Search", "moscow": "should"},
            {"feature_id": "F1", "name": "Contacts", "moscow": "must"},
        ],
    }
    md, js = render.roadmap(data)
    assert render.roadmap(data) == (md, js)  # deterministic
    # Sprints sorted by number; skeleton labelled; tasks sorted.
    assert md.index("Sprint 1") < md.index("Sprint 2")
    assert "(walking skeleton)" in md
    assert "Tasks: T1, T2" in md
    # M0..M2 reached, M3/M4 not.
    assert js["milestones"][2] == {"id": "M2", "label": "Feature Complete", "reached": True}
    assert js["milestones"][3]["reached"] is False
    # MUST sorts before SHOULD.
    assert [p["feature_id"] for p in js["priorities"]] == ["F1", "F2"]


def test_kanban_groups_into_lanes_in_fixed_order() -> None:
    data = {
        "cards": [
            {"id": "b", "title": "beta", "priority": 2, "lane": "DONE"},
            {"id": "a", "title": "alpha", "priority": 1, "lane": "IN_PROGRESS"},
            {"id": "c", "title": "gamma", "priority": 1, "lane": "IN_PROGRESS"},
        ]
    }
    md, js = render.kanban(data)
    lanes = [col["lane"] for col in js["columns"]]
    assert lanes[:4] == ["BACKLOG", "READY", "BLOCKED", "IN_PROGRESS"]
    ip = next(c for c in js["columns"] if c["lane"] == "IN_PROGRESS")
    assert [c["id"] for c in ip["cards"]] == ["a", "c"]  # sorted by (priority, id)


def test_dependency_graph_emits_dot_with_dependency_direction() -> None:
    data = {
        "task_nodes": [
            {"id": "T2", "title": "b", "status": "ready"},
            {"id": "T1", "title": "a", "status": "done"},
        ],
        "task_edges": [{"task_id": "T2", "depends_on_id": "T1"}],
        "component_nodes": [{"name": "UC", "layer": "use_cases"}],
        "component_edges": [{"from": "Entity", "to": "UC"}],
    }
    md, js = render.dependency_graph(data)
    assert md.startswith("digraph tasks {")
    assert '"T1" -> "T2";' in md  # dependency points into the dependent task
    assert "digraph components {" in md
    assert js["tasks"]["nodes"][0]["id"] == "T1"  # sorted by id


def test_release_plan_and_debt_and_risk_render_tables() -> None:
    rel_md, rel_js = render.release_plan(
        {
            "current_milestone": "M4",
            "deployments": [
                {"name": "web", "status": "running", "health": "green", "url": "http://x"}
            ],
            "traceability": {
                "rows": [
                    {
                        "feature_id": "F1",
                        "story_ids": ["S1"],
                        "task_ids": ["T1"],
                        "pr_numbers": [1],
                        "test_ids": ["t1"],
                        "doc_refs": ["r"],
                    }
                ],
                "gaps": [],
            },
        }
    )
    assert "Release Plan" in rel_md and "M4" in rel_md and "F1" in rel_md

    debt_md, debt_js = render.debt_report(
        {
            "open_total": 2,
            "tickets": [
                {
                    "severity": "low",
                    "source": "code_review",
                    "title": "z",
                    "status": "open",
                    "paydown_proposal": "p",
                },
                {
                    "severity": "high",
                    "source": "security",
                    "title": "a",
                    "status": "open",
                    "paydown_proposal": "q",
                },
            ],
        }
    )
    # Ranked by severity: high before low.
    assert debt_js["tickets"][0]["severity"] == "high"
    assert "Open tickets:** 2" in debt_md

    risk_md, risk_js = render.risk_analysis(
        {
            "risks": [
                {
                    "category": "assumption",
                    "description": "z",
                    "likelihood": 2,
                    "impact": 3,
                    "owner": "ceo",
                    "mitigation": "m",
                },
                {
                    "category": "pipeline_escalation",
                    "description": "a",
                    "likelihood": 4,
                    "impact": 5,
                    "owner": "arch",
                    "mitigation": "m",
                },
            ]
        }
    )
    # Ranked by score desc: escalation (20) before assumption (6).
    assert [r["score"] for r in risk_js["risks"]] == [20, 6]
    assert "Risk Analysis" in risk_md
