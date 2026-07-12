"""Unit tests for the hermeticity gate (TITAN-WFL-031, Phase 46).

Pins the true positives the gate must catch and the false positives it must not,
because an over-eager hermeticity gate is one a team disables — and a disabled
gate enforces nothing.
"""

from titan.ci_kit.gates import hermeticity


def _rules(source: str) -> list[str]:
    return [f.message for f in hermeticity.scan_source(source, "tests/t.py")]


def test_flags_real_network_calls() -> None:
    assert _rules("import requests\ndef test_x():\n    requests.get('http://x')\n")
    assert _rules("import urllib.request\ndef test_x():\n    urllib.request.urlopen('http://x')\n")
    assert _rules("import socket\ndef test_x():\n    socket.create_connection(('h', 80))\n")


def test_flags_external_url_literals_but_not_local() -> None:
    assert _rules("URL = 'https://api.stripe.com/charges'\n")
    # localhost / 127.0.0.1 / example.com are hermetic (containers, fixtures).
    assert _rules("URL = 'http://localhost:8000/health'\n") == []
    assert _rules("URL = 'http://127.0.0.1:5432'\n") == []
    assert _rules("URL = 'https://example.com/'\n") == []


def test_flags_unseeded_randomness_and_wall_clock() -> None:
    assert _rules("import random\ndef test_x():\n    random.random()\n")
    assert _rules("import random\ndef test_x():\n    random.randint(1, 9)\n")
    assert _rules("from datetime import datetime\ndef test_x():\n    datetime.now()\n")
    assert _rules("import time\ndef test_x():\n    time.time()\n")
    assert _rules("import uuid\ndef test_x():\n    uuid.uuid1()\n")  # time+MAC based


def test_does_not_flag_seeded_rng_or_uuid4() -> None:
    # A seeded local generator is the CORRECT pattern; it must not be flagged.
    seeded = "import random\ndef test_x():\n    rng = random.Random(0)\n    rng.random()\n"
    assert _rules(seeded) == []
    # uuid4 is random but ubiquitous for unique ids and rarely an assertion flake.
    assert _rules("import uuid\ndef test_x():\n    uuid.uuid4()\n") == []


def test_resolves_from_imports_and_aliases() -> None:
    # from-import: the callee is a bare Name, not module.attr.
    assert _rules("from random import random\ndef test_x():\n    random()\n")
    assert _rules("from time import time\ndef test_x():\n    time()\n")
    assert _rules("from random import randint as ri\ndef test_x():\n    ri(1, 9)\n")
    # aliased module import.
    assert _rules("import requests as r\ndef test_x():\n    r.get('http://svc')\n")
    # the `from datetime import datetime` idiom -> datetime.now() is the class method.
    assert _rules("from datetime import datetime\ndef test_x():\n    datetime.now()\n")
    assert _rules("from datetime import datetime as dt\ndef test_x():\n    dt.now()\n")


def test_flags_httpx_and_aiohttp() -> None:
    # httpx is a scaffold dependency; a call with a NON-literal URL still fires.
    assert _rules("import httpx\ndef test_x():\n    httpx.get(url)\n")
    assert _rules("from httpx import get\ndef test_x():\n    get(url)\n")


def test_fstring_local_url_prefix_is_not_flagged() -> None:
    # f"https://{host}/health" -> the constant prefix is exactly "https://" with
    # no host char; it must NOT be flagged (a real false-positive otherwise).
    assert hermeticity._EXTERNAL_URL.search("https://") is None
    assert _rules('def test_x():\n    url = f"https://{host}:{port}/health"\n') == []


def test_localhost_lookalike_is_still_flagged() -> None:
    # https://localhost.evil.com must NOT slip through the localhost exclusion.
    assert _rules("URL = 'https://localhost.evil.com/steal'\n")


def test_survives_a_syntax_error() -> None:
    assert hermeticity.scan_source("def broken(:\n", "tests/t.py") == []
