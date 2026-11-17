import io
import logging
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from uvicorn.logging import AccessFormatter

from refactra.access_logs import AuthQueryRedactionFilter
from refactra.api import create_app
from refactra.config import Settings

UNUSED_DATABASE = "postgresql+psycopg://unused:unused@127.0.0.1:1/unused"


@pytest.mark.parametrize(
    "target",
    [
        "/api/v1/auth/github/callback?code=sensitive-code&state=sensitive-state",
        "/api/v1/auth/github/callback/?error_description=sensitive-error",
        "/api/v1/auth/github/login?unexpected=sensitive-value",
    ],
)
def test_formatted_auth_log_hides_query_values_but_preserves_request_details(target):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", target, "1.1", 400),
        None,
    )
    AuthQueryRedactionFilter().filter(record)
    output = AccessFormatter("%(request_line)s %(status_code)s", use_colors=False).format(record)
    assert output == f"GET {target.partition('?')[0]}?[redacted] HTTP/1.1 400 Bad Request"
    assert "sensitive-" not in record.getMessage()


@pytest.mark.parametrize("target", ["/api/v1/auth/github/callback", "/health/live?probe=visible"])
def test_unrelated_query_strings_and_query_free_requests_are_unchanged(target):
    args = ("127.0.0.1:1234", "GET", target, "1.1", 200)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d', args, None
    )
    AuthQueryRedactionFilter().filter(record)
    assert record.args == args


def test_repeated_app_creation_installs_only_one_filter(monkeypatch):
    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "filters", [])
    create_app(Settings(UNUSED_DATABASE))
    create_app(Settings(UNUSED_DATABASE))
    assert sum(isinstance(item, AuthQueryRedactionFilter) for item in logger.filters) == 1


def test_real_uvicorn_access_output_redacts_callback_queries(monkeypatch):
    # No database or GitHub request is needed: an unconfigured callback returns 503.
    monkeypatch.delenv("REFACTRA_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("REFACTRA_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.delenv("REFACTRA_GITHUB_CLIENT_SECRET", raising=False)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(AccessFormatter("%(request_line)s %(status_code)s", use_colors=False))
    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)
    app = create_app(Settings(UNUSED_DATABASE))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, http="h11", log_config=None))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Local Uvicorn server did not start"
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False
            ) as client:
                response = client.get(
                    "/api/v1/auth/github/callback",
                    params={"code": "never-log-this-code", "state": "never-log-this-state"},
                )
                assert response.status_code == 503
                assert client.get("/health/live?probe=visible").status_code == 200
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive()
    logs = output.getvalue()
    assert "/api/v1/auth/github/callback?[redacted] HTTP/1.1 503" in logs
    assert "never-log-this" not in logs
    assert "/health/live?probe=visible HTTP/1.1 200" in logs
