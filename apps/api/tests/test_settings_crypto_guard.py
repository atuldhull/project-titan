"""The fail-closed guard on empty crypto keys outside local (Phase 68 security
review FIX A). An empty bus_hmac_key/field_encryption_key derives an insecure
fallback, so staging/production must refuse to boot rather than warn."""

import pytest
from pydantic import ValidationError

from titan.settings import Settings


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "bus_hmac_key": "set",
        "field_encryption_key": "set",
        "_env_file": None,
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_local_allows_empty_crypto_keys() -> None:
    # The dev conveniences (fixed dev HMAC key / ephemeral AES key) are local-only.
    _settings(environment="local", bus_hmac_key="", field_encryption_key="")


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_non_local_refuses_empty_bus_hmac_key(environment: str) -> None:
    with pytest.raises(ValidationError, match="TITAN_BUS_HMAC_KEY"):
        _settings(environment=environment, bus_hmac_key="")


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_non_local_refuses_empty_field_encryption_key(environment: str) -> None:
    with pytest.raises(ValidationError, match="TITAN_FIELD_ENCRYPTION_KEY"):
        _settings(environment=environment, field_encryption_key="")


def test_non_local_boots_when_both_keys_set() -> None:
    settings = _settings(environment="production")
    assert settings.bus_hmac_key == "set" and settings.field_encryption_key == "set"


def test_vault_age_key_stays_optional_in_production() -> None:
    # An env-backed vault (empty age key) is a supported prod mode (SEC-050); the
    # write API answers 503 rather than failing open, so it must NOT block boot.
    settings = _settings(environment="production", vault_age_key="")
    assert settings.vault_age_key == ""
