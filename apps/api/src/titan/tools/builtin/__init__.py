"""Builtin tool providers — the boot-time sources of the registry rebuild.

Each module exposes ``SPECS``; :func:`register_builtin` loads them all. Plugin
and MCP providers (PostgreSQL-backed) join this rebuild in their own phases
through the same :meth:`ToolRegistry.register` seam.
"""

from titan.tools.builtin import browser as browser_tools
from titan.tools.builtin import code as code_tools
from titan.tools.builtin import deploy as deploy_tools
from titan.tools.builtin import docker as docker_tools
from titan.tools.builtin import fs as fs_tools
from titan.tools.builtin import git as git_tools
from titan.tools.builtin import messaging as messaging_tools
from titan.tools.builtin import tasks as task_tools
from titan.tools.builtin import terminal as terminal_tools
from titan.tools.builtin import web as web_tools
from titan.tools.registry import ToolRegistry


def register_builtin(registry: ToolRegistry) -> None:
    for spec in (
        *task_tools.SPECS,
        *messaging_tools.SPECS,
        *fs_tools.SPECS,
        *terminal_tools.SPECS,
        *git_tools.SPECS,
        *code_tools.SPECS,
        *docker_tools.SPECS,
        *browser_tools.SPECS,
        *web_tools.SPECS,
        *deploy_tools.SPECS,
    ):
        registry.register(spec)


__all__ = ["register_builtin"]
