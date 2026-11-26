import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import refactra.github_user_repositories as listing_module
from refactra.auth_sessions import GitHubIdentity, issue_session, session_token_hash
from refactra.github_user_repositories import (
    SLUG_ENV,
    RepositoryListing,
    UserRepository,
    UserTokenRejected,
    install_url,
    list_user_repositories,
)
from refactra.github_user_tokens import GitHubUserToken, TokenCipher
from refactra.models import AuthSession
from refactra.user_repository_routes import create_user_repository_router


def repo(number, owner="octo", push=True):
    return {
        "id": number,
        "full_name": f"{owner}/repo-{number:03d}",
        "private": number % 2 == 0,
        "default_branch": "main",
        "permissions": {"admin": False, "push": push, "pull": True},
    }


INSTALLATIONS = {
    "total_count": 2,
    "installations": [
        {
            "id": 11,
            "account": {"login": "octo", "type": "User"},
            "repository_selection": "selected",
        },
        {
            "id": 22,
            "account": {"login": "octo-org", "type": "Organization"},
            "repository_selection": "all",
        },
    ],
}
# 150 repositories across two pages for the user, one read-only repository for the org.
PAGES = {
    (11, 1): {"total_count": 150, "repositories": [repo(n) for n in range(1, 101)]},
    (11, 2): {"total_count": 150, "repositories": [repo(n) for n in range(101, 151)]},
    (22, 1): {"total_count": 1, "repositories": [repo(500, "octo-org", push=False)]},
}


def github(calls, status=200):
    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer ghu_user"
        if status != 200:
            return httpx.Response(status, json={"message": "Bad credentials"})
        page = int(request.url.params["page"])
        if request.url.path == "/user/installations":
            return httpx.Response(200, json=INSTALLATIONS)
        installation = int(request.url.path.split("/")[3])
        return httpx.Response(200, json=PAGES[(installation, page)])

    return httpx.MockTransport(handler)


def test_listing_follows_pages_across_installations_and_marks_write_access():
    calls = []
    result = list_user_repositories("ghu_user", transport=github(calls))
    assert [(item.id, item.account, item.settings_url) for item in result.installations] == [
        (11, "octo", "https://github.com/settings/installations/11"),
        (22, "octo-org", "https://github.com/organizations/octo-org/settings/installations/22"),
    ]
    assert len(result.repositories) == 151 and not result.truncated
    names = [item.full_name for item in result.repositories]
    assert names == sorted(names, key=str.lower)
    org = next(item for item in result.repositories if item.id == 500)
    assert (org.installation_id, org.writable) == (22, False)
    assert all(item.writable for item in result.repositories if item.id != 500)
    assert [call.url.params["per_page"] for call in calls] == ["100"] * 4


def test_page_caps_mark_the_listing_truncated(monkeypatch):
    monkeypatch.setattr(listing_module, "MAX_REPOSITORY_PAGES", 1)
    result = list_user_repositories("ghu_user", transport=github([]))
    assert result.truncated and len(result.repositories) == 101


def test_a_revoked_token_and_unexpected_data_are_reported(monkeypatch):
    with pytest.raises(UserTokenRejected):
        list_user_repositories("ghu_user", transport=github([], status=401))
    monkeypatch.setitem(PAGES, (22, 1), {"total_count": 1, "repositories": [{"id": "x"}]})
    with pytest.raises((ValueError, KeyError)):
        list_user_repositories("ghu_user", transport=github([]))


def test_install_url_comes_from_a_valid_slug(monkeypatch):
    monkeypatch.delenv(SLUG_ENV, raising=False)
    assert install_url() is None
    monkeypatch.setenv(SLUG_ENV, "refactra-app")
    assert install_url() == "https://github.com/apps/refactra-app/installations/new"
    monkeypatch.setenv(SLUG_ENV, "../evil")
    with pytest.raises(ValueError):
        install_url()


CIPHER = TokenCipher(bytes(range(32)))
USER_TOKEN = GitHubUserToken("ghu_user")
LISTING = RepositoryListing(
    (),
    (UserRepository(7, "octo/app", True, "main", 11, True),),
    False,
)


@pytest.fixture
def api(session_factory, monkeypatch):
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "12345")
    monkeypatch.setenv(SLUG_ENV, "refactra-app")
    behavior = {"result": LISTING}

    def lister(token):
        behavior["token"] = token
        if isinstance(behavior["result"], Exception):
            raise behavior["result"]
        return behavior["result"]

    def build(cipher=CIPHER):
        app = FastAPI()
        app.include_router(
            create_user_repository_router(session_factory, cipher=cipher, lister=lister)
        )
        return TestClient(app)

    def sign_in(user_id=12345, user_token=USER_TOKEN):
        with session_factory.begin() as session:
            return issue_session(session, GitHubIdentity(user_id, "octo"), user_token, CIPHER)

    return build, sign_in, behavior


def get(client, session=None):
    client.cookies.clear()
    if session:
        client.cookies.set("refactra_session", session)
    return client.get("/api/v1/user/repositories")


def test_a_signed_in_user_gets_their_repositories(api):
    build, sign_in, behavior = api
    response = get(build(), sign_in())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["install_url"] == "https://github.com/apps/refactra-app/installations/new"
    assert body["repositories"] == [
        {
            "id": 7,
            "full_name": "octo/app",
            "private": True,
            "default_branch": "main",
            "installation_id": 11,
            "writable": True,
        }
    ]
    assert behavior["token"] == "ghu_user" and "ghu_user" not in response.text


def test_anonymous_uninvited_and_tokenless_sessions_are_refused(api):
    build, sign_in, _ = api
    client = build()
    assert get(client).json()["error"]["code"] == "authentication_required"
    response = get(client, sign_in(user_id=999))
    assert (response.status_code, response.json()["error"]["code"]) == (
        403,
        "repository_access_denied",
    )
    response = get(client, sign_in(user_token=None))
    assert (response.status_code, response.json()["error"]["code"]) == (
        401,
        "github_reauthorization_required",
    )
    response = get(build(cipher=None), sign_in())
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"


def test_a_token_github_rejects_is_wiped(api, session_factory):
    build, sign_in, behavior = api
    session = sign_in()
    behavior["result"] = UserTokenRejected()
    response = get(build(), session)
    assert response.json()["error"]["code"] == "github_reauthorization_required"
    with session_factory() as db:
        assert db.get(AuthSession, session_token_hash(session)).github_token_ciphertext is None


def test_github_outages_are_reported_without_details(api):
    build, sign_in, behavior = api
    behavior["result"] = httpx.ConnectError("private upstream details")
    response = get(build(), sign_in())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "github_unavailable"
    assert "private upstream details" not in response.text
