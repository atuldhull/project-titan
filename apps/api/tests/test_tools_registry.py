"""Tool Registry + allowlist + redaction units (Phase 18; Vol 7 §4.1,
Vol 8 §4.3). No DB — the registry is in-memory."""

from typing import Any

import pytest

from titan.llm.service import ToolDef
from titan.tools.allowlists import allowed_for_role
from titan.tools.redaction import MASK, default_redactor
from titan.tools.registry import (
    ExecutionOutcome,
    ProjectToolPolicy,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)

_OBJ: dict[str, Any] = {"type": "object"}


async def _noop(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
    return ExecutionOutcome(result={})


def _spec(name: str, provider: str = "builtin:test", risk: str = "read", **kw: Any) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider=provider,
        description=f"{name} test tool",
        risk=risk,
        required_scopes=("test:run",),
        input_schema=kw.pop("input_schema", _OBJ),
        output_schema=kw.pop("output_schema", _OBJ),
        executor=_noop,
        **kw,
    )


# --- registration validation -------------------------------------------------


def test_register_accepts_the_spec_registry_entry_shape() -> None:
    # The §4.1 git.commit example, field for field.
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="git.commit",
            provider="builtin:git",
            description="Create a commit in the project workspace repository.",
            risk="write",
            required_scopes=("workspace:write",),
            timeout_s=30,
            input_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "message": {"type": "string", "minLength": 10},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "author_agent": {"type": "string"},
                },
                "required": ["message", "paths"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "commit_sha": {"type": "string"},
                    "files_changed": {"type": "integer"},
                },
                "required": ["commit_sha"],
            },
            executor=_noop,
        )
    )
    spec = registry.get("git.commit")
    assert spec is not None and spec.risk == "write" and spec.timeout_s == 30


@pytest.mark.parametrize(
    "bad_name", ["NotNamespaced", "single", "fs..read", ".fs.read", "fs.Read", "9fs.read"]
)
def test_register_rejects_bad_names(bad_name: str) -> None:
    with pytest.raises(ValueError, match="namespaced"):
        ToolRegistry().register(_spec(bad_name))


def test_register_rejects_duplicates_bad_risk_and_bad_timeout() -> None:
    registry = ToolRegistry()
    registry.register(_spec("test.echo"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec("test.echo"))
    with pytest.raises(ValueError, match="risk"):
        registry.register(_spec("test.risky", risk="yolo"))
    with pytest.raises(ValueError, match="timeout"):
        registry.register(_spec("test.slow", timeout_s=0))


def test_register_rejects_invalid_json_schema_and_non_object_schema() -> None:
    with pytest.raises(Exception):  # noqa: B017 — jsonschema SchemaError
        ToolRegistry().register(_spec("test.bad", input_schema={"type": 42}))
    with pytest.raises(ValueError, match="type 'object'"):
        ToolRegistry().register(_spec("test.scalar", input_schema={"type": "string"}))
    with pytest.raises(ValueError, match="output_schema"):
        ToolRegistry().register(_spec("test.out", output_schema={"type": "array"}))


# --- per-turn filtering (§4.1 three-way intersection) -------------------------


def _filtering_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_spec("task.get"))
    registry.register(_spec("message.post", provider="builtin:messaging", risk="write"))
    registry.register(_spec("git.commit", provider="builtin:git", risk="write"))
    registry.register(_spec("terminal.run", provider="builtin:terminal", risk="write"))
    return registry


def test_tools_for_turn_intersects_role_allowlist() -> None:
    registry = _filtering_registry()
    policy = ProjectToolPolicy()
    # PM: task CRUD + messaging; never git/terminal (Vol 8 §4.3 row 1).
    assert [t.name for t in registry.tools_for_turn("pm", policy)] == ["message.post", "task.get"]
    # Backend: everything registered here.
    assert [t.name for t in registry.tools_for_turn("backend", policy)] == [
        "git.commit",
        "message.post",
        "task.get",
        "terminal.run",
    ]
    # Unknown role: fail closed.
    assert registry.tools_for_turn("intern", policy) == []


def test_tools_for_turn_intersects_enabled_providers_and_policy() -> None:
    registry = _filtering_registry()
    only_tasks = ProjectToolPolicy(enabled_providers=["builtin:tasks", "builtin:test"])
    assert [t.name for t in registry.tools_for_turn("backend", only_tasks)] == ["task.get"]
    no_git = ProjectToolPolicy(disabled_tools=["git.*"])
    names = [t.name for t in registry.tools_for_turn("backend", no_git)]
    assert "git.commit" not in names and "terminal.run" in names


def test_tools_for_turn_emits_gateway_tool_def_format() -> None:
    registry = ToolRegistry()
    schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    }
    registry.register(_spec("task.get", input_schema=schema))
    (tool,) = registry.tools_for_turn("pm", ProjectToolPolicy())
    # The Phase-15 canonical tool-definition DTO — not a parallel dialect.
    assert isinstance(tool, ToolDef)
    assert tool.name == "task.get"
    assert tool.input_schema == schema


# --- Vol 8 §4.3 allowlist matrix ----------------------------------------------


@pytest.mark.parametrize(
    ("role", "tool", "expected"),
    [
        ("ceo", "task.create", True),
        ("ceo", "git.commit", False),  # git write denied
        ("ceo", "terminal.run", False),  # shell denied
        ("architect", "fs.read", True),
        ("architect", "git.push", False),
        ("architect", "docs.write", True),
        ("backend", "terminal.run", True),
        ("backend", "git.commit", True),
        ("backend", "deploy.trigger", False),
        ("backend", "plugin.install", False),
        ("qa", "test.run", True),
        ("qa", "git.push", False),  # write outside test dirs
        ("security", "scanner.sast", True),
        ("security", "secret.read", False),
        ("reviewer", "git.diff", True),
        ("reviewer", "fs.write", False),  # any code write denied
        ("reviewer", "git.commit", False),
        ("devops", "deploy.trigger", True),
        ("devops", "secret.read", False),  # plaintext read denied
        ("docs", "memory.write", True),
        ("docs", "git.commit", False),
        ("nonexistent", "task.get", False),  # fail closed
    ],
)
def test_allowlist_matrix(role: str, tool: str, expected: bool) -> None:
    assert allowed_for_role(role, tool) is expected


# --- default redaction hook ---------------------------------------------------


def test_redactor_masks_credential_keys_and_token_values() -> None:
    redacted = default_redactor(
        {
            "api_key": "super-secret",
            "nested": {"Authorization": "whatever", "note": "use sk-abcdefgh12345678990 now"},
            "list": ["ghp_0123456789abcdefghij", 42],
            "ok": "plain text stays",
        }
    )
    assert redacted["api_key"] == MASK
    assert redacted["nested"]["Authorization"] == MASK
    assert MASK in redacted["nested"]["note"] and "sk-" not in redacted["nested"]["note"]
    assert redacted["list"][0] == MASK and redacted["list"][1] == 42
    assert redacted["ok"] == "plain text stays"


def test_redactor_masks_dsn_password_and_private_key() -> None:
    out = default_redactor(
        {
            "dsn": "postgresql+asyncpg://titan:hunter2@db:5432/titan",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
        }
    )
    assert "hunter2" not in out["dsn"]
    assert "BEGIN RSA" not in out["pem"]


def test_redactor_does_not_mutate_input() -> None:
    original: dict[str, Any] = {"api_key": "value", "deep": {"token": "t"}}
    default_redactor(original)
    assert original["api_key"] == "value" and original["deep"]["token"] == "t"
