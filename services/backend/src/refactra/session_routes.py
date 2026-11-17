"""Browser session status and logout; identity does not grant repository access.

The web proxy must forward the browser's original Origin and CSRF header unchanged.
Custom-header protection assumes no cross-origin credentialed CORS access is granted.
"""

from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from refactra.auth_sessions import GitHubIdentity, resolve_session, revoke_session
from refactra.github_auth import GitHubOAuthConfig


class SessionStatus(BaseModel):
    authenticated: bool
    user: GitHubIdentity | None


def create_session_router(session_factory, config: GitHubOAuthConfig | None = None) -> APIRouter:
    config = config or GitHubOAuthConfig.from_env()
    router = APIRouter(prefix="/api/v1/auth", tags=["Session"])

    @router.get("/session", response_model=SessionStatus)
    def session_status(request: Request, response: Response):
        token = request.cookies.get(config.session_cookie_name)
        with session_factory() as session:
            identity = resolve_session(session, token)
        response.headers["Cache-Control"] = "no-store"
        if identity is None and token is not None:
            response.delete_cookie(
                config.session_cookie_name,
                path="/",
                secure=config.secure_cookie,
                httponly=True,
                samesite="lax",
            )
        return SessionStatus(authenticated=identity is not None, user=identity)

    @router.post("/logout", status_code=204, response_class=Response)
    def logout(request: Request):
        try:
            origin = urlsplit(config.callback_url)._replace(path="", query="", fragment="").geturl()
        except ValueError:
            origin = ""
        if (
            not origin
            or request.headers.get("origin") != origin
            or request.headers.get("x-refactra-csrf") != "1"
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "csrf_rejected",
                        "message": "Logout requires a trusted-origin request.",
                    }
                },
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        with session_factory.begin() as session:
            revoke_session(session, request.cookies.get(config.session_cookie_name))
        # Expire cookies only after the revocation transaction has committed.
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        for name in (config.session_cookie_name, config.cookie_name):
            response.delete_cookie(
                name, path="/", secure=config.secure_cookie, httponly=True, samesite="lax"
            )
        return response

    return router
