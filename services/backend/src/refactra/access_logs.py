"""Redact authentication query strings from the supported Uvicorn access-log format.

This filters log records only; it does not change the request seen by OAuth.
Reverse proxies and other logging systems need their own equivalent policy.
"""

import logging


class AuthQueryRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn logs (client address, method, request target, HTTP version, status).
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, separator, _ = args[2].partition("?")
            if separator and (path == "/api/v1/auth" or path.startswith("/api/v1/auth/")):
                record.args = (*args[:2], f"{path}?[redacted]", *args[3:])
        return True


def install_auth_query_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, AuthQueryRedactionFilter) for item in logger.filters):
        logger.addFilter(AuthQueryRedactionFilter())
