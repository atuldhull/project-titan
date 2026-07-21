"""The env-backed get-by-reference driver (the Phase-52 default).

Secrets come from the process environment and are never persisted by TITAN
(SEC-050). This is a READ-only provider — env is set externally (by the age
vault at boot in production, §6.3), so it cannot ``put``/``delete`` and is not a
:class:`~titan.vault.provider.SecretsStore`.
"""

import os

#: Env prefix a plugin secret resolves from, e.g. JIRA_API_TOKEN →
#: TITAN_PLUGIN_SECRET_JIRA_API_TOKEN (falling back to the bare name).
ENV_PREFIX = "TITAN_PLUGIN_SECRET_"


class EnvSecretsProvider:
    """Resolves a secret from the process environment (``TITAN_PLUGIN_SECRET_<NAME>``
    then the bare name). Production swaps in the age/Vault store behind the same
    Protocol (configuration-only)."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._env = environ if environ is not None else dict(os.environ)

    async def get(self, name: str) -> str:
        for key in (f"{ENV_PREFIX}{name}", name):
            value = self._env.get(key)
            if value:
                return value
        raise KeyError(name)


__all__ = ["ENV_PREFIX", "EnvSecretsProvider"]
