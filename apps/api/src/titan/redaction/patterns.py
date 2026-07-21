"""Provider-key patterns (TITAN-SEC-054 detector 3): well-known credential
shapes redactable without any inventory knowledge.

Order matters only as a tie-break: when two patterns match the same span (an
``sk-ant-…`` key matches both the anthropic and the generic openai shape), the
engine keeps the earlier entry's class, so more specific shapes come first.
The list subsumes the Phase-18 ``titan.tools.redaction`` value shapes and adds
the credential formats TITAN itself mints (``titan_ak_…`` API keys,
``titan_rt_…`` refresh tokens, ``AGE-SECRET-KEY-1…`` vault identities).

EVERY quantifier is UPPER-BOUNDED and POSSESSIVE (``{lo,hi}+``, Python 3.11+).
The engine runs ``finditer`` for every pattern on EVERY string it redacts —
log lines and sandbox output are attacker-influenceable — so an open-ended
``{n,}`` is a ReDoS surface: the original ``jwt`` pattern was quadratic on
``eyJeyJeyJ…`` input (a match-start every 3 chars, each greedily consuming the
tail then backtracking for a ``.`` that never comes). Possessive quantifiers
forbid the backtrack; the upper bounds keep a single start's work constant.
Real credentials sit far below these ceilings. (Phase-69 security review.)
"""

import re

DEFAULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("age-key", re.compile(r"(?i)AGE-SECRET-KEY-1[0-9A-Z]{50,256}+")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,512}+")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_-]{16,512}+")),
    (
        "github-token",
        re.compile(
            r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,512}+|github_pat_[A-Za-z0-9_]{20,512}+"
        ),
    ),
    ("gitlab-token", re.compile(r"glpat-[A-Za-z0-9_-]{20,512}+")),
    ("aws-key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("slack-token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,512}+")),
    ("titan-api-key", re.compile(r"titan_(?:ak|rt)_[A-Za-z0-9_-]{16,512}+")),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{8,4096}+\.eyJ[A-Za-z0-9_-]{8,4096}+\.[A-Za-z0-9_-]{8,4096}+"),
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s{1,8}+[A-Za-z0-9._~+/-]{16,4096}+=*+")),
    (
        # `[A-Z ]{0,64}` is bounded but NOT possessive: it must backtrack to let
        # the literal `PRIVATE KEY` match (the class overlaps that literal). The
        # 64-char bound keeps that backtracking trivial; the ReDoS surface was
        # the body, now the bounded lazy `[\s\S]{0,8192}?`.
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----"
            r"[\s\S]{0,8192}?"
            r"-----END [A-Z ]{0,64}PRIVATE KEY-----"
        ),
    ),
    # Masks 'scheme://user:password@' (any scheme, incl. postgresql+asyncpg,
    # redis, amqp, https) — the host/db tail stays readable for diagnostics.
    ("dsn-password", re.compile(r"\b[a-z][a-z0-9+.-]{1,30}+://[^:/\s@]{1,128}+:[^@\s]{1,256}+@")),
)

__all__ = ["DEFAULT_PATTERNS"]
