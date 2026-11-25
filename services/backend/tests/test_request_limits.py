import asyncio
import json
from uuid import uuid4

from fastapi.testclient import TestClient

import refactra.request_limits as limits
from refactra.api import create_app
from refactra.config import Settings

UNREACHABLE = "postgresql+psycopg://unused:unused@127.0.0.1:1/unused"


def app():
    return create_app(Settings(UNREACHABLE, enable_mock=True))


def call(application, path, chunks, *, delay=0.0):
    """Drive the ASGI app with a chunked body and no Content-Length, as a raw client would."""
    pending = list(chunks)
    sent = []

    async def receive():
        if pending:
            body = pending.pop(0)
            if delay and len(pending) < len(chunks) - 1:
                await asyncio.sleep(delay)  # Every chunk after the first arrives late.
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"test")],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    asyncio.run(application(scope, receive, send))
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, json.loads(body)


def test_a_declared_oversized_body_is_rejected_before_it_is_read():
    with TestClient(app()) as client:
        response = client.post("/api/v1/mock/runs", content=b"x" * (limits.DEFAULT_BODY_LIMIT + 1))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.headers["cache-control"] == "no-store" and response.headers["x-request-id"]


def test_an_oversized_chunked_body_without_length_is_rejected():
    status, body = call(app(), "/api/v1/mock/runs", [b"x" * 8192] * 3)
    assert (status, body["error"]["code"]) == (413, "request_too_large")


def test_a_slow_body_is_rejected_at_the_read_deadline(monkeypatch):
    monkeypatch.setattr(limits, "BODY_READ_TIMEOUT_SECONDS", 0.2)
    status, body = call(app(), "/api/v1/mock/runs", [b'{"task_text": ', b'"late"}'], delay=1.0)
    assert (status, body["error"]["code"]) == (408, "request_timeout")


def test_plan_edits_may_send_larger_bodies_than_other_writes():
    run = f"/api/v1/runs/{uuid4()}/plan"
    status, _ = call(app(), run, [b" " * 8192] * 5)  # 40 KiB: within the plan limit.
    assert status != 413
    status, _ = call(app(), run, [b" " * 65536] * 3)  # 192 KiB: beyond it.
    assert status == 413
