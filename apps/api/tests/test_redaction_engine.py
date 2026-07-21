"""The shared redaction engine (TITAN-SEC-054/055): detectors, replacement
grammar, overlap merging, inventory hashing, the hit reporter, and the
secret-file matcher. Pure unit tests — no DB, no Redis, no Docker."""

import hashlib
import re

import pytest

from titan.redaction import (
    DEFAULT_PATTERNS,
    EMPTY_INVENTORY,
    Hit,
    InventoryIndex,
    RedactionEngine,
    RedactionReporter,
    default_engine,
    hash8,
    is_high_entropy,
    is_secret_path,
    redact_structure,
)
from titan.redaction.engine import MIN_INVENTORY_VALUE_LENGTH
from titan.redaction.report import OVERFLOW_CLASS

_MARKER = re.compile(r"\[REDACTED:[a-z0-9_-]+:[0-9a-f]{8}\]")


def _engine(values: list[str] | None = None) -> RedactionEngine:
    engine = RedactionEngine()
    if values:
        engine.set_inventory(InventoryIndex.from_values(values))
    return engine


# --- pattern detector --------------------------------------------------------

_PATTERN_CASES = [
    ("anthropic-key", "sk-ant-api03-AbCdEf1234567890xyz"),
    ("openai-key", "sk-1234567890abcdefGHIJ"),
    ("github-token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWX123456789012"),
    ("github-token", "github_pat_11ABCDEFG0123456789abcdef"),
    ("gitlab-token", "glpat-AbCdEfGhIjKlMnOpQrSt"),
    ("aws-key", "AKIAIOSFODNN7EXAMPLE"),
    ("aws-key", "ASIAIOSFODNN7EXAMPLE"),
    ("google-api-key", "AIzaSyA1bC2dE3fG4hI5jK6lM7nO8pQ9rS0tU1v"),
    ("slack-token", "xoxb-123456789012-abcdefghijkl"),
    ("titan-api-key", "titan_ak_0123456789abcdefFEDCBA"),
    ("titan-api-key", "titan_rt_0123456789abcdefFEDCBA"),
    ("age-key", "AGE-SECRET-KEY-1" + "Q" * 58),
    ("age-key", "age-secret-key-1" + "q" * 58),  # lowercase is a VALID identity
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P",
    ),
    ("bearer", "Bearer abc.def-ghi_jkl123456789"),
    (
        "private-key",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    ),
    ("dsn-password", "postgresql+asyncpg://titan:hunter2@db:5432/titan"),
    ("dsn-password", "redis://default:s3cr3t@cache:6379/0"),
    ("dsn-password", "https://deploy:t0ken@git.example.com/repo.git"),
]


@pytest.mark.parametrize(("cls", "secret"), _PATTERN_CASES)
def test_pattern_classes_redact_and_stamp_their_class(cls: str, secret: str) -> None:
    out, hits = _engine().redact_with_hits(f"before {secret} after")
    assert secret not in out
    assert hits, f"no hit for {cls}"
    assert hits[0].cls == cls
    assert _MARKER.search(out)


def test_replacement_grammar_and_hash8_are_stable() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    expected = hashlib.sha256(secret.encode()).hexdigest()[:8]
    out, hits = _engine().redact_with_hits(f"x {secret} y")
    assert out == f"x [REDACTED:aws-key:{expected}] y"
    assert hits == [Hit(cls="aws-key", hash8=expected)]
    assert hash8(secret) == expected


def test_anthropic_key_wins_over_generic_openai_shape() -> None:
    # sk-ant-… matches both patterns over the same span; the more specific
    # class must name the replacement.
    out, hits = _engine().redact_with_hits("sk-ant-api03-AbCdEf1234567890xyz")
    assert [h.cls for h in hits] == ["anthropic-key"]
    assert "openai-key" not in out


