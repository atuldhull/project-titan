"""Per-task workspace sandboxing (Phase 20; Vol 7 §4.3, Vol 8 §5, ADR-SEC-02).

The Sandbox Controller creates hardened, ephemeral per-task containers in which
LLM-generated code runs; the per-task Workspace is the jailed ``/workspace`` the
fs.* tools operate on and the sandbox bind-mounts. Nothing generated ever runs
in the orchestrator process or on the host (TITAN-INT-001 / TITAN-SEC-040)."""
