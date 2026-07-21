"""Scoped sandbox secret injection (Phase 68, TITAN-SEC-053). No Docker: tests
the pure resolver + the secrets-tar builder. The whole point is that a task gets
ONLY its granted tools' declared secrets — zero for every current task."""

import io
import tarfile

from tests.conftest import run_async
from titan.tools.registry import ToolRegistry, ToolSpec
from titan.tools.sandbox.controller import _build_secrets_tar, _uid_gid
from titan.tools.sandbox.injection import resolve_sandbox_secrets
from titan.vault.drivers.static import StaticSecretsProvider

_SCHEMA = {"type": "object"}


def _spec(name: str, required_secrets: tuple[str, ...] = ()) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider="builtin:test",
        description="t",
        risk="read",
        required_scopes=(),
        input_schema=_SCHEMA,
        output_schema=_SCHEMA,
        executor=lambda call, ctx: None,  # type: ignore[arg-type,return-value]
        required_secrets=required_secrets,
    )


def test_docs_task_tools_inject_no_secrets() -> None:
    registry = ToolRegistry()
    registry.register(_spec("docs.read"))
    registry.register(_spec("memory.search"))
    provider = StaticSecretsProvider({"DEPLOY_KEY": "should-not-appear"})

    async def go() -> dict[str, str]:
        return await resolve_sandbox_secrets(["docs.read", "memory.search"], registry, provider)

    assert run_async(go) == {}


def test_tool_declaring_a_secret_gets_exactly_that_secret() -> None:
    registry = ToolRegistry()
    registry.register(_spec("deploy.push", required_secrets=("DEPLOY_KEY",)))
    registry.register(_spec("terminal.run"))
    provider = StaticSecretsProvider({"DEPLOY_KEY": "sk-deploy", "OTHER": "nope"})

    async def go() -> dict[str, str]:
        return await resolve_sandbox_secrets(["deploy.push", "terminal.run"], registry, provider)

    injected = run_async(go)
    assert injected == {"DEPLOY_KEY": "sk-deploy"}  # scoped: only the declared one


def test_missing_provisioned_secret_is_skipped_not_fatal() -> None:
    registry = ToolRegistry()
    registry.register(_spec("deploy.push", required_secrets=("ABSENT",)))
    provider = StaticSecretsProvider({})

    async def go() -> dict[str, str]:
        return await resolve_sandbox_secrets(["deploy.push"], registry, provider)

    assert run_async(go) == {}  # degrades, does not raise


def test_ungranted_tool_secret_is_not_injected() -> None:
    # A tool the task is NOT granted must not leak its secret in.
    registry = ToolRegistry()
    registry.register(_spec("deploy.push", required_secrets=("DEPLOY_KEY",)))
    provider = StaticSecretsProvider({"DEPLOY_KEY": "sk"})

    async def go() -> dict[str, str]:
        return await resolve_sandbox_secrets(["terminal.run"], registry, provider)

    assert run_async(go) == {}


def test_secrets_tar_builds_readonly_owned_files() -> None:
    blob = _build_secrets_tar({"DEPLOY_KEY": "sk-1", "TOKEN": "t-2"}, uid=10001, gid=10001)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert set(members) == {"DEPLOY_KEY", "TOKEN"}
        for member in members.values():
            assert member.mode == 0o400  # only the sandbox user can read
            assert member.uid == 10001 and member.gid == 10001
        assert tar.extractfile("DEPLOY_KEY").read() == b"sk-1"  # type: ignore[union-attr]


def test_uid_gid_parsing() -> None:
    assert _uid_gid("10001:10002") == (10001, 10002)
    assert _uid_gid("10001") == (10001, 10001)
    assert _uid_gid("root") == (0, 0)  # unparseable → safe default
