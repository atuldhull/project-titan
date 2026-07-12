"""The CEO clarification engine (Vol 6 §2.4, TITAN-WFL-007/008).

When the CEO turns a UserRequest into a ProjectBrief, most unknowns are resolved
autonomously from canonical defaults and organization memory and logged as
user-overridable assumptions. Only *essential* unknowns — those whose answer is not
inferable, not a canonical default, and materially changes the brief — become
questions, at most five, in a single batch. If the user does not answer within a
configurable non-blocking window (default 24h) the pipeline proceeds with
best-judgment assumptions, flagged for the release approval (H12).

Only ``service`` crosses the module boundary (TITAN-BCK-004)."""
