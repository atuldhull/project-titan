"""Unit tests for the log-call scanner (TITAN-WFL-029, Phase 46)."""

from titan.ci_kit.gates import log_scan


def _msgs(source: str) -> list[str]:
    return [f.message for f in log_scan.scan_source(source, "src/app/x.py")]


def test_flags_sensitive_fields_in_log_calls() -> None:
    assert _msgs('logger.info("login", password="x")')
    assert _msgs('log.warning("k", api_key=k)')
    assert _msgs('logger.error("db", db_password=p)')  # segment 'password'
    assert _msgs('logger.info("auth", access_token=t)')


def test_does_not_flag_safe_fields() -> None:
    assert _msgs('logger.info("login", user_id="u-1", attempt=3)') == []
    assert _msgs('logger.info("charge", amount_cents=500, currency="usd")') == []


def test_flags_print_as_a_logging_bypass() -> None:
    msgs = _msgs('print("some debug output")')
    assert msgs and "print()" in msgs[0]


def test_only_matches_calls_on_a_logger_receiver() -> None:
    # A non-logger method named info/error must not be treated as a log call.
    assert _msgs('response.error(token="x")') == []
    assert _msgs("self.password = secret") == []  # assignment, not a log call


def test_survives_a_syntax_error() -> None:
    assert log_scan.scan_source("def broken(:\n", "src/app/x.py") == []
