"""The engineering-standards constitution (Vol 6 §6).

The constitution is a versioned, committed data artifact — the verbatim §6 rules
that bind every TITAN-generated project. Agents retrieve it into their system
context (never hardcode it into prompts); gate G5 evaluates the ArchitecturePackage
against a *specific committed version* and records which version it judged against.

Only ``service`` crosses the module boundary (TITAN-BCK-004)."""
