"""A DELIBERATELY VULNERABLE fixture for the TITAN-SEC-072 semgrep rule.

Never imported or executed — it exists only so the dynamic-code-execution ban
(.semgrep/no-dynamic-exec.yml) has a committed positive case to fire on. Both
functions evaluate arbitrary code from an argument, the exact builtins the rule
bans; `make semgrep` scans src/ + alembic/ (which are clean), this fixture is
scanned only by the rule's self-test.
"""


def via_eval(model_output):  # type: ignore[no-untyped-def]
    return eval(model_output)  # noqa


def via_exec(tool_result):  # type: ignore[no-untyped-def]
    exec(tool_result)  # noqa
