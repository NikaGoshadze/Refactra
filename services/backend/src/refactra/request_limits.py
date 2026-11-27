"""Bound request body size and read time before handlers parse them."""

import asyncio
import json

from fastapi import HTTPException

DEFAULT_BODY_LIMIT = 16 * 1024
PLAN_BODY_LIMIT = 128 * 1024  # A full plan edit; saved plans are at most 64 KiB.
# GitHub's installation events list repositories; larger deliveries are refused, and queued
# runs are still stopped by the workers' own access checks.
WEBHOOK_BODY_LIMIT = 1024 * 1024
WEBHOOK_PATH = "/api/v1/github/webhook"
BODY_READ_TIMEOUT_SECONDS = 10.0
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def body_limit(path: str) -> int:
    if path == WEBHOOK_PATH:
        return WEBHOOK_BODY_LIMIT
    return (
        PLAN_BODY_LIMIT
        if path.startswith("/api/v1/runs/") and path.endswith("/plan")
        else DEFAULT_BODY_LIMIT
    )


def _too_large() -> HTTPException:
    return HTTPException(
        413, {"code": "request_too_large", "message": "The request body is too large."}
    )


def _too_slow() -> HTTPException:
    return HTTPException(
        408,
        {"code": "request_timeout", "message": "The request body was not received in time."},
    )


class BodyLimitMiddleware:
    """Reject oversized or slow request bodies, with or without Content-Length.

    A declared length over the limit is rejected before reading. Otherwise each chunk is
    counted and the whole body must arrive within the deadline; the handler's body parsing
    then receives an HTTPException, which the app's error handler turns into JSON.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in _READ_METHODS:
            await self.app(scope, receive, send)
            return
        limit = body_limit(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                oversized = int(declared) > limit
            except ValueError:
                oversized = True
            if oversized:
                await _send_error(send, _too_large())
                return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BODY_READ_TIMEOUT_SECONDS
        received = 0
        complete = False

        async def bounded_receive():
            nonlocal received, complete
            if complete:
                return await receive()  # Later messages, such as a client disconnect.
            try:
                message = await asyncio.wait_for(receive(), max(deadline - loop.time(), 0))
            except TimeoutError:
                raise _too_slow() from None
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _too_large()
                complete = not message.get("more_body", False)
            return message

        await self.app(scope, bounded_receive, send)


async def _send_error(send, error: HTTPException) -> None:
    body = json.dumps({"error": error.detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": error.status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
