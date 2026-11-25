"""GitHub App sign-in. Repository authorization remains a separate boundary."""

import os
import re
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from httpx import Client, HTTPError
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.exc import SQLAlchemyError

from refactra.auth_sessions import SESSION_TTL, GitHubIdentity, issue_session, revoke_session
from refactra.diagnostics import database_error_details, workflow_log

OAUTH_TTL_SECONDS = 600
CALLBACK_PATH = "/api/v1/auth/github/callback"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class GitHubOAuthConfig:
    client_id: str
    callback_url: str
    state_secret: str = field(repr=False)
    client_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "GitHubOAuthConfig":
        return cls(
            client_id=os.getenv("REFACTRA_GITHUB_CLIENT_ID", ""),
            callback_url=os.getenv(
                "REFACTRA_GITHUB_CALLBACK_URL", f"http://localhost:3000{CALLBACK_PATH}"
            ),
            state_secret=os.getenv("REFACTRA_OAUTH_STATE_SECRET", ""),
            client_secret=os.getenv("REFACTRA_GITHUB_CLIENT_SECRET", ""),
        )

    @property
    def secure_cookie(self) -> bool:
        try:
            return urlsplit(self.callback_url).scheme == "https"
        except ValueError:
            return True  # Fail closed while clearing a failed attempt with invalid configuration.

    @property
    def cookie_name(self) -> str:
        return "__Host-refactra_oauth" if self.secure_cookie else "refactra_oauth"

    @property
    def session_cookie_name(self) -> str:
        return "__Host-refactra_session" if self.secure_cookie else "refactra_session"

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
    """Return the PKCE verifier; the callback clears the cookie on success and failure.

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


def _github_identity(config: GitHubOAuthConfig, code: str, verifier: str) -> GitHubIdentity:
    if not config.client_secret:
        raise HTTPException(
            503,
            {
                "code": "github_auth_unconfigured",
                "message": "The GitHub client secret is not configured.",
            },
        )
    try:
        with Client(timeout=10.0, follow_redirects=False, trust_env=False) as github:
            response = github.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": config.callback_url,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise HTTPException(
                    401,
                    {
                        "code": "github_authorization_failed",
                        "message": "GitHub rejected this login. Start again.",
                    },
                )
            token = data["access_token"]
            if not isinstance(token, str) or not token or data.get("token_type") != "bearer":
                raise ValueError("Invalid token response")
            response = github.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            )
            response.raise_for_status()
            user = response.json()
            if type(user["id"]) is not int or not isinstance(user["login"], str):
                raise ValueError("Invalid GitHub identity")
            return GitHubIdentity(user_id=user["id"], login=user["login"])
    except (HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(
            502,
            {
                "code": "github_unavailable",
                "message": "GitHub sign-in could not be verified. Start again.",
            },
        ) from None


def create_github_auth_router(
    session_factory, config: GitHubOAuthConfig | None = None
) -> APIRouter:
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

    @router.get("/callback")
    def login_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        try:
            verifier = validate_oauth_attempt(
                config, request.cookies.get(config.cookie_name), state
            )
            if error or not code or len(code) > 512:
                raise HTTPException(
                    400,
                    {
                        "code": "github_login_incomplete",
                        "message": "GitHub sign-in was declined or incomplete.",
                    },
                )
            identity = _github_identity(config, code, verifier)
            with session_factory.begin() as session:
                revoke_session(session, request.cookies.get(config.session_cookie_name))
                token = issue_session(session, identity)
            # The transaction has committed before an authenticated cookie is issued.
            destination = urlsplit(config.callback_url)._replace(path="/").geturl()
            response = RedirectResponse(destination, status_code=302)
            response.set_cookie(
                config.session_cookie_name,
                token,
                max_age=int(SESSION_TTL.total_seconds()),
                path="/",
                secure=config.secure_cookie,
                httponly=True,
                samesite="lax",
            )
        except HTTPException as exc:
            response = JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        except SQLAlchemyError as error:
            request_id = getattr(request.state, "request_id", None)
            workflow_log(
                "database_error",
                warning=True,
                request_id=request_id,
                method=request.method,
                route=CALLBACK_PATH,
                **database_error_details(error),
            )
            response = JSONResponse(
                {
                    "error": {
                        "code": "database_unavailable",
                        "message": "The session could not be saved. Start again.",
                        "request_id": request_id,
                    }
                },
                status_code=503,
            )
        response.delete_cookie(
            config.cookie_name, path="/", secure=config.secure_cookie, httponly=True, samesite="lax"
        )
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        return response

    return router
