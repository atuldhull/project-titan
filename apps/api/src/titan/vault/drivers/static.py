"""An in-memory secret store — used by tests and as the substrate an operator's
unlocked age file decrypts into.

Implements the full :class:`~titan.vault.provider.SecretsStore` (get/put/delete/
names) so the vault service can rotate against it without any file or crypto —
the SOPS+age store composes ONE of these with the age file (decrypt→this on
boot, this→encrypt on write).
"""


class StaticSecretsProvider:
    """An explicit in-memory name→value map."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    async def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError as exc:
            raise KeyError(name) from exc

    async def put(self, name: str, value: str) -> None:
        self._values[name] = value

    async def delete(self, name: str) -> None:
        self._values.pop(name, None)

    async def names(self) -> list[str]:
        return sorted(self._values)


__all__ = ["StaticSecretsProvider"]
