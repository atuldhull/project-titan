"""SopsAgeStore — the age-encrypted YAML vault (Phase 68, TITAN-SEC-050). No DB;
uses a temp file. Proves ciphertext-at-rest and cross-process reload."""

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tests.conftest import run_async
from titan.vault import age
from titan.vault.drivers.sops_age import SopsAgeStore


@pytest.fixture
def identity() -> X25519PrivateKey:
    ident, _ = age.generate_identity()
    return age.parse_identity(ident)


def test_store_roundtrip_and_ciphertext_at_rest(tmp_path: Path, identity: X25519PrivateKey) -> None:
    path = tmp_path / "secrets.yaml.age"
    store = SopsAgeStore(identity, path)

    async def go() -> tuple[str, list[str], bytes]:
        assert await store.names() == []
        await store.put("GITHUB_TOKEN", "ghp_verysecret")
        await store.put("SLACK_BOT_TOKEN", "xoxb-abc")
        value = await store.get("GITHUB_TOKEN")
        names = await store.names()
        return value, names, path.read_bytes()

    value, names, blob = run_async(go)
    assert value == "ghp_verysecret"
    assert names == ["GITHUB_TOKEN", "SLACK_BOT_TOKEN"]
    # SEC-050: the file on disk is age ciphertext — no plaintext value or name.
    assert blob.startswith(b"age-encryption.org/v1")
    assert b"ghp_verysecret" not in blob
    assert b"GITHUB_TOKEN" not in blob


def test_missing_secret_raises_keyerror(tmp_path: Path, identity: X25519PrivateKey) -> None:
    store = SopsAgeStore(identity, tmp_path / "v.age")

    async def go() -> None:
        with pytest.raises(KeyError):
            await store.get("NOPE")

    run_async(go)


def test_second_process_sees_writes_via_mtime_reload(
    tmp_path: Path, identity: X25519PrivateKey
) -> None:
    path = tmp_path / "secrets.yaml.age"
    writer = SopsAgeStore(identity, path)
    reader = SopsAgeStore(identity, path)  # simulates the worker process

    async def go() -> tuple[str, bool]:
        await writer.put("K", "v1")
        seen = await reader.get("K")  # reader reloads on mtime change
        await writer.delete("K")
        deleted = False
        try:
            await reader.get("K")
        except KeyError:
            deleted = True
        return seen, deleted

    seen, deleted = run_async(go)
    assert seen == "v1"
    assert deleted is True
