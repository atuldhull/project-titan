"""TITAN-Bench: a fixed, versioned agent-performance suite (Vol 10 §6.2, EVO-016/017).

The suite (``evolution/bench/suite.yaml``) is a corpus of labeled cases across six
tracks; each track has a DETERMINISTIC scorer so a run is reproducible. A ``solver``
(``BenchCase -> response dict``) produces each case's response — in production by running
the case through the agent runtime on the offline profile; in tests a stub. Scores are
0..1 per case, averaged per track. The offline GATE (``gates.yaml``) blocks an EP whose
challenger is inferior to the champion on any track (non-inferiority margin + floor).

Scoring is intentionally simple + pure: no LLM is needed to SCORE a case (only to SOLVE
one), so the gate is deterministic and CI-safe.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TRACKS: tuple[str, ...] = ("B-CODE", "B-REVIEW", "B-PLAN", "B-ARCH", "B-SEC", "B-REG")

Solver = Callable[["BenchCase"], dict[str, Any]]


@dataclass(frozen=True)
class BenchCase:
    id: str
    track: str
    title: str
    input: str
    answer: dict[str, Any]
    weight: float = 1.0


@dataclass(frozen=True)
class BenchSuite:
    suite_version: int
    cases: tuple[BenchCase, ...]

    def by_track(self, track: str) -> list[BenchCase]:
        return [c for c in self.cases if c.track == track]


@dataclass
class TrackResult:
    track: str
    score: float
    n: int


@dataclass
class BenchReport:
    suite_version: int
    per_track: dict[str, TrackResult]
    per_case: dict[str, float]
    overall: float
    manifest_sha: str = ""
    label: str = ""

    def track_score(self, track: str) -> float | None:
        r = self.per_track.get(track)
        return r.score if r is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_version": self.suite_version,
            "manifest_sha": self.manifest_sha,
            "label": self.label,
            "overall": round(self.overall, 4),
            "tracks": {
                t: {"score": round(r.score, 4), "n": r.n} for t, r in sorted(self.per_track.items())
            },
            "cases": {cid: round(s, 4) for cid, s in sorted(self.per_case.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchReport:
        tracks = {
            str(t): TrackResult(track=str(t), score=float(v["score"]), n=int(v.get("n", 0)))
            for t, v in (data.get("tracks") or {}).items()
        }
        return cls(
            suite_version=int(data["suite_version"]),
            per_track=tracks,
            per_case={str(k): float(v) for k, v in (data.get("cases") or {}).items()},
            overall=float(data.get("overall", 0.0)),
            manifest_sha=str(data.get("manifest_sha", "")),
            label=str(data.get("label", "")),
        )


def _repo_root(repo_root: Path | None) -> Path:
    if repo_root is not None:
        return repo_root
    # apps/api/src/titan/evolution/bench.py -> repo root is five parents up.
    return Path(__file__).resolve().parents[5]


def load_bench_suite(repo_root: Path | None = None) -> BenchSuite:
    raw = yaml.safe_load(
        (_repo_root(repo_root) / "evolution" / "bench" / "suite.yaml").read_text("utf-8")
    )
    cases = tuple(
        BenchCase(
            id=str(c["id"]),
            track=str(c["track"]),
            title=str(c.get("title", "")),
            input=str(c.get("input", "")),
            answer=dict(c.get("answer", {})),
            weight=float(c.get("weight", 1.0)),
        )
        for c in raw.get("cases", [])
    )
    return BenchSuite(suite_version=int(raw["suite_version"]), cases=cases)


def load_gates(repo_root: Path | None = None) -> dict[str, Any]:
    raw = yaml.safe_load(
        (_repo_root(repo_root) / "evolution" / "bench" / "gates.yaml").read_text("utf-8")
    )
    return dict(raw) if isinstance(raw, dict) else {}


# ---- per-track scorers (pure; response is the solver's structured output) -------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score_code(answer: dict[str, Any], response: dict[str, Any]) -> float:
    total = int(answer.get("hidden_tests_total", 0)) or 1
    passed = int(response.get("tests_passed", 0))
    return _clamp01(passed / total)


def _score_review(answer: dict[str, Any], response: dict[str, Any]) -> float:
    seeded = {str(x) for x in answer.get("seeded_defects", [])}
    if not seeded:
        return 0.0
    found = {str(x) for x in response.get("found", [])}
    return _clamp01(len(seeded & found) / len(seeded))


def _score_plan(answer: dict[str, Any], response: dict[str, Any]) -> float:
    exp_nodes = {str(x) for x in answer.get("nodes", [])}
    exp_edges = {(str(a), str(b)) for a, b in answer.get("edges", [])}
    got_nodes = {str(x) for x in response.get("nodes", [])}
    got_edges = {(str(a), str(b)) for a, b in response.get("edges", []) if isinstance(a, str | int)}
    node_recall = len(exp_nodes & got_nodes) / len(exp_nodes) if exp_nodes else 0.0
    edge_recall = len(exp_edges & got_edges) / len(exp_edges) if exp_edges else node_recall
    return _clamp01((node_recall + edge_recall) / 2)


def _score_arch(answer: dict[str, Any], response: dict[str, Any]) -> float:
    # Coverage of the required concepts ONLY — the solver never supplies its own grade (a
    # trusted `rubric_score` in the response would be self-grading, a fail-open: review gate-7).
    must = {str(x).lower() for x in answer.get("must_mention", [])}
    if not must:
        return 0.0
    mentioned = {str(x).lower() for x in response.get("mentioned", [])}
    return _clamp01(len(must & mentioned) / len(must))


def _score_sec(answer: dict[str, Any], response: dict[str, Any]) -> float:
    return 1.0 if str(response.get("label", "")) == str(answer.get("label", "")) else 0.0


def _score_reg(answer: dict[str, Any], response: dict[str, Any]) -> float:
    # A regression case passes iff the fixed signature did NOT recur. FAIL-CLOSED on a
    # non-answer: a missing `recurred` key cannot pass a zero-margin regression track — an
    # empty/garbled solver response must NOT silently clear B-REG (that would be a
    # fail-open gate: a challenger that can't answer would auto-pass the regression check).
    if "recurred" not in response:
        return 0.0
    return 0.0 if bool(response["recurred"]) else 1.0


_SCORERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], float]] = {
    "B-CODE": _score_code,
    "B-REVIEW": _score_review,
    "B-PLAN": _score_plan,
    "B-ARCH": _score_arch,
    "B-SEC": _score_sec,
    "B-REG": _score_reg,
}


def score_case(case: BenchCase, response: dict[str, Any]) -> float:
    scorer = _SCORERS.get(case.track)
    if scorer is None:
        raise ValueError(f"unknown bench track: {case.track}")
    return scorer(case.answer, response)


def run_bench(
    suite: BenchSuite,
    solver: Solver,
    *,
    tracks: Sequence[str] | None = None,
    manifest_sha: str = "",
    label: str = "",
) -> BenchReport:
    """Score every case (in the selected tracks) through ``solver`` + the track scorer.

    A solver that raises on a case scores that case 0.0 (a crashing config must not get a
    free pass) — the bench is a floor test, not a fault-tolerance test."""
    wanted = set(tracks) if tracks else set(TRACKS)
    per_case: dict[str, float] = {}
    track_scores: dict[str, list[tuple[float, float]]] = {}  # track -> [(score, weight)]
    for case in suite.cases:
        if case.track not in wanted:
            continue
        try:
            response = solver(case)
            s = score_case(case, response)
        except Exception:  # noqa: BLE001 — a crashing solver scores 0, never aborts the run
            s = 0.0
        per_case[case.id] = s
        track_scores.setdefault(case.track, []).append((s, case.weight))
    per_track: dict[str, TrackResult] = {}
    total_w = 0.0
    total_s = 0.0
    for track, pairs in track_scores.items():
        w = sum(p[1] for p in pairs) or 1.0
        score = sum(p[0] * p[1] for p in pairs) / w
        per_track[track] = TrackResult(track=track, score=score, n=len(pairs))
        total_w += w
        total_s += score * w
    overall = total_s / total_w if total_w else 0.0
    return BenchReport(
        suite_version=suite.suite_version,
        per_track=per_track,
        per_case=per_case,
        overall=overall,
        manifest_sha=manifest_sha,
        label=label,
    )


# ---- the offline gate (non-inferiority vs champion + absolute floor) -------------------


@dataclass
class GateOutcome:
    passed: bool
    failures: list[str] = field(default_factory=list)


def evaluate_gate(
    challenger: BenchReport, gates: dict[str, Any], *, champion: BenchReport | None = None
) -> GateOutcome:
    """Challenger passes a track iff its score ≥ max(min_score, champion_score - margin).

    With no champion report, only the absolute ``min_score`` floor applies. A track the
    challenger did not run (score is None) FAILS closed — a missing track is not a pass."""
    if int(gates.get("suite_version", -1)) != challenger.suite_version:
        return GateOutcome(passed=False, failures=["suite_version mismatch vs gates.yaml"])
    failures: list[str] = []
    track_gates: dict[str, Any] = gates.get("tracks", {})
    for track, cfg in track_gates.items():
        cs = challenger.track_score(track)
        if cs is None:
            failures.append(f"{track}: challenger has no score (missing track fails closed)")
            continue
        floor = float(cfg.get("min_score", 0.0))
        threshold = floor
        if champion is not None:
            champ = champion.track_score(track)
            if champ is not None:
                threshold = max(floor, champ - float(cfg.get("margin", 0.0)))
        if cs + 1e-9 < threshold:
            failures.append(f"{track}: {cs:.3f} < required {threshold:.3f}")
    return GateOutcome(passed=not failures, failures=failures)


def render_markdown(report: BenchReport, *, gate: GateOutcome | None = None) -> str:
    lines = [
        f"# TITAN-Bench report (suite v{report.suite_version})",
        "",
        f"- manifest_sha: `{report.manifest_sha or 'n/a'}`",
        f"- label: {report.label or 'n/a'}",
        f"- overall: **{report.overall:.3f}**",
        "",
        "| track | score | n |",
        "|---|---|---|",
    ]
    for track in TRACKS:
        r = report.per_track.get(track)
        if r is not None:
            lines.append(f"| {track} | {r.score:.3f} | {r.n} |")
    if gate is not None:
        lines += ["", f"**Gate: {'PASS' if gate.passed else 'FAIL'}**"]
        lines += [f"- {f}" for f in gate.failures]
    return "\n".join(lines) + "\n"


# ---- B-REG cases generated from the live Root-Cause Records (Phase 85) -----------------


_TRACK_RESPONSE_SHAPE = {
    "B-CODE": '{"tests_passed": <int>}',
    "B-REVIEW": '{"found": ["<defect-id>", "..."]}',
    "B-PLAN": '{"nodes": ["..."], "edges": [["a","b"]]}',
    "B-ARCH": '{"mentioned": ["<concept>", "..."]}',
    "B-SEC": '{"label": "vuln|safe"}',
    "B-REG": '{"recurred": <true|false>}',
}


async def gateway_solve(
    gateway: Any, case: BenchCase, *, model_alias: str | None = None
) -> dict[str, Any]:
    """Solve one case by routing it through the LLM gateway and parsing the structured
    response. The model SOLVES; the track scorer GRADES the parsed response — so scoring
    stays deterministic. An unparseable response is ``{}`` (which the scorer grades as a
    miss). Used by ``titan bench run`` against the offline profile."""
    import json as _json

    from titan.llm.models import ChatRequest, Message, RequestMetadata

    shape = _TRACK_RESPONSE_SHAPE.get(case.track, "{}")
    system = (
        f"You are solving a {case.track} benchmark case. Read the input and respond with "
        f"ONLY a JSON object of the form {shape} — no prose, no code fences."
    )
    resp = await gateway.chat(
        ChatRequest(
            messages=[
                Message(role="system", content=system),
                Message(role="user", content=f"{case.title}\n\n{case.input}"),
            ],
            model_alias=model_alias,
            max_tokens=400,
            metadata=RequestMetadata(
                agent_id="agent.bench", task_id=None, project_id=None, prompt_sha=""
            ),
        )
    )
    text_out = str(resp.text or "")
    try:
        start, end = text_out.index("{"), text_out.rindex("}") + 1
        data = _json.loads(text_out[start:end])
    except (ValueError, _json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


async def run_bench_async(
    suite: BenchSuite,
    solver: Any,  # Callable[[BenchCase], Awaitable[dict]]
    *,
    tracks: Sequence[str] | None = None,
    manifest_sha: str = "",
    label: str = "",
) -> BenchReport:
    """Async variant of :func:`run_bench` for a coroutine solver (e.g. the gateway).
    Same scoring/aggregation; a solver that raises scores that case 0.0."""
    wanted = set(tracks) if tracks else set(TRACKS)
    responses: dict[str, dict[str, Any]] = {}
    for case in suite.cases:
        if case.track not in wanted:
            continue
        try:
            responses[case.id] = await solver(case)
        except Exception:  # noqa: BLE001 — a crashing solver scores that case 0
            responses[case.id] = {"__error__": True}
    return run_bench(
        suite,
        lambda c: responses.get(c.id, {}),
        tracks=tracks,
        manifest_sha=manifest_sha,
        label=label,
    )


async def bench_cases_from_rcrs(engine: Any, *, limit: int = 25) -> list[BenchCase]:
    """Generate B-REG regression cases from REMEDIATED RCRs (EVO-016 §6.2): every
    remediated failure signature becomes a case that must not recur. The solver checks
    whether the signature has an OPEN RCR again (a recurrence)."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT DISTINCT signature FROM root_cause_records "
                    "WHERE status = 'remediated' ORDER BY signature LIMIT :lim"
                ),
                {"lim": limit},
            )
        ).all()
    return [
        BenchCase(
            id=f"reg-{sig}",
            track="B-REG",
            title=f"Regression — remediated {sig} must not recur",
            input=f"Re-run the scenario that produced {sig}.",
            answer={"signature": sig},
        )
        for (sig,) in rows
    ]


async def signature_recurred(engine: Any, signature: str) -> bool:
    """True if the signature currently has an OPEN RCR (i.e. it recurred after remediation)."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT 1 FROM root_cause_records WHERE signature = :sig AND status = 'open' "
                    "LIMIT 1"
                ),
                {"sig": signature},
            )
        ).first()
    return row is not None


__all__ = [
    "TRACKS",
    "BenchCase",
    "BenchReport",
    "BenchSuite",
    "GateOutcome",
    "Solver",
    "TrackResult",
    "bench_cases_from_rcrs",
    "evaluate_gate",
    "gateway_solve",
    "load_bench_suite",
    "load_gates",
    "render_markdown",
    "run_bench",
    "run_bench_async",
    "score_case",
    "signature_recurred",
]
