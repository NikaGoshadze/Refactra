"""GET /api/v1/user/repositories: the signed-in user's repositories for the picker."""

from collections.abc import Callable

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import update

from refactra.auth_sessions import session_token_hash
from refactra.github_auth import GitHubOAuthConfig, refresh_github_token
from refactra.github_user_repositories import (
    RepositoryListing,
    UserTokenRejected,
    install_url,
    list_user_repositories,
)
from refactra.github_user_tokens import TokenCipher, current_user_token
from refactra.models import AuthSession
from refactra.repository_access import RepositoryAccessPolicy


class InstallationRead(BaseModel):
    id: int
    account: str
    account_type: str
    repository_selection: str
    settings_url: str


class UserRepositoryRead(BaseModel):
    id: int
    full_name: str
    private: bool
    default_branch: str
    installation_id: int
    writable: bool


class UserRepositoriesRead(BaseModel):
    install_url: str | None
    installations: list[InstallationRead]
    repositories: list[UserRepositoryRead]
    truncated: bool


REAUTHORIZE = {
    "code": "github_reauthorization_required",
    "message": "Sign in with GitHub again to list your repositories.",
}


def create_user_repository_router(
    session_factory,
    *,
    cipher: TokenCipher | None = None,
    lister: Callable[[str], RepositoryListing] = list_user_repositories,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/user", tags=["Repositories"])
    oauth = GitHubOAuthConfig.from_env()
    cipher = cipher or TokenCipher.from_env()
    link = install_url()  # Read at startup so an invalid slug stops the API.

    def error(status: int, detail: dict) -> JSONResponse:
        return JSONResponse(
            status_code=status, content={"error": detail}, headers={"Cache-Control": "no-store"}
        )

    def wipe(token_hash: str) -> None:
        with session_factory.begin() as session:
            session.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == token_hash)
                .values(
                    github_token_ciphertext=None,
                    github_token_expires_at=None,
                    github_refresh_ciphertext=None,
                    github_refresh_expires_at=None,
                )
            )

    @router.get("/repositories", response_model=UserRepositoriesRead)
    def user_repositories(request: Request):
        cookie = request.cookies.get(oauth.session_cookie_name)
        token_hash = session_token_hash(cookie)
        try:
            with session_factory.begin() as session:
                RepositoryAccessPolicy.from_env().require_allowed_user(session, cookie)
                if cipher is None:
                    return error(
                        503,
                        {
                            "code": "repository_listing_unconfigured",
                            "message": "Repository listing is not configured.",
                        },
                    )
                token = current_user_token(
                    session,
                    token_hash,
                    cipher,
                    lambda refresh: refresh_github_token(oauth, refresh),
                )
            if token is None:
                return error(401, REAUTHORIZE)
            listing = lister(token)
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)
        except UserTokenRejected:
            wipe(token_hash)
            return error(401, REAUTHORIZE)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return error(
                502,
                {
                    "code": "github_unavailable",
                    "message": "Your repositories could not be loaded from GitHub. Try again.",
                },
            )
        body = UserRepositoriesRead(
            install_url=link,
            installations=[
                InstallationRead(**vars(item), settings_url=item.settings_url)
                for item in listing.installations
            ],
            repositories=[UserRepositoryRead(**vars(item)) for item in listing.repositories],
            truncated=listing.truncated,
        )
        return JSONResponse(body.model_dump(), headers={"Cache-Control": "no-store"})

    return router
