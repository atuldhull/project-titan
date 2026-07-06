"""Sandbox red-team suite (Phase 20; Vol 8 §5, TITAN-INT-021, TITAN-SEC-040..044).

Marked ``sandbox``: needs a reachable Docker daemon + the alpine base image, so
it runs on the host dev loop and a dedicated non-blocking CI job, skipping
elsewhere (like the ``schema``/``live_llm`` markers). Every test creates a REAL
hardened container and proves the §5 controls hold: host-fs reads blocked,
read-only rootfs, deny-all network, no docker socket, no leaked env, non-root,
and cgroup containment of fork-bomb / memory-hog — with the workspace as the
only writable, host-visible surface.
"""

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from titan.tools.errors import ToolError
from titan.tools.sandbox.controller import SandboxController
from titan.tools.sandbox.policy import ResourceLimits, SandboxPolicy
from titan.tools.sandbox.workspace import Workspace, WorkspaceManager

from .conftest import run_async

pytestmark = pytest.mark.sandbox

_IMAGE = "alpine:3.20"


def _docker_ready() -> bool:
    try:
        return bool(run_async(SandboxController(SandboxPolicy(image=_IMAGE)).ping))
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_docker() -> None:
    if not _docker_ready():
        pytest.skip("no reachable Docker daemon / sandbox image for the red-team suite")


@pytest.fixture
def controller() -> SandboxController:
    return SandboxController(SandboxPolicy(image=_IMAGE))


@pytest.fixture
def workspace() -> Iterator[Workspace]:
    root = Path(tempfile.mkdtemp(prefix="titan-sbx-ws-"))
    manager = WorkspaceManager(root)
    yield manager.for_call(uuid.uuid4(), uuid.uuid4())


def _make(controller: SandboxController, workspace: Workspace, **overrides: object):  # type: ignore[no-untyped-def]
    policy = SandboxPolicy(image=_IMAGE).with_overrides(**overrides) if overrides else None
    return run_async(lambda: controller.create(uuid.uuid4(), workspace, policy=policy))


# --- TITAN-SEC-041: non-root, caps dropped, read-only rootfs -----------------


def test_runs_non_root(controller: SandboxController, workspace: Workspace) -> None:
    sbx = _make(controller, workspace)
    try:
        result = run_async(lambda: sbx.exec(["id", "-u"]))
        assert result.stdout.strip() == "10001"
    finally:
        run_async(sbx.destroy)


def test_no_new_privileges_and_seccomp_are_active(
    controller: SandboxController, workspace: Workspace
) -> None:
    # The privilege-escalation half of SEC-041's acceptance, read from the
    # kernel-exposed process bits (NoNewPrivs=1, Seccomp=2 SECCOMP_MODE_FILTER).
    sbx = _make(controller, workspace)
    try:
        status = run_async(lambda: sbx.exec(["cat", "/proc/self/status"]))
        assert "NoNewPrivs:\t1" in status.stdout
        seccomp = [ln for ln in status.stdout.splitlines() if ln.startswith("Seccomp:")]
        assert seccomp and seccomp[0].strip().endswith("2")  # 0 would be unconfined
    finally:
        run_async(sbx.destroy)


def test_dropping_the_hardening_is_observable(
    controller: SandboxController, workspace: Workspace
) -> None:
    # Negative control: prove the NoNewPrivs probe is meaningful — a container
    # built WITHOUT the opt reports NoNewPrivs=0, so the positive test above
    # would catch a controller that silently stopped emitting it. (The seccomp
    # exec bit is not asserted here: Docker Desktop applies its default profile
    # to `docker exec` regardless of the container's seccomp=unconfined, so the
    # negative control is platform-dependent; the positive Seccomp:2 check
    # remains the SEC-041 guard.)
    sbx = _make(controller, workspace, no_new_privileges=False)
    try:
        status = run_async(lambda: sbx.exec(["cat", "/proc/self/status"]))
        assert "NoNewPrivs:\t0" in status.stdout
    finally:
        run_async(sbx.destroy)


def test_rootfs_is_read_only(controller: SandboxController, workspace: Workspace) -> None:
    sbx = _make(controller, workspace)
    try:
        # A write outside /workspace and /tmp must fail (read-only rootfs).
        result = run_async(lambda: sbx.exec(["touch", "/root/pwned"]))
        assert not result.ok
        # /workspace IS writable and host-visible.
        ok = run_async(lambda: sbx.exec(["touch", "/workspace/created-in-sandbox"]))
        assert ok.ok
        assert (workspace.root / "created-in-sandbox").exists()
    finally:
        run_async(sbx.destroy)


def test_host_secret_read_is_blocked(controller: SandboxController, workspace: Workspace) -> None:
    sbx = _make(controller, workspace)
    try:
        # /etc/shadow exists but is root-only; a non-root sandbox can't read it.
        result = run_async(lambda: sbx.exec(["cat", "/etc/shadow"]))
        assert not result.ok
        assert "permission denied" in (result.stderr.lower() + result.stdout.lower())
    finally:
        run_async(sbx.destroy)


# --- TITAN-SEC-043: deny-all network -----------------------------------------


