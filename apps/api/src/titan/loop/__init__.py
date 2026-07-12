"""The self-improvement loop (Vol 6 §3, TITAN-WFL-010–014).

The macro pipeline (§2) is the flow *between* agents; this is the micro flow
*inside* every engineering task — no component ships as a first draft. A bounded
per-task loop (≤3 cycles) reviews, critiques, refactors, tests, scans, and profiles
the work; it converges or escalates, and a task cannot MERGE without an approved
loop record. The reviewer instance is isolated from the implementer's context.

Only ``service`` crosses the module boundary (TITAN-BCK-004); the engine, models,
repository, and router stay private."""
