"""Protected metadata endpoint; App credentials remain inside the backend."""

import os
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from refactra.github_app import GitHubAppClient, GitHubAppError
from refactra.github_auth import GitHubOAuthConfig
from refactra.github_repository_access import require_github_write_access
from refactra.repository_access import RepositoryAccessPolicy


class RepositoryRead(BaseModel):
    repository_id: int
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    default_branch: str = Field(min_length=1)
    access: Literal["read-only"] = "read-only"


def create_repository_router(session_factory) -> APIRouter:
    router = APIRouter(prefix="/api/v1/repositories", tags=["Repositories"])
    cookie_name = GitHubOAuthConfig.from_env().session_cookie_name

    def error(status, detail):
        return JSONResponse(
            status_code=status, content={"error": detail}, headers={"Cache-Control": "no-store"}
        )

    @router.get("/{repository_id}", response_model=RepositoryRead)
    def repository(repository_id: int, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            policy = RepositoryAccessPolicy.from_env()
            with session_factory() as session:
                identity = policy.require_eligible_session(
                    session, request.cookies.get(cookie_name), repository_id
                )
            installation_id = int(os.environ["REFACTRA_GITHUB_INSTALLATION_ID"])
            client_id = os.environ["REFACTRA_GITHUB_CLIENT_ID"].strip()
            if installation_id <= 0 or not client_id:
                raise ValueError("Invalid App configuration")
            app = GitHubAppClient(
                client_id,
                Path(os.environ["REFACTRA_GITHUB_PRIVATE_KEY_PATH"]).read_bytes(),
            )
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)
        except (OSError, ValueError, KeyError):
            return error(
                503,
                {
                    "code": "repository_access_unconfigured",
                    "message": "Repository access configuration is unavailable.",
                },
            )
        try:
            token = require_github_write_access(app, installation_id, repository_id, identity)
            with httpx.Client(
                timeout=10.0, follow_redirects=False, trust_env=False, transport=app.transport
            ) as client:
                result = client.get(
                    f"https://api.github.com/repositories/{repository_id}",
                    headers={
                        "Authorization": f"Bearer {token.value}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10",
                    },
                )
            result.raise_for_status()
            data = result.json()
            if type(data["id"]) is not int or data["id"] != repository_id:
                raise ValueError("Unexpected repository identity")
            return RepositoryRead(
                repository_id=repository_id,
                full_name=data["full_name"],
                default_branch=data["default_branch"],
            )
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)
        except (GitHubAppError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return error(
                502,
                {
                    "code": "github_unavailable",
                    "message": "Repository access could not be verified.",
                },
            )

    return router