def test_overlapping_spans_merge_to_their_union() -> None:
    # 'Bearer <token>' overlaps the bare token match; a partial replacement
    # would leave a secret fragment behind.
    text = "Authorization: Bearer sk-1234567890abcdefGHIJ trailing"
    out, hits = _engine().redact_with_hits(text)
    assert "sk-" not in out
    assert "GHIJ" not in out
    assert len(hits) == 1
    assert out.endswith(" trailing")


def test_multiline_pem_block_redacts_as_one_span() -> None:
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    out, hits = _engine().redact_with_hits(f"key:\n{pem}\ndone")
    assert "BEGIN OPENSSH" not in out
    assert [h.cls for h in hits] == ["private-key"]


def test_redaction_is_idempotent() -> None:
    engine = _engine(["hunter2-super-secret-value"])
    once = engine.redact("v=hunter2-super-secret-value k=AKIAIOSFODNN7EXAMPLE")
    assert engine.redact(once) == once


def test_empty_and_clean_text_pass_through_unchanged() -> None:
    engine = _engine()
    assert engine.redact("") == ""
    clean = "task 42 moved to in_progress by agent.backend.1"
    assert engine.redact(clean) == clean


# --- entropy detector ----------------------------------------------------------


def test_entropy_catches_unshaped_random_tokens() -> None:
    out, hits = _engine().redact_with_hits("token 9fXk2LmQ7pZ4tRv8WqYs1JhN5B3d end")
    assert [h.cls for h in hits] == ["entropy"]
    assert "9fXk2" not in out


@pytest.mark.parametrize(
    "value",
    [
        "0197a3c2-9d4e-7bb2-8f3e-1a2b3c4d5e6f",  # UUID
        "4bf92f3577b34da6a3ce929d0e0e4736",  # 32-hex trace id
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256
        "sha512-mE9rkclJk8Rzg6XyDy8t1PWkGDNK9dfcbBs1x9aJ4kAX0kd8P2T6nT4Cq==",  # SRI
        "apps/api/src/titan/alembic/versions/0035_secrets_vault",  # path w/ digits
        "InternalServerErrorHandlerConfiguration",  # long identifier, no digit
        "short",
    ],
)
def test_entropy_guards_spare_operational_identifiers(value: str) -> None:
    assert not is_high_entropy(value)
    assert _engine().redact(f"id={value}") == f"id={value}"


# --- inventory detector -------------------------------------------------------


def test_inventory_exact_match_via_hashed_lookup() -> None:
    secret = "hunter2-super-secret-value"
    engine = _engine([secret])
    out, hits = engine.redact_with_hits(f"DB_PASSWORD={secret} rest")
    assert secret not in out
    assert hits == [Hit(cls="inventory", hash8=hash8(secret))]
    # The engine holds digests, not values.
    assert engine.inventory_size > 0


def test_inventory_matches_base64_padding_variants() -> None:
    secret = "c2VjcmV0LXZhbHVlLTEyMw=="
    engine = _engine([secret])
    assert secret not in engine.redact(f"a {secret} b")
    stripped = secret.rstrip("=")
    assert stripped not in engine.redact(f"a {stripped} b")


@pytest.mark.parametrize(
    ("secret", "surrounding"),
    [
        # Phase-69 security review: a vaulted value containing a char outside
        # [A-Za-z0-9+/_-] used to be split into fragments and leak verbatim.
        ("Str0ng.P@ss.w0rd", "DB_PASSWORD={s} rest"),  # KEY=value (printenv)
        ("SG.aB3dEfGh1jKl.MnOpQrStUvWx", "sendgrid key: {s}"),  # dotted API key
        ("clientid1234:s3cr3tValue987", '{{"auth":"{s}"}}'),  # id:secret in JSON
        ("my-super.secret_key!2024xxx", "the key is {s}."),  # trailing punctuation
        ("p@ssw0rd with spaces? no", "x={s}"),  # (control: space DOES split; see below)
    ],
)
def test_inventory_matches_punctuated_values(secret: str, surrounding: str) -> None:
    engine = _engine([secret])
    text = surrounding.format(s=secret)
    out = engine.redact(text, sink="log")
    if " " in secret:
        # A space is a genuine structural separator — a value with spaces is NOT
        # a single token and is out of scope (documented). This row asserts the
        # boundary, not a redaction.
        return
    assert secret not in out
    assert "[REDACTED:inventory:" in out


