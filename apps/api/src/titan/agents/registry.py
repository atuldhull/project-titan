"""The in-process agent roster (TITAN-AGT-008).

:class:`AgentRegistry` is an immutable, validated view of the loaded roster,
built once at startup by :mod:`titan.agents.loader` and cached on
``app.state.agent_registry``. It is a read model: the runtime looks up a role's
contract by ``agent_id`` and lists the roster; it never mutates it (roster
changes are configuration edits + a reload, per TITAN-AGT-006).
"""

from collections.abc import Mapping

from titan.agents.definition import AgentDefinition


def _tier_rank(definition: AgentDefinition) -> int:
    """Sort key so listings read top-of-org first: CEO (0) → tier 3 → cross."""
    tier = definition.tier
    return tier if isinstance(tier, int) else 4  # cross-cutting roles sort last


class AgentRegistry:
    """An immutable, agent_id-keyed roster of :class:`AgentDefinition`.

    Deterministically ordered by tier then agent_id, so ``all()`` and the HTTP
    listing are stable across processes.
    """

    __slots__ = ("_by_id", "_ordered")

    def __init__(self, definitions: Mapping[str, AgentDefinition]) -> None:
        self._by_id: dict[str, AgentDefinition] = dict(definitions)
        self._ordered: tuple[AgentDefinition, ...] = tuple(
            sorted(self._by_id.values(), key=lambda d: (_tier_rank(d), d.agent_id))
        )

    def get(self, agent_id: str) -> AgentDefinition | None:
        """The contract for ``agent_id``, or None if the roster has no such role."""
        return self._by_id.get(agent_id)

    def all(self) -> tuple[AgentDefinition, ...]:
        """Every contract, tier-then-id ordered."""
        return self._ordered

    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)


__all__ = ["AgentRegistry"]
