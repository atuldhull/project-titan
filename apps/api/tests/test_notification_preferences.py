"""Settings → Notifications: the §7.1 channel matrix (Phase 64).

The category axis has been real data since migration 0003; the channel axis did
not exist anywhere until now. The tests that matter are about the sparse-row
design — defaults are computed, not stored — because that is the part a later
change could quietly break without any endpoint returning an error.
"""

from typing import Any

import pytest

from titan.messaging.preferences import (
    CATEGORIES,
    CHANNELS,
    DEFAULT_CHANNELS,
    UNAVAILABLE_CHANNELS,
    CategoryPreference,
    CategoryUpdate,
    PreferencesOut,
    PreferencesUpdate,
    channels_for,
    validate,
)

_URL = "/api/v1/notification-preferences"


# --- pure -------------------------------------------------------------------


def test_the_defaults_are_exactly_the_spec_table() -> None:
    # Vol 4 §7.1's "Default channels" column, transcribed. If a later edit
    # drifts from the spec this is the thing that says so.
    assert DEFAULT_CHANNELS["approval"] == frozenset({"toast", "bell", "attention_queue"})
    assert DEFAULT_CHANNELS["failure"] == frozenset({"toast", "bell"})
    assert DEFAULT_CHANNELS["progress"] == frozenset({"bell"})
    assert DEFAULT_CHANNELS["activity"] == frozenset()  # "Feed only (muted by default)"
    assert DEFAULT_CHANNELS["security"] == frozenset({"toast", "bell", "attention_queue"})
    assert set(DEFAULT_CHANNELS) == set(CATEGORIES)


def test_an_unknown_category_is_rejected() -> None:
    # `notifications.category` is free-form Text with no CHECK constraint, so
    # this validation is the only thing between a typo and a preference row
    # that silently never matches anything.
    with pytest.raises(ValueError, match="unknown categor"):
        validate(PreferencesUpdate(categories=[CategoryUpdate(category="urgent", channels=[])]))


def test_an_unknown_channel_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown channel"):
        validate(
            PreferencesUpdate(
                categories=[CategoryUpdate(category="failure", channels=["carrier-pigeon"])]
            )
        )


def test_a_repeated_category_is_rejected() -> None:
    # Two rows for one category would make the effective matrix depend on which
    # write landed last, so it is refused rather than resolved arbitrarily.
    with pytest.raises(ValueError, match="more than once"):
        validate(
            PreferencesUpdate(
                categories=[
                    CategoryUpdate(category="failure", channels=["bell"]),
                    CategoryUpdate(category="failure", channels=["toast"]),
                ]
            )
        )


def test_an_undeliverable_channel_is_filtered_from_the_read() -> None:
    # `plugins` has no fan-out: no producer, no consumer. A preference naming it
    # is stored (so it starts working when delivery arrives) but never reported
    # as a channel that would actually deliver.
    prefs = PreferencesOut(
        categories=[
            CategoryPreference(category="failure", severity="high", channels=["bell", "plugins"])
        ]
    )
    assert channels_for(prefs, "failure") == frozenset({"bell"})
    assert "plugins" in UNAVAILABLE_CHANNELS
    assert "plugins" in CHANNELS


def test_an_unknown_category_falls_back_to_its_default() -> None:
    assert channels_for(PreferencesOut(), "approval") == DEFAULT_CHANNELS["approval"]


# --- API ---------------------------------------------------------------------


