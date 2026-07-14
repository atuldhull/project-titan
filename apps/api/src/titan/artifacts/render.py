"""Deterministic renderers for the six auto-generated project artifacts (Vol 6 §5,
TITAN-WFL-020). PURE — each takes an already-gathered plain-dict snapshot of the
domain state and returns ``(content_md, content_json)``.

Determinism is the contract (the replay DoD): every list is sorted by a stable key
and NO wall-clock value ever enters the content, so regenerating from the same
domain state reproduces byte-identical output.
"""

from typing import Any

# --- helpers -----------------------------------------------------------------

#: MoSCoW priority order for stable ranking.
_MOSCOW_RANK = {"must": 0, "should": 1, "could": 2, "wont": 3}
#: Severity order for the debt/risk ledgers.
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

#: The M0–M4 milestone ladder with human labels (fixed display order).
_MILESTONE_LADDER = [
    ("M0", "Inception"),
    ("M1", "Walking Skeleton"),
    ("M2", "Feature Complete"),
    ("M3", "Hardened"),
    ("M4", "Released"),
]
_MILESTONE_INDEX = {mid: i for i, (mid, _) in enumerate(_MILESTONE_LADDER)}

#: Fixed Kanban lane order (§2.5).
_LANE_ORDER = [
    "BACKLOG",
    "READY",
    "BLOCKED",
    "IN_PROGRESS",
    "IN_LOOP",
    "MERGED",
    "DONE",
    "REOPENED",
    "FAILED",
    "ESCALATED",
    "CANCELLED",
]


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """A GitHub-flavored Markdown table (deterministic; empty → a note)."""
    if not rows:
        return "_none_\n"
    head = "| " + " | ".join(headers) + " |\n"
    sep = "| " + " | ".join("---" for _ in headers) + " |\n"
    body = "".join("| " + " | ".join(cells) + " |\n" for cells in rows)
    return head + sep + body


# --- 1. Roadmap --------------------------------------------------------------


def roadmap(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Milestones + sprint plan + brief feature priorities (Timeline JSON + MD)."""
    current = str(data.get("current_milestone", "none"))
    reached = _MILESTONE_INDEX.get(current, -1)
    milestones = [
        {"id": mid, "label": label, "reached": _MILESTONE_INDEX[mid] <= reached}
        for mid, label in _MILESTONE_LADDER
    ]
    sprints = sorted(data.get("sprints", []), key=lambda s: s["number"])
    priorities = sorted(
        data.get("priorities", []),
        key=lambda f: (_MOSCOW_RANK.get(f.get("moscow", "wont"), 9), str(f.get("feature_id"))),
    )
    content = {
        "current_milestone": current,
        "milestones": milestones,
        "sprints": sprints,
        "priorities": priorities,
    }

    md = ["# Roadmap", "", f"**Current milestone:** {current}", "", "## Milestones", ""]
    for m in milestones:
        md.append(f"- [{'x' if m['reached'] else ' '}] {m['id']} — {m['label']}")
    md += ["", "## Sprints", ""]
    if not sprints:
        md.append("_No sprint plan yet._")
    for s in sprints:
        skeleton = " (walking skeleton)" if s.get("walking_skeleton") else ""
        tasks = ", ".join(sorted(str(t) for t in s.get("task_ids", []))) or "—"
        md.append(f"### Sprint {s['number']} — {s.get('goal', '')}{skeleton}")
        md.append(f"Tasks: {tasks}")
    md += ["", "## Feature priorities", ""]
    if not priorities:
        md.append("_No features yet._")
    for f in priorities:
        md.append(
            f"- {str(f.get('moscow', '')).upper()}: {f.get('name', '')} ({f.get('feature_id')})"
        )
    return "\n".join(md) + "\n", content


# --- 2. Kanban board ---------------------------------------------------------


def kanban(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The task state machine projected into lanes (§2.5)."""
    by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in _LANE_ORDER}
    for card in data.get("cards", []):
        by_lane.setdefault(card["lane"], []).append(card)
    columns: list[dict[str, Any]] = []
    for lane in _LANE_ORDER:
        cards = sorted(by_lane.get(lane, []), key=lambda c: (c.get("priority", 3), str(c["id"])))
        columns.append(
            {
                "lane": lane,
                "cards": [
                    {"id": c["id"], "title": c["title"], "priority": c.get("priority", 3)}
                    for c in cards
                ],
            }
        )
    content = {"columns": columns}

    md = ["# Kanban Board", ""]
    for col in columns:
        md.append(f"## {col['lane']} ({len(col['cards'])})")
        if not col["cards"]:
            md.append("_empty_")
        for c in col["cards"]:
            md.append(f"- [P{c['priority']}] {c['title']} ({c['id']})")
        md.append("")
    return "\n".join(md) + "\n", content


# --- 3. Dependency graph -----------------------------------------------------


