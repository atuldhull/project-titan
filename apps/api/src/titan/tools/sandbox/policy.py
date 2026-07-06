"""Sandbox hardening policy + per-call resource limits (Vol 7 §4.3, Vol 8 §5).

``SandboxPolicy`` is the full per-task container hardening (TITAN-SEC-041..044):
non-root uid 10001, ALL caps dropped, no-new-privileges, read-only rootfs with
only ``/workspace`` and ``/tmp`` writable, seccomp runtime-default, deny-all
network, and the §5 cgroup ceiling (CPU ≤ 2, mem ≤ 2 GiB, PIDs ≤ 256, disk ≤ 5
GiB, wall-clock ≤ task timeout). ``ResourceLimits`` is the §4.3 PER-CALL default
(1 vCPU / 1 GiB / 60 s) that a tool's registry entry may tighten but never
loosen past the task ceiling.
"""

from dataclasses import dataclass, field, replace

_GIB = 1024**3

# §5 per-task ceiling (TITAN-SEC-042). Admin policy may override; a per-call
# ResourceLimits is clamped to these.
TASK_MAX_NANO_CPUS = 2_000_000_000  # 2 vCPU (docker nano_cpus)
TASK_MAX_MEMORY_BYTES = 2 * _GIB
TASK_MAX_PIDS = 256
TASK_MAX_WORKSPACE_BYTES = 5 * _GIB
TASK_DEFAULT_WALL_CLOCK_S = 30 * 60  # 30 min

# §4.3 per-call default (1 vCPU / 1 GiB / 60 s).
CALL_DEFAULT_NANO_CPUS = 1_000_000_000
CALL_DEFAULT_MEMORY_BYTES = 1 * _GIB
CALL_DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ResourceLimits:
    """The compute budget for a single tool call inside the sandbox. Defaults
    are the §4.3 per-call allowance; a tool may tighten (never loosen) via its
    registry entry."""

    nano_cpus: int = CALL_DEFAULT_NANO_CPUS
    memory_bytes: int = CALL_DEFAULT_MEMORY_BYTES
    timeout_s: float = CALL_DEFAULT_TIMEOUT_S

    def clamped(self, ceiling: "SandboxPolicy") -> "ResourceLimits":
        """Never exceed the per-task ceiling (Admin-only override lives on the
        policy, not on model-influenced per-call values)."""
        return ResourceLimits(
            nano_cpus=min(self.nano_cpus, ceiling.nano_cpus),
            memory_bytes=min(self.memory_bytes, ceiling.memory_bytes),
            timeout_s=min(self.timeout_s, ceiling.wall_clock_s),
        )


@dataclass(frozen=True)
class SandboxPolicy:
    """Per-task container hardening (Vol 8 §5 diagram + §4.3 policy table)."""

    image: str = "alpine:3.20"
    user: str = "10001:10001"  # non-root (TITAN-SEC-041)
    read_only_rootfs: bool = True  # writable /workspace + /tmp only
    network_mode: str = "none"  # deny-all egress (TITAN-SEC-043)
    no_new_privileges: bool = True
    drop_all_capabilities: bool = True  # ALL caps dropped (TITAN-SEC-041)
    seccomp: str = "runtime-default"  # at least runtime-default strict
    # §5 cgroup ceiling (TITAN-SEC-042).
    nano_cpus: int = TASK_MAX_NANO_CPUS
    memory_bytes: int = TASK_MAX_MEMORY_BYTES
    pids_limit: int = TASK_MAX_PIDS
    workspace_bytes: int = TASK_MAX_WORKSPACE_BYTES
    wall_clock_s: float = TASK_DEFAULT_WALL_CLOCK_S
    # tmpfs mounts (size in bytes) for the writable-but-ephemeral paths.
    tmpfs: dict[str, int] = field(default_factory=lambda: {"/tmp": 256 * 1024 * 1024})
    # A hard ceiling on captured output per stream (TITAN-INT-031: 1 MiB).
    max_output_bytes: int = 1024 * 1024

    def security_opts(self) -> list[str]:
        opts: list[str] = []
        # Docker applies its built-in default seccomp profile when NO seccomp
        # option is given — that IS "runtime-default". Only emit an explicit
        # option for a custom profile (a JSON path) or to disable it.
        if self.seccomp and self.seccomp != "runtime-default":
            opts.append(f"seccomp={self.seccomp}")
        if self.no_new_privileges:
            opts.append("no-new-privileges")
        return opts

    def with_overrides(self, **changes: object) -> "SandboxPolicy":
        """Admin-policy override seam (TITAN-SEC-042 'overridable only by Admin
        policy'). Never called with model-influenced values."""
        return replace(self, **changes)  # type: ignore[arg-type]


__all__ = [
    "CALL_DEFAULT_MEMORY_BYTES",
    "CALL_DEFAULT_NANO_CPUS",
    "CALL_DEFAULT_TIMEOUT_S",
    "TASK_DEFAULT_WALL_CLOCK_S",
    "TASK_MAX_MEMORY_BYTES",
    "TASK_MAX_NANO_CPUS",
    "TASK_MAX_PIDS",
    "TASK_MAX_WORKSPACE_BYTES",
    "ResourceLimits",
    "SandboxPolicy",
]