@pytest.mark.schema
class TestPreferencesApi:
    def test_a_user_with_no_rows_gets_the_spec_defaults(self, api: dict[str, Any]) -> None:
        body = api["client"].get(_URL, headers=api["token_for"]("viewer")).json()
        by_category = {c["category"]: c for c in body["categories"]}
        assert set(by_category) == set(CATEGORIES)
        assert sorted(by_category["failure"]["channels"]) == ["bell", "toast"]
        assert by_category["activity"]["channels"] == []
        # Every category is flagged as a default, not a stored choice.
        assert all(c["is_default"] for c in body["categories"])

    def test_the_matrix_reports_which_channels_actually_deliver(self, api: dict[str, Any]) -> None:
        body = api["client"].get(_URL, headers=api["token_for"]("admin")).json()
        assert body["unavailable_channels"] == ["plugins"]
        assert set(body["available_channels"]) == {"toast", "bell", "attention_queue"}

    def test_severity_comes_from_the_spec_table(self, api: dict[str, Any]) -> None:
        body = api["client"].get(_URL, headers=api["token_for"]("admin")).json()
        by_category = {c["category"]: c for c in body["categories"]}
        assert by_category["approval"]["severity"] == "critical"
        assert by_category["activity"]["severity"] == "low"

    def test_an_override_persists_and_is_marked_as_one(self, api: dict[str, Any]) -> None:
        saved = api["client"].put(
            _URL,
            headers=api["token_for"]("maintainer"),
            json={"categories": [{"category": "activity", "channels": ["bell"]}]},
        )
        assert saved.status_code == 200

        reread = api["client"].get(_URL, headers=api["token_for"]("maintainer")).json()
        activity = [c for c in reread["categories"] if c["category"] == "activity"][0]
        assert activity["channels"] == ["bell"]
        assert activity["is_default"] is False

    def test_setting_a_category_back_to_its_default_drops_the_row(
        self, api: dict[str, Any]
    ) -> None:
        # The row's absence is meaningful — it means "track the default". If a
        # user restores the default, they should resume tracking it rather than
        # be pinned to today's value forever.
        headers = api["token_for"]("owner")
        api["client"].put(
            _URL, headers=headers, json={"categories": [{"category": "failure", "channels": []}]}
        )
        assert [
            c
            for c in api["client"].get(_URL, headers=headers).json()["categories"]
            if c["category"] == "failure"
        ][0]["is_default"] is False

        api["client"].put(
            _URL,
            headers=headers,
            json={"categories": [{"category": "failure", "channels": ["toast", "bell"]}]},
        )
        failure = [
            c
            for c in api["client"].get(_URL, headers=headers).json()["categories"]
            if c["category"] == "failure"
        ][0]
        assert failure["is_default"] is True
        assert sorted(failure["channels"]) == ["bell", "toast"]

    def test_muting_a_category_entirely_is_allowed(self, api: dict[str, Any]) -> None:
        # An empty channel list is a real choice, not a missing value — which is
        # why the update replaces the whole matrix rather than patching it.
        headers = api["token_for"]("admin")
        api["client"].put(
            _URL, headers=headers, json={"categories": [{"category": "approval", "channels": []}]}
        )
        approval = [
            c
            for c in api["client"].get(_URL, headers=headers).json()["categories"]
            if c["category"] == "approval"
        ][0]
        assert approval["channels"] == []
        assert approval["is_default"] is False

    def test_one_users_preferences_do_not_leak_into_anothers(self, api: dict[str, Any]) -> None:
        api["client"].put(
            _URL,
            headers=api["token_for"]("admin"),
            json={"categories": [{"category": "progress", "channels": ["toast"]}]},
        )
        other = api["client"].get(_URL, headers=api["token_for"]("viewer")).json()
        progress = [c for c in other["categories"] if c["category"] == "progress"][0]
        assert progress["channels"] == ["bell"], "another user's override must not be visible"
        assert progress["is_default"] is True

    def test_an_unknown_channel_is_a_422(self, api: dict[str, Any]) -> None:
        response = api["client"].put(
            _URL,
            headers=api["token_for"]("admin"),
            json={"categories": [{"category": "failure", "channels": ["sms"]}]},
        )
        assert response.status_code == 422

    def test_an_unknown_category_is_a_422(self, api: dict[str, Any]) -> None:
        response = api["client"].put(
            _URL,
            headers=api["token_for"]("admin"),
            json={"categories": [{"category": "gossip", "channels": ["bell"]}]},
        )
        assert response.status_code == 422

    def test_an_unauthenticated_caller_is_rejected(self, api: dict[str, Any]) -> None:
        assert api["client"].get(_URL).status_code == 401

    def test_a_viewer_may_set_their_own_preferences(self, api: dict[str, Any]) -> None:
        # Self-scoped: the lowest-privilege role still configures its own
        # notifications. There is no permission string to hold.
        response = api["client"].put(
            _URL,
            headers=api["token_for"]("viewer"),
            json={"categories": [{"category": "security", "channels": ["bell"]}]},
        )
        assert response.status_code == 200
