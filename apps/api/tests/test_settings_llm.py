"""Settings → LLM Providers + Budgets (Phase 64; Vol 4 §6.13, TITAN-FRT-036).

Two halves:

* **Pure overlay mechanics** — no DB, no app. ``ConfigLoader`` merging, atomic
  writes, rollback on an invalid merge. These are where the sharp edges are.
* **The API**, schema-marked, including the phase's DoD: switching the default
  provider through Settings alone changes what the gateway resolves NEXT, with
  no restart and no second loader.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import text

from titan.llm.config import ConfigLoader, merge_overlay, overlay_path_for
from titan.llm.routing import ModelRouter
from titan.llm.settings_service import (
    LlmSettingsUpdate,
    apply_update,
    build_overlay,
    clear_overrides,
    describe,
    effective_profile,
    read_overlay,
    write_overlay,
)

_BASE = """\
providers:
  anthropic:
    api_key: sk-ant-secret-tail7f2c
    base_url: https://api.anthropic.com
  ollama:
    base_url: http://ollama:11434
model_aliases:
  coding-default:
    provider: anthropic
    model: claude-sonnet-4-5
    price: { input_per_mtok: 3.00, output_per_mtok: 15.00 }
    context_window: 200000
  local-default:
    provider: ollama
    model: qwen2.5-coder:7b
    context_window: 32768
routing:
  default: coding-default
budgets:
  per_project_usd: 25.00
