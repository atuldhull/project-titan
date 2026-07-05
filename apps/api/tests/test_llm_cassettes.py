"""Cassette harness tests (Phase 16) — record → scrub → replay round-trip.

Proves the recorder captures replayable interactions, that credentials never
enter a cassette (secrets scrubbed at record time, item 2), and that replay is
deterministic and order-checked.
"""

import json
from pathlib import Path

import httpx
import pytest

from ._cassettes import CassetteReplayTransport, RecordingTransport, load_cassette
from .conftest import run_async  # noqa: I001  (local import grouping)

_SECRET = "AIzaSy-super-secret-key"


def _fake_provider(request: httpx.Request) -> httpx.Response:
    # A stand-in "live" provider; asserts the caller actually sent credentials.
    assert request.url.params.get("key") == _SECRET
    assert request.headers.get("authorization") == f"Bearer {_SECRET}"
    return httpx.Response(200, json={"ok": True, "echo": request.url.path})


def test_recording_scrubs_secrets_and_replays(tmp_path: Path) -> None:
    recorder = RecordingTransport(httpx.MockTransport(_fake_provider))

    async def record() -> httpx.Response:
        async with httpx.AsyncClient(transport=recorder) as client:
            return await client.post(
                f"https://api.example.com/v1/models/m:generateContent?key={_SECRET}",
                headers={"authorization": f"Bearer {_SECRET}"},
                json={"contents": []},
            )

    resp = run_async(record)
    assert resp.status_code == 200

    # The recorded interaction stored no secret: the ?key= is scrubbed, and only
    # method + path (no headers/body) are kept.
    interaction = recorder.interactions[0]
    assert "key=" not in interaction["request"]["path"]
    assert _SECRET not in json.dumps(recorder.interactions)

    # Persist + reload → the on-disk cassette also carries no secret.
    cassette_path = tmp_path / "scrubbed.json"
    recorder.dump(cassette_path, provider="example")
    assert _SECRET not in cassette_path.read_text(encoding="utf-8")

    # Replay reproduces the recorded response deterministically, keyless.
    replay = CassetteReplayTransport(
        json.loads(cassette_path.read_text(encoding="utf-8"))["interactions"]
    )

    async def replay_call() -> httpx.Response:
        async with httpx.AsyncClient(transport=replay) as client:
            return await client.post("https://api.example.com/v1/models/m:generateContent")

    replayed = run_async(replay_call)
    assert replayed.status_code == 200
    assert replayed.json()["ok"] is True


def test_replay_exhaustion_raises(tmp_path: Path) -> None:
    replay = CassetteReplayTransport([])

    async def call() -> None:
        async with httpx.AsyncClient(transport=replay) as client:
            await client.get("https://x/anything")

    with pytest.raises(AssertionError, match="cassette exhausted"):
        run_async(call)


def test_committed_cassettes_are_wellformed() -> None:
    for provider in ("anthropic", "openai", "google", "ollama"):
        transcript = load_cassette(f"{provider}.transcript")
        assert len(transcript) == 2  # two chat turns
        stream = load_cassette(f"{provider}.stream")
        assert "sse" in stream[0]["response"]
