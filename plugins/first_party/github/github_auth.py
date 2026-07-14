"""GitHub auth (Vol 7 §6.1): a GitHub App installation token (preferred — fine-grained,
higher limits) or a PAT fallback for local/free-tier. The App private key and the PAT live
in the secret store, fetched via the ``secret.get`` capability — never in the manifest.

RS256 signing of the App JWT is the ONE thing stdlib cannot do, so it is an injected
``signer`` seam: PAT mode needs no signer (works out of the box), and App-mode JWT
construction + the installation-token exchange are unit-tested with a fake signer. In
production the connector's App mode wires a ``cryptography``-backed signer.
"""

import base64
import json
import time
from collections.abc import Callable
from typing import Any

from github_http import HttpTransport

#: (signing_input, private_key_pem) -> raw RS256 signature bytes.
Signer = Callable[[bytes, str], bytes]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_app_jwt(
    app_id: str, private_key_pem: str, *, signer: Signer, now: int
) -> str:
    """A GitHub App JWT (RS256): iat backdated 60s for clock skew, exp at the 10-minute
    ceiling, iss = app id. The signing input is the pure part; ``signer`` does the RSA."""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": now - 60, "exp": now + 600, "iss": app_id}
    signing_input = f"{_b64url(_dump(header))}.{_b64url(_dump(claims))}".encode("ascii")
    signature = signer(signing_input, private_key_pem)
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


def _dump(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _default_signer(
    signing_input: bytes, private_key_pem: str
) -> bytes:  # pragma: no cover
    """Production RS256 via the optional ``cryptography`` extra. Absent → a clear error
    pointing at PAT mode, never a silent failure."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # noqa: TRY003
        raise RuntimeError(
            "GitHub App auth needs the 'cryptography' extra; use a PAT (GITHUB_TOKEN) instead"
        ) from exc
    key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    return key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]


class GithubAuth:
    """Resolves the Authorization token: a PAT verbatim, or an App installation token
    exchanged (and cached until ~1 min before expiry) via the App JWT."""

    def __init__(
        self,
        config: "dict[str, str]",
        transport: HttpTransport,
        *,
        api_url: str,
        signer: Signer | None = None,
        clock: "Callable[[], float]" = time.time,
    ) -> None:
        self._cfg = config
        self._http = transport
        self._api = api_url.rstrip("/")
        self._signer = signer or _default_signer
        self._clock = clock
        self._inst_token = ""
        self._inst_exp = 0

    @property
    def is_app(self) -> bool:
        return not self._cfg.get("GITHUB_TOKEN") and bool(
            self._cfg.get("GITHUB_APP_ID")
        )

    def token(self) -> str:
        pat = self._cfg.get("GITHUB_TOKEN", "")
        if pat:
            return pat
        return self._installation_token()

    def _installation_token(self) -> str:
        now = int(self._clock())
        if self._inst_token and now < self._inst_exp - 60:
            return self._inst_token
        app_id = self._cfg.get("GITHUB_APP_ID", "")
        installation = self._cfg.get("GITHUB_APP_INSTALLATION_ID", "")
        key = self._cfg.get("GITHUB_APP_PRIVATE_KEY", "")
        if not (app_id and installation and key):
            raise RuntimeError(
                "github: neither GITHUB_TOKEN nor a complete App config is set"
            )
        jwt = build_app_jwt(app_id, key, signer=self._signer, now=now)
        resp = self._http.request(
            "POST",
            f"{self._api}/app/installations/{installation}/access_tokens",
            headers={
                "authorization": f"Bearer {jwt}",
                "accept": "application/vnd.github+json",
                "x-github-api-version": "2022-11-28",
            },
        )
        if resp.status not in (200, 201):
            raise RuntimeError(
                f"github: installation token exchange failed ({resp.status})"
            )
        data = resp.json()
        self._inst_token = str(data.get("token", ""))
        self._inst_exp = _parse_expiry(str(data.get("expires_at", "")), now)
        return self._inst_token


def _parse_expiry(iso: str, now: int) -> int:
    """GitHub returns expires_at as ISO-8601 (~1h out). Parse it; on any trouble fall back
    to a conservative 50-minute lifetime so we never treat a token as valid past expiry."""
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return now + 3000