"""


@pytest.fixture
def gateway_config(tmp_path: Path) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(_BASE, encoding="utf-8")
    return path


# --- overlay mechanics -------------------------------------------------------


def test_the_overlay_path_is_derived_from_the_base(tmp_path: Path) -> None:
    assert overlay_path_for(tmp_path / "models.yaml").name == "models.overrides.yaml"
    # The offline profile gets its OWN overlay rather than inheriting the cloud
    # profile's overrides, which would silently re-point a local-only run.
    assert overlay_path_for(tmp_path / "models.local.yaml").name == "models.local.overrides.yaml"


def test_a_missing_overlay_loads_the_base_untouched(gateway_config: Path) -> None:
    loader = ConfigLoader(gateway_config)
    assert loader.get().routing.default == "coding-default"


def test_the_overlay_overrides_only_what_it_names(gateway_config: Path) -> None:
    overlay_path_for(gateway_config).write_text(
        yaml.safe_dump({"routing": {"default": "local-default"}}), encoding="utf-8"
    )
    config = ConfigLoader(gateway_config).get()
    assert config.routing.default == "local-default"
    # Everything the overlay did not mention survives.
    assert set(config.model_aliases) == {"coding-default", "local-default"}
    assert config.budgets is not None and config.budgets.per_project_usd == Decimal("25.00")


def test_entries_merge_but_records_replace_whole() -> None:
    # An alias is a RECORD (provider + model + price + window). Half-merging one
    # would leave a price belonging to a different model.
    merged = merge_overlay(
        {
            "model_aliases": {
                "a": {"provider": "anthropic", "model": "old", "context_window": 1},
                "b": {"provider": "ollama", "model": "keep"},
            }
        },
        {"model_aliases": {"a": {"provider": "openai", "model": "new"}}},
    )
    assert merged["model_aliases"]["a"] == {"provider": "openai", "model": "new"}
    assert "context_window" not in merged["model_aliases"]["a"]
    assert merged["model_aliases"]["b"] == {"provider": "ollama", "model": "keep"}


def test_an_empty_overlay_file_is_not_an_error(gateway_config: Path) -> None:
    overlay_path_for(gateway_config).write_text("", encoding="utf-8")
    assert ConfigLoader(gateway_config).get().routing.default == "coding-default"


def test_an_overlay_naming_an_unknown_alias_is_rejected(gateway_config: Path) -> None:
    loader = ConfigLoader(gateway_config)
    assert loader.get().routing.default == "coding-default"  # prime the cache

    overlay_path_for(gateway_config).write_text(
        yaml.safe_dump({"routing": {"default": "does-not-exist"}}), encoding="utf-8"
    )
    # Validation runs on the MERGED document, so the last-good config keeps
    # serving rather than the gateway 500-ing on every call.
    assert loader.get().routing.default == "coding-default"


def test_a_first_load_of_a_broken_overlay_raises(gateway_config: Path) -> None:
    # Nothing good to fall back to — the error must surface, not be swallowed.
    overlay_path_for(gateway_config).write_text("routing: [not, a, mapping]", encoding="utf-8")
    with pytest.raises(ValueError):
        ConfigLoader(gateway_config).get()


def test_the_loader_reloads_when_only_the_overlay_changes(gateway_config: Path) -> None:
    # The base file's mtime never moves here. A loader that stamped only the
    # base would serve the pre-write config forever.
    loader = ConfigLoader(gateway_config)
    assert loader.get().routing.default == "coding-default"
    write_overlay(overlay_path_for(gateway_config), {"routing": {"default": "local-default"}})
    loader.invalidate()
    assert loader.get().routing.default == "local-default"


def test_write_overlay_is_atomic_and_leaves_no_temp_files(gateway_config: Path) -> None:
    target = overlay_path_for(gateway_config)
    write_overlay(target, {"routing": {"default": "local-default"}})
    assert target.is_file()
    leftovers = [p.name for p in gateway_config.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_the_written_overlay_says_what_wrote_it(gateway_config: Path) -> None:
    target = overlay_path_for(gateway_config)
    write_overlay(target, {"routing": {"default": "local-default"}})
    assert "GENERATED by Settings" in target.read_text(encoding="utf-8")


def test_build_overlay_folds_a_partial_update_over_the_existing_one() -> None:
    first = build_overlay({}, LlmSettingsUpdate(default_alias="local-default"))
    second = build_overlay(first, LlmSettingsUpdate(per_project_usd=Decimal("5")))
    # Changing the budget must not drop the routing override written earlier.
    assert second["routing"]["default"] == "local-default"
    assert second["budgets"]["per_project_usd"] == 5.0


def test_apply_update_rolls_back_an_invalid_write(gateway_config: Path) -> None:
    loader = ConfigLoader(gateway_config)
    apply_update(loader, LlmSettingsUpdate(default_alias="local-default"))

    with pytest.raises(ValueError):
        apply_update(loader, LlmSettingsUpdate(default_alias="nonexistent-alias"))

    # The rejected write left NOTHING behind: the previous override still
    # stands and the gateway still resolves.
    assert loader.get().routing.default == "local-default"
    assert read_overlay(loader.overlay_path)["routing"]["default"] == "local-default"


def test_clearing_overrides_restores_the_operators_file(gateway_config: Path) -> None:
    loader = ConfigLoader(gateway_config)
    apply_update(loader, LlmSettingsUpdate(default_alias="local-default"))
    assert loader.get().routing.default == "local-default"

    result = clear_overrides(loader)
    assert result.default_alias == "coding-default"
    assert result.overridden is False
    assert not loader.overlay_path.exists()
    # The hand-written base is untouched — comments and all.
    assert gateway_config.read_text(encoding="utf-8") == _BASE


def test_a_key_is_never_described_only_hinted(gateway_config: Path) -> None:
    described = describe(ConfigLoader(gateway_config).get(), overridden=False)
    anthropic = [p for p in described.providers if p.name == "anthropic"][0]
    assert anthropic.key_configured is True
    assert anthropic.key_hint == "7f2c"
    # The secret itself appears nowhere in the DTO.
    assert "sk-ant-secret-tail7f2c" not in described.model_dump_json()


def test_a_keyless_local_provider_is_not_reported_as_misconfigured(
    gateway_config: Path,
) -> None:
    # Ollama declares base_url and no api_key. A missing ${ENV_VAR} and a
    # deliberately keyless provider both arrive as empty, so without
    # key_required the UI would show "key missing" for a provider that needs
    # none.
    described = describe(ConfigLoader(gateway_config).get(), overridden=False)
    ollama = [p for p in described.providers if p.name == "ollama"][0]
    assert ollama.key_required is False
    assert ollama.key_configured is False


def test_the_agent_profile_is_what_re_points_an_agent_turn(gateway_config: Path) -> None:
    """TITAN-FRT-036, at the level that actually matters.

    An agent turn NEVER falls through to ``routing.default``:
    ``GatewayBinding.build_request`` always passes an explicit ``model_alias``
    resolved from model-policy (agent → tier → provider), so changing the
    default alias moves callers that supply no alias and nothing else. The
    load-bearing switch is the model-policy PROFILE, which is why Settings
    writes it and why ``build_runtime`` reads it back per build.

    This test is the one that would have caught the phase shipping a Settings
    screen whose provider change agents ignored.
    """
    from titan.agents.model_policy import parse_model_policy
    from titan.runtime.binding import GatewayBinding

    policy = parse_model_policy(
        """
tiers:
  standard: { provider: anthropic, model: claude-sonnet-4-5, temperature: 0.3 }
profiles:
  local-free:
    standard: { provider: ollama, model: qwen2.5-coder:7b, temperature: 0.3 }
agents:
  eng_backend: standard
