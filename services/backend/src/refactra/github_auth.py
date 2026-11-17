"""GitHub App login start. Callback/token exchange and session issuance are a later batch."""

import os
import re
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import BadData, URLSafeTimedSerializer

OAUTH_TTL_SECONDS = 600
CALLBACK_PATH = "/api/v1/auth/github/callback"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class GitHubOAuthConfig:
    client_id: str
    callback_url: str
    state_secret: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "GitHubOAuthConfig":
        return cls(
            client_id=os.getenv("REFACTRA_GITHUB_CLIENT_ID", ""),
            callback_url=os.getenv(
                "REFACTRA_GITHUB_CALLBACK_URL", f"http://localhost:3000{CALLBACK_PATH}"
            ),
            state_secret=os.getenv("REFACTRA_OAUTH_STATE_SECRET", ""),
        )

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.callback_url).scheme == "https"

    @property
    def cookie_name(self) -> str:
        return "__Host-refactra_oauth" if self.secure_cookie else "refactra_oauth"

    def serializer(self) -> URLSafeTimedSerializer:
        try:
            url = urlsplit(self.callback_url)
            _ = url.port  # Validate an explicitly configured port too.
            safe_url = (url.scheme == "https" and bool(url.hostname)) or (
                url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            valid = (
                safe_url
                and url.path == CALLBACK_PATH
                and not url.query
                and not url.fragment
                and url.username is None
                and url.password is None
            )
        except ValueError:
            valid = False
        if not self.client_id.strip() or len(self.state_secret) < 32 or not valid:
            raise HTTPException(
                503,
                {
                    "code": "github_auth_unconfigured",
                    "message": "GitHub sign-in configuration is missing or invalid.",
                },
            )
        return URLSafeTimedSerializer(
            self.state_secret,
            salt="refactra-github-oauth-v1",
            signer_kwargs={"digest_method": sha256},
        )


def validate_oauth_attempt(config: GitHubOAuthConfig, cookie: str | None, state: str | None) -> str:
    """Return the PKCE verifier for the future callback. That route must clear the cookie.

    The signed cookie is not encrypted. It contains only temporary state and the
    PKCE verifier, never GitHub credentials or a Refactra authenticated session.
    """
    signer = config.serializer()
    invalid = HTTPException(
        400,
        {
            "code": "invalid_oauth_state",
            "message": "The login attempt is invalid or expired. Start again.",
        },
    )
    if not cookie or len(cookie) > 2048 or state is None or _TOKEN.fullmatch(state) is None:
        raise invalid
    try:
        payload = signer.loads(cookie, max_age=OAUTH_TTL_SECONDS)
    except BadData:
        raise invalid from None
    if not isinstance(payload, dict):
        raise invalid
    expected, verifier = payload.get("state"), payload.get("verifier")
    if (
        not isinstance(expected, str)
        or not isinstance(verifier, str)
        or _TOKEN.fullmatch(expected) is None
        or _TOKEN.fullmatch(verifier) is None
        or not compare_digest(expected, state)
    ):
        raise invalid
    return verifier


def create_github_auth_router(config: GitHubOAuthConfig | None = None) -> APIRouter:
    config = config or GitHubOAuthConfig.from_env()
    router = APIRouter(prefix="/api/v1/auth/github", tags=["GitHub sign-in"])

    @router.get("/login", response_class=RedirectResponse, status_code=302)
    def login_start():
        signer = config.serializer()
        state, verifier = token_urlsafe(32), token_urlsafe(32)
        challenge = (
            urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        )
        query = urlencode(
            {
                "client_id": config.client_id,
                "redirect_uri": config.callback_url,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(
            f"https://github.com/login/oauth/authorize?{query}",
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.set_cookie(
            config.cookie_name,
            signer.dumps({"state": state, "verifier": verifier}),
            max_age=OAUTH_TTL_SECONDS,
            path="/",
            secure=config.secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return response

    return router