def dependency_graph(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """TaskGraph DAG + component graph, as DOT + structured JSON."""
    task_nodes = sorted(data.get("task_nodes", []), key=lambda n: str(n["id"]))
    task_edges = sorted(
        data.get("task_edges", []), key=lambda e: (str(e["task_id"]), str(e["depends_on_id"]))
    )
    comp_nodes = sorted(data.get("component_nodes", []), key=lambda n: str(n["name"]))
    comp_edges = sorted(
        data.get("component_edges", []), key=lambda e: (str(e["from"]), str(e["to"]))
    )
    content = {
        "tasks": {"nodes": task_nodes, "edges": task_edges},
        "components": {"nodes": comp_nodes, "edges": comp_edges},
    }

    dot = ["digraph tasks {", "  rankdir=LR;"]
    for n in task_nodes:
        dot.append(f'  "{n["id"]}" [label="{n.get("title", "")} ({n.get("status", "")})"];')
    for e in task_edges:
        # task depends on depends_on_id ⇒ the dependency points into the task.
        dot.append(f'  "{e["depends_on_id"]}" -> "{e["task_id"]}";')
    dot.append("}")
    dot += ["", "digraph components {", "  rankdir=LR;"]
    for n in comp_nodes:
        dot.append(f'  "{n["name"]}" [label="{n["name"]} ({n.get("layer", "")})"];')
    for e in comp_edges:
        dot.append(f'  "{e["from"]}" -> "{e["to"]}";')
    dot.append("}")
    return "\n".join(dot) + "\n", content


# --- 4. Release plan ---------------------------------------------------------


def release_plan(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Milestones + DeploymentRecords + the traceability table."""
    current = str(data.get("current_milestone", "none"))
    deployments = sorted(data.get("deployments", []), key=lambda d: str(d["name"]))
    rows = sorted(data.get("traceability", {}).get("rows", []), key=lambda r: str(r["feature_id"]))
    gaps = sorted(str(g) for g in data.get("traceability", {}).get("gaps", []))
    content = {
        "current_milestone": current,
        "deployments": deployments,
        "traceability": {"rows": rows, "gaps": gaps},
    }

    md = ["# Release Plan", "", f"**Milestone:** {current}", "", "## Deployments", ""]
    md.append(
        _md_table(
            ["Name", "Status", "Health", "URL"],
            [
                [d["name"], d.get("status", ""), d.get("health", ""), d.get("url") or "—"]
                for d in deployments
            ],
        )
    )
    md += ["", "## Traceability", ""]
    md.append(
        _md_table(
            ["Feature", "Stories", "Tasks", "PRs", "Tests", "Docs"],
            [
                [
                    r["feature_id"],
                    ", ".join(map(str, r.get("story_ids", []))) or "—",
                    ", ".join(map(str, r.get("task_ids", []))) or "—",
                    ", ".join(map(str, r.get("pr_numbers", []))) or "—",
                    ", ".join(map(str, r.get("test_ids", []))) or "—",
                    ", ".join(map(str, r.get("doc_refs", []))) or "—",
                ]
                for r in rows
            ],
        )
    )
    md += ["", "## Gaps", ""]
    if not gaps:
        md.append("_No traceability gaps._")
    for g in gaps:
        md.append(f"- {g}")
    return "\n".join(md) + "\n", content


# --- 5. Risk analysis --------------------------------------------------------


def risk_analysis(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A ranked risk register (likelihood × impact) over escalations, blockers,
    security mediums, and the assumption log."""
    # Rank by score desc, then tie-break on every rendered field for a total order
    # (two blockers/mediums can share a description) — determinism for replay.
    risks = sorted(
        data.get("risks", []),
        key=lambda r: (
            -int(r["likelihood"]) * int(r["impact"]),
            str(r["category"]),
            str(r["description"]),
            int(r["likelihood"]),
            int(r["impact"]),
            str(r.get("owner", "")),
            str(r.get("mitigation", "")),
        ),
    )
    scored = [{**r, "score": int(r["likelihood"]) * int(r["impact"])} for r in risks]
    content = {"risks": scored}

    md = ["# Risk Analysis", "", "## Register (ranked by likelihood × impact)", ""]
    md.append(
        _md_table(
            ["Score", "Category", "Risk", "Likelihood", "Impact", "Owner", "Mitigation"],
            [
                [
                    str(r["score"]),
                    r["category"],
                    r["description"],
                    str(r["likelihood"]),
                    str(r["impact"]),
                    r.get("owner", ""),
                    r.get("mitigation", ""),
                ]
                for r in scored
            ],
        )
    )
    return "\n".join(md) + "\n", content


# --- 6. Technical debt report ------------------------------------------------


def debt_report(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A ranked debt ledger with proposed paydown work (TITAN-WFL-021)."""
    # Sort by (severity, source, title) but tie-break on EVERY remaining rendered
    # field so the order is a total order over distinct tickets — two tickets that
    # differ only in detail/paydown must not depend on the DB's unstable tie order
    # (else byte-identical replay fails). Identical-content rows sort identically and
    # their order is irrelevant to the bytes.
    tickets = sorted(
        data.get("tickets", []),
        key=lambda t: (
            _SEVERITY_RANK.get(t.get("severity", "medium"), 9),
            str(t.get("source")),
            str(t.get("title")),
            str(t.get("detail", "")),
            str(t.get("paydown_proposal", "")),
            str(t.get("status", "")),
        ),
    )
    open_total = int(data.get("open_total", 0))
    content = {"open_total": open_total, "tickets": tickets}

    md = [
        "# Technical Debt Report",
        "",
        f"**Open tickets:** {open_total}",
        "",
        "## Ledger (ranked by severity)",
        "",
    ]
    md.append(
        _md_table(
            ["Severity", "Source", "Title", "Status", "Paydown"],
            [
                [
                    t.get("severity", ""),
                    t.get("source", ""),
                    t.get("title", ""),
                    t.get("status", ""),
                    t.get("paydown_proposal") or "—",
                ]
                for t in tickets
            ],
        )
    )
    return "\n".join(md) + "\n", content


#: kind → renderer. The canonical set of six §5 artifacts.
RENDERERS = {
    "roadmap": roadmap,
    "kanban": kanban,
    "dependency_graph": dependency_graph,
    "release_plan": release_plan,
    "risk_analysis": risk_analysis,
    "debt_report": debt_report,
}
ARTIFACT_KINDS = tuple(RENDERERS)

__all__ = ["ARTIFACT_KINDS", "RENDERERS"]