"""
    )
    loader = ConfigLoader(gateway_config)

    # No profile written: the agent routes to Anthropic.
    before = GatewayBinding(policy, effective_profile(loader, None))
    assert before.selection("eng_backend").provider == "anthropic"
    assert before.alias_for(before.selection("eng_backend")) == "coding-default"

    apply_update(loader, LlmSettingsUpdate(agent_profile="local-free"))

    # A new binding — exactly what `build_runtime` constructs — now resolves
    # the SAME agent to Ollama, with no restart and no code change.
    after = GatewayBinding(policy, effective_profile(loader, None))
    assert after.selection("eng_backend").provider == "ollama"
    assert after.alias_for(after.selection("eng_backend")) == "local-default"


def test_clearing_the_agent_profile_falls_back_to_the_process_default(
    gateway_config: Path,
) -> None:
    loader = ConfigLoader(gateway_config)
    # Never written → the process setting stands.
    assert effective_profile(loader, "local-free") == "local-free"

    # Explicitly cleared → None, which is DISTINCT from "never set".
    apply_update(loader, LlmSettingsUpdate(agent_profile=""))
    assert effective_profile(loader, "local-free") is None


def test_the_profile_does_not_leak_into_the_gateway_document(
    gateway_config: Path,
) -> None:
    # `GatewayConfig` is extra="forbid", so an unstripped top-level key would
    # make the whole overlay fail validation and silently revert to last-good.
    loader = ConfigLoader(gateway_config)
    apply_update(
        loader, LlmSettingsUpdate(agent_profile="local-free", default_alias="local-default")
    )
    assert loader.get().routing.default == "local-default"


def test_switching_the_default_changes_what_routing_resolves(gateway_config: Path) -> None:
    """Settings alone re-points the GATEWAY default (callers with no alias)."""
    loader = ConfigLoader(gateway_config)
    before = ModelRouter(loader.get()).resolve_chat(model_alias=None, agent_id=None)
    assert before.provider == "anthropic"

    apply_update(loader, LlmSettingsUpdate(default_alias="local-default"))

    after = ModelRouter(loader.get()).resolve_chat(model_alias=None, agent_id=None)
    assert after.provider == "ollama"
    assert after.model_id == "qwen2.5-coder:7b"


def test_a_second_process_picks_the_change_up_by_mtime(gateway_config: Path) -> None:
    """No restart, and no shared in-memory state: this is what makes the change
    reach the WORKER and not only the API process that served the write."""
    writer = ConfigLoader(gateway_config)
    reader = ConfigLoader(gateway_config)  # stands in for the worker
    assert reader.get().routing.default == "coding-default"

    apply_update(writer, LlmSettingsUpdate(default_alias="local-default"))

    # The reader was never invalidated — it must notice via the overlay's mtime
    # alone. A loader that stat'd only the base file would fail here.
    assert reader.get().routing.default == "local-default"


# --- the HTTP surface --------------------------------------------------------


@pytest.mark.schema
class TestLlmSettingsApi:
    def test_a_viewer_cannot_read_org_llm_settings(self, api: dict[str, Any]) -> None:
        # `settings` is carved into SENSITIVE_RESOURCES, so the `*:read` that
        # viewer AND every agent carries must not reach it.
        response = api["client"].get("/api/v1/settings/llm", headers=api["token_for"]("viewer"))
        assert response.status_code == 403

    def test_an_admin_can_read_them(self, api: dict[str, Any]) -> None:
        response = api["client"].get("/api/v1/settings/llm", headers=api["token_for"]("admin"))
        assert response.status_code == 200
        body = response.json()
        assert body["default_alias"]
        assert isinstance(body["providers"], list)

    def test_no_provider_key_appears_in_the_response_body(self, api: dict[str, Any]) -> None:
        response = api["client"].get("/api/v1/settings/llm", headers=api["token_for"]("owner"))
        raw = response.text
        for provider in response.json()["providers"]:
            # Only a four-character hint is ever sent.
            assert len(provider["key_hint"] or "") <= 4
        assert "sk-ant-" not in raw and "sk-proj-" not in raw

    def test_a_maintainer_cannot_write_them(self, api: dict[str, Any]) -> None:
        response = api["client"].put(
            "/api/v1/settings/llm",
            headers=api["token_for"]("maintainer"),
            json={"default_alias": "local-default"},
        )
        assert response.status_code == 403

    def test_an_unknown_alias_is_a_422_not_a_500(self, api: dict[str, Any]) -> None:
        response = api["client"].put(
            "/api/v1/settings/llm",
            headers=api["token_for"]("admin"),
            json={"default_alias": "no-such-alias"},
        )
        assert response.status_code == 422

    def test_an_unknown_field_is_rejected(self, api: dict[str, Any]) -> None:
        # extra="forbid": a typo'd key must not be silently dropped, leaving a
        # 200 that changed nothing.
        response = api["client"].put(
            "/api/v1/settings/llm",
            headers=api["token_for"]("admin"),
            json={"defualt_alias": "local-default"},
        )
        assert response.status_code == 422

    def test_a_negative_budget_is_rejected(self, api: dict[str, Any]) -> None:
        response = api["client"].put(
            "/api/v1/settings/llm",
            headers=api["token_for"]("admin"),
            json={"per_project_usd": -1},
        )
        assert response.status_code == 422

    def test_testing_a_provider_needs_write_not_read(self, api: dict[str, Any]) -> None:
        """A test call spends real money and writes a ledger row, so it is a
        mutation however much it reads like one.

        No SEEDED role holds `settings:read` WITHOUT `settings:write` — both
        live only in the owner and admin bundles — so a seeded token could not
        tell the two guards apart, and asserting a plain viewer gets 403 would
        prove only that viewers lack `settings:*` entirely. A read-only role is
        seeded here so the distinction is genuinely exercised.
        """
        import json as _json

        from tests.conftest import run_async
        from titan.db import uuid7

        async def seed_reader() -> None:
            async with api["engine"].begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO roles (id, name, permissions) VALUES (:id, :n, :p) "
                        "ON CONFLICT (name) DO UPDATE SET permissions = :p"
                    ),
                    {"id": uuid7(), "n": "settings-reader", "p": _json.dumps(["settings:read"])},
                )

        run_async(seed_reader)
        read_only = api["token_for"]("viewer", roles=["settings-reader"])

        assert api["client"].get("/api/v1/settings/llm", headers=read_only).status_code == 200, (
            "settings:read alone must reach the READ"
        )
        assert (
            api["client"]
            .post(
                "/api/v1/settings/llm:test",
                headers=read_only,
                json={"alias": "coding-default"},
            )
            .status_code
            == 403
        ), "settings:read alone must NOT reach the live round-trip"

    def test_testing_an_unknown_alias_is_a_422(self, api: dict[str, Any]) -> None:
        response = api["client"].post(
            "/api/v1/settings/llm:test",
            headers=api["token_for"]("admin"),
            json={"alias": "not-an-alias"},
        )
        assert response.status_code == 422

    def test_switching_the_default_provider_over_http_repoints_the_gateway(
        self, api: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE DoD (TITAN-FRT-036): Anthropic → Ollama through Settings alone.

        The loader is redirected at a temp copy first, so the test never writes
        into the repository's real ``config/`` — a test that mutated the
        operator's gateway config as a side effect would be a far worse bug
        than the one it is guarding.
        """
        base = tmp_path / "models.yaml"
        base.write_text(_BASE, encoding="utf-8")
        gateway = api["client"].app.state.llm_gateway
        monkeypatch.setattr(gateway, "_loader", ConfigLoader(base))

        before = api["client"].get("/api/v1/settings/llm", headers=api["token_for"]("admin")).json()
        assert before["default_alias"] == "coding-default"
        assert before["overridden"] is False
        # What an agent's next turn would resolve, right now.
        assert (
            ModelRouter(gateway.config_loader.get())
            .resolve_chat(model_alias=None, agent_id=None)
            .provider
            == "anthropic"
        )

        saved = api["client"].put(
            "/api/v1/settings/llm",
            headers=api["token_for"]("admin"),
            json={"default_alias": "local-default"},
        )
        assert saved.status_code == 200
        assert saved.json()["default_alias"] == "local-default"
        assert saved.json()["overridden"] is True

        # No restart, no re-created gateway: the SAME live object now routes to
        # Ollama, which is what "takes effect without redeploying" means.
        resolved = ModelRouter(gateway.config_loader.get()).resolve_chat(
            model_alias=None, agent_id=None
        )
        assert resolved.provider == "ollama"
        assert resolved.model_id == "qwen2.5-coder:7b"

        # And it survives a fresh read through HTTP.
        after = api["client"].get("/api/v1/settings/llm", headers=api["token_for"]("admin")).json()
        assert after["default_alias"] == "local-default"

    def test_the_api_tests_never_write_the_repos_real_config(self) -> None:
        # A belt-and-braces guard: every write-path test above redirects the
        # loader at a temp file. If one ever stops doing so, this fails loudly
        # rather than leaving a stray override in the working tree that the
        # next developer's gateway would silently obey.
        from titan.llm.config import resolve_config_path

        real = resolve_config_path("config/models.yaml")
        assert not overlay_path_for(real).exists(), (
            f"{overlay_path_for(real)} was created by the test suite"
        )