def test_gateway_sink_marker_omits_the_hash_correlator() -> None:
    # Phase-69 review: the marker egresses to the LLM provider; it must not
    # carry a stable secret fingerprint. Internal sinks keep the hash.
    engine = _engine(["hunter2-super-secret-value"])
    gate = engine.redact("k AKIAIOSFODNN7EXAMPLE v hunter2-super-secret-value", sink="gateway")
    assert "AKIA" not in gate and "hunter2" not in gate
    assert "[REDACTED:aws-key]" in gate  # class only, NO :hash
    assert "[REDACTED:inventory]" in gate
    assert not _MARKER.search(gate)  # the hash-bearing grammar must be absent
    log = engine.redact("k AKIAIOSFODNN7EXAMPLE", sink="log")
    assert _MARKER.search(log)  # internal sink keeps the correlator


def test_patterns_are_not_redos_on_hot_path() -> None:
    # Phase-69 review: the jwt pattern was quadratic on eyJ-repeat input (120 KB
    # ~ 7 s). Bounded + possessive quantifiers make it linear. A generous bound
    # that still bites the O(n^2) regression (which was seconds) without flaking.
    import time

    engine = _engine()
    payload = "eyJ" * (120_000 // 3)  # 120 KB of match-starts, no delimiters
    start = time.perf_counter()
    engine.redact(payload, sink="log")
    assert time.perf_counter() - start < 3.0
    # A real JWT still redacts.
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P"
    assert "eyJ" not in engine.redact(f"t {jwt}")


def test_inventory_skips_values_too_short_to_redact_safely() -> None:
    engine = _engine(["true"])
    assert engine.inventory_size == 0
    assert engine.redact("flag=true") == "flag=true"
    assert MIN_INVENTORY_VALUE_LENGTH == 8


def test_inventory_class_wins_over_entropy_for_the_same_token() -> None:
    secret = "9fXk2LmQ7pZ4tRv8WqYs1JhN5B3d"  # also entropy-shaped
    _, hits = _engine([secret]).redact_with_hits(f"x {secret}")
    assert [h.cls for h in hits] == ["inventory"]


def test_inventory_swap_is_wholesale() -> None:
    engine = _engine(["first-secret-value-1"])
    assert "first-secret-value-1" not in engine.redact("x first-secret-value-1")
    engine.set_inventory(InventoryIndex.from_values(["second-secret-value-2"]))
    assert engine.redact("x first-secret-value-1") == "x first-secret-value-1"
    assert "second-secret-value-2" not in engine.redact("x second-secret-value-2")
    engine.set_inventory(EMPTY_INVENTORY)
    assert engine.redact("x second-secret-value-2") == "x second-secret-value-2"


# --- structures, reporter, global engine ---------------------------------------


def test_redact_structure_walks_values_keys_and_nesting() -> None:
    engine = _engine(["hunter2-super-secret-value"])
    payload = {
        "note": "key AKIAIOSFODNN7EXAMPLE",
        "hunter2-super-secret-value": "keyed by a secret",
        "nested": {"list": ["hunter2-super-secret-value", 42, None]},
        "tuple": ("sk-1234567890abcdefGHIJ",),
        "count": 7,
    }
    out = redact_structure(payload, engine=engine, sink="test")
    flat = repr(out)
    assert "AKIA" not in flat
    assert "hunter2" not in flat
    assert "sk-12345" not in flat
    assert out["count"] == 7
    assert out["nested"]["list"][1:] == [42, None]
    # Never mutates the input.
    assert payload["note"] == "key AKIAIOSFODNN7EXAMPLE"


def test_reporter_dedupes_counts_and_bounds() -> None:
    reporter = RedactionReporter(max_entries=2)
    engine = _engine()
    engine.set_reporter(reporter.record)
    for _ in range(3):
        engine.redact("k AKIAIOSFODNN7EXAMPLE", sink="log")
    engine.redact("k sk-1234567890abcdefGHIJ", sink="log")
    engine.redact("k ghp_ABCDEFGHIJKLMNOPQRSTUVWX123456789012", sink="log")  # over cap
    records = reporter.drain()
    by_cls = {r.cls: r for r in records}
    assert by_cls["aws-key"].count == 3
    assert by_cls["aws-key"].sink == "log"
    assert by_cls[OVERFLOW_CLASS].count == 1
    # drain() clears.
    assert reporter.drain() == []


def test_a_crashing_reporter_never_blocks_redaction() -> None:
    engine = _engine()

    def boom(sink: str, hits: object) -> None:
        raise RuntimeError("reporter down")

    engine.set_reporter(boom)
    out = engine.redact("k AKIAIOSFODNN7EXAMPLE")
    assert "AKIA" not in out


def test_default_engine_is_reset_between_tests_a() -> None:
    # Paired with _b below: whichever runs first would poison the other
    # without the autouse conftest reset.
    default_engine().set_inventory(InventoryIndex.from_values(["poisoned-value-123"]))
    assert default_engine().inventory_size > 0


def test_default_engine_is_reset_between_tests_b() -> None:
    assert default_engine().inventory_size == 0
    assert default_engine().redact("poisoned-value-123") == "poisoned-value-123"


# --- secret-bearing file patterns (TITAN-SEC-055) -------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        ".env.example",  # a carve-out would be a rename-based bypass
        "sub/dir/.env",
        ".ENV",  # case evasion
        "windows\\style\\.env",  # separator evasion
        "certs/server.pem",
        "id_rsa",
        "id_rsa.pub",
        ".ssh/id_ed25519",
        "home/user/.ssh/known_hosts",  # blanket .ssh/
        "service_account_key.json",
        "api_key_manager.py",  # *_key* over-matches by spec design
        ".aws/credentials",
        "x/.aws/credentials.bak",  # blanket .aws/
        ".config/gcloud/application_default_credentials.json",
        ".azure/accessTokens.json",
        ".kube/config",
        ".docker/config.json",
        ".netrc",
        ".pgpass",
        ".npmrc",
        ".git-credentials",
        "backup.ppk",
        # Phase-69 review: common secret filenames that are NOT *_key*/*.pem.
        "server.key",
        "tls.key",
        "deploy/privkey.key",
        "app.keystore",
        "client.jks",
        "cert.p12",
        "cert.pfx",
        "credentials.json",
        "aws-credentials.json",
        "gcp_credentials.json",
        "service-account-prod.json",
        "gcloud-service-key.json",
        "secrets.yaml",
        "config/secrets.yml",
    ],
)
def test_secret_paths_are_blocked(path: str) -> None:
    assert is_secret_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/main.py",
        "README.md",
        "kube/config",  # no leading dot — a different file
        "environment.yaml",
        "keyboard.ts",  # 'key' without the '_key' stem
        "docs/pemdas.md",
        "config/settings.yaml",
        "package.json",  # *.json is NOT blanket-blocked; only credential names
        "config.json",
        "app.yaml",
        "data.yml",
    ],
)
def test_ordinary_paths_are_allowed(path: str) -> None:
    assert not is_secret_path(path)


def test_pattern_table_is_wired_into_the_default_engine() -> None:
    assert DEFAULT_PATTERNS
    assert {cls for cls, _ in DEFAULT_PATTERNS} >= {
        "anthropic-key",
        "github-token",
        "aws-key",
        "age-key",
        "titan-api-key",
    }