def test_network_is_deny_all(controller: SandboxController, workspace: Workspace) -> None:
    sbx = _make(controller, workspace)
    try:
        # No network namespace connectivity → any egress attempt fails fast.
        result = run_async(
            lambda: sbx.exec(
                ["wget", "-T", "3", "-q", "-O", "-", "http://example.com"],
                limits=ResourceLimits(timeout_s=10),
            )
        )
        assert not result.ok
    finally:
        run_async(sbx.destroy)


# --- TITAN-SEC-044: no docker socket, no leaked credentials ------------------


def test_no_docker_socket_or_credentials(
    controller: SandboxController, workspace: Workspace
) -> None:
    sbx = _make(controller, workspace)
    try:
        sock = run_async(lambda: sbx.exec(["ls", "/var/run/docker.sock"]))
        assert not sock.ok  # never mounted
        # An env scan finds no secrets (we inject an empty environment).
        env = run_async(lambda: sbx.exec(["env"]))
        blob = env.stdout.lower()
        assert "secret" not in blob and "token" not in blob and "api_key" not in blob
        # No k8s serviceaccount token mount.
        sa = run_async(lambda: sbx.exec(["ls", "/var/run/secrets/kubernetes.io"]))
        assert not sa.ok
    finally:
        run_async(sbx.destroy)


# --- TITAN-INT-031: argv execution, hostile filenames are literal ------------


def test_hostile_argv_is_executed_literally(
    controller: SandboxController, workspace: Workspace
) -> None:
    (workspace.root / "safe.txt").write_text("intact")
    sbx = _make(controller, workspace)
    try:
        # A shell-metachar "filename" passed as an argv element is a literal
        # arg to echo, never interpreted — 'safe.txt' is untouched.
        result = run_async(lambda: sbx.exec(["echo", "; rm -rf /workspace/safe.txt"]))
        assert result.ok
        assert (workspace.root / "safe.txt").read_text() == "intact"
    finally:
        run_async(sbx.destroy)


# --- TITAN-SEC-042: cgroup containment (fork-bomb, memory-hog) ----------------


def test_fork_bomb_is_contained_by_pids_limit(
    controller: SandboxController, workspace: Workspace
) -> None:
    # A tight pids_limit means the fork storm hits the ceiling and the whole
    # thing is reaped at the timeout — no host impact, the daemon stays healthy.
    sbx = _make(controller, workspace, pids_limit=32)
    try:
        result = run_async(
            lambda: sbx.exec(["sh", "-c", ":(){ :|:& };:"], limits=ResourceLimits(timeout_s=6))
        )
        assert not result.ok  # killed / failed to spawn, never a clean success
        # The daemon is still responsive and the container is controllable.
        assert run_async(controller.ping) is True
    finally:
        run_async(sbx.destroy)


def test_memory_hog_is_killed_by_mem_limit(
    controller: SandboxController, workspace: Workspace
) -> None:
    # A hard 64 MiB cap OOM-kills a runaway allocation; the host is unaffected.
    sbx = _make(controller, workspace, memory_bytes=64 * 1024 * 1024)
    try:
        result = run_async(
            lambda: sbx.exec(
                ["sh", "-c", "x=x; while true; do x=$x$x; done"],
                limits=ResourceLimits(timeout_s=15),
            )
        )
        assert not result.ok
        # An OOM kill (well before the 15s window) is labeled oom_killed, NOT
        # timed_out — callers must be able to tell the two apart.
        assert result.oom_killed and not result.timed_out
        assert run_async(controller.ping) is True
    finally:
        run_async(sbx.destroy)


# --- TITAN-SEC-040: per-task isolation ---------------------------------------


def test_sequential_tasks_get_distinct_containers(controller: SandboxController) -> None:
    root = Path(tempfile.mkdtemp(prefix="titan-sbx-iso-"))
    manager = WorkspaceManager(root)
    project = uuid.uuid4()
    ws_a = manager.reset(project, uuid.uuid4())
    ws_b = manager.reset(project, uuid.uuid4())
    sbx_a = run_async(lambda: controller.create(uuid.uuid4(), ws_a))
    sbx_b = run_async(lambda: controller.create(uuid.uuid4(), ws_b))
    try:
        assert sbx_a.container_id != sbx_b.container_id  # never shared
        # Task A's workspace file is invisible to task B's sandbox.
        run_async(lambda: sbx_a.exec(["touch", "/workspace/only-a"]))
        seen_in_b = run_async(lambda: sbx_b.exec(["ls", "/workspace/only-a"]))
        assert not seen_in_b.ok
    finally:
        run_async(sbx_a.destroy)
        run_async(sbx_b.destroy)


# --- exec timeout ------------------------------------------------------------


def test_exec_timeout_is_enforced(controller: SandboxController, workspace: Workspace) -> None:
    sbx = _make(controller, workspace)
    try:
        result = run_async(lambda: sbx.exec(["sleep", "30"], limits=ResourceLimits(timeout_s=2)))
        assert result.timed_out and not result.ok
    finally:
        run_async(sbx.destroy)


def test_destroyed_sandbox_rejects_exec(
    controller: SandboxController, workspace: Workspace
) -> None:
    sbx = _make(controller, workspace)
    run_async(sbx.destroy)
    with pytest.raises(ToolError) as exc:
        run_async(lambda: sbx.exec(["echo", "hi"]))
    assert exc.value.code in {"SANDBOX_VIOLATION", "UPSTREAM_ERROR"}
