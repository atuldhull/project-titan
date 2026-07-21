"""Secret-bearing file exclusion (TITAN-SEC-055).

``is_secret_path`` answers "may this workspace file's NAME ever reach agent
context?" for the file-reading surfaces (``fs.read/list/glob/grep``, the
``code.test`` report reader, the plugin ``fs.read`` capability). Matching is
case-insensitive and separator-tolerant (evasion via ``.ENV`` or backslashes
must not work).

The basename patterns are Vol 8 §6.4's list verbatim — including ``*_key*``,
which knowingly over-matches source files like ``api_key_manager.py``; the
spec chose recall over precision for a MUST control and this module follows
it. ``.env.example`` is likewise blocked: a carve-out would be a rename-based
bypass (real secrets in a file named like a template).

``terminal.run`` cannot be filename-filtered (``cat .env`` inside the sandbox
returns bytes, not paths) — that surface is covered by CONTENT redaction at
the Tool Bus and the output stream (TITAN-SEC-046/054), which is why this
module and the engine ship together.
"""

from fnmatch import fnmatchcase
from pathlib import PurePath, PurePosixPath

_BASENAME_PATTERNS: tuple[str, ...] = (
    ".env*",
    "*.pem",
    "*_key*",
    # Private keys / keystores that are NOT `*_key*` or `*.pem`: TLS/JWT keys
    # are conventionally `server.key`/`tls.key`/`privkey.key`, and Java/Windows
    # keystores carry secrets too (Phase-69 review — `*.key` was a real gap).
    "*.key",
    "*.keystore",
    "*.jks",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.ppk",
    # Cloud / service-account credential files by their conventional NAMES (the
    # `.aws/`-style directory rules below cover the standard locations; these
    # catch a `credentials.json` dropped anywhere in a repo).
    "credentials.json",
    "*_credentials.json",
    "*-credentials.json",
    "service-account*.json",
    "gcloud-service-key*.json",
    "secrets.yaml",
    "secrets.yml",
    ".netrc",
    "_netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
)

# Cloud/tooling credential locations, matched as anchored path segments: a
# trailing '/' means "anything under this directory".
_PATH_SEGMENTS: tuple[str, ...] = (
    ".aws/",
    ".config/gcloud/",
    ".azure/",
    ".kube/config",
    ".docker/config.json",
    ".ssh/",
)

SECRET_FILE_PATTERNS: tuple[str, ...] = _BASENAME_PATTERNS + _PATH_SEGMENTS


def is_secret_path(path: str | PurePath) -> bool:
    """True when *path* (workspace-relative) matches a secret-bearing pattern."""
    text = str(path).replace("\\", "/").lower().lstrip("/")
    pure = PurePosixPath(text)
    name = pure.name
    if any(fnmatchcase(name, pattern) for pattern in _BASENAME_PATTERNS):
        return True
    # '/x/.aws/credentials/' anchors each segment pattern on both sides so
    # '.aws/credentials.bak' does not match while '.ssh/<anything>' does.
    haystack = f"/{pure.as_posix()}/"
    for segment in _PATH_SEGMENTS:
        needle = f"/{segment}" if segment.endswith("/") else f"/{segment}/"
        if needle in haystack:
            return True
    return False


__all__ = ["SECRET_FILE_PATTERNS", "is_secret_path"]
