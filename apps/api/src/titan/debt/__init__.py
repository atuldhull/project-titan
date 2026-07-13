"""The technical-debt ledger (Vol 6 §5, TITAN-WFL-021).

Debt is tracked, never hidden: any deviation from the constitution accepted at
review — every ``APPROVED_WITH_COMMENTS`` release-review verdict and every accepted
medium-severity security finding — creates a debt ticket here with a proposed
paydown, and the ledger total is exposed for the project dashboard.

Only ``service`` crosses the module boundary (TITAN-BCK-004); the models,
repository, and router stay private."""
