"""Structured workflow diagnostics using metadata already available to the backend."""

import json
import logging
import re
from datetime import UTC, datetime

from sqlalchemy import exc as sa_exc

logger = logging.getLogger("refactra.workflow")


def workflow_log(event: str, *, warning: bool = False, **metadata) -> None:
    destination = logger if logger.hasHandlers() else logging.getLogger("uvicorn.error")
    level = logging.WARNING if warning else logging.INFO
    if destination.isEnabledFor(level):
        destination.log(
            level,
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "event": event,
                    **metadata,
                },
                default=str,
            ),
        )


def diagnostic_id(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        return value
    return None


# SQLSTATE classes (https://www.postgresql.org/docs/current/errcodes-appendix.html).
_SQLSTATE_CATEGORIES = {
    "08": "connectivity",  # connection exception
    "53": "connectivity",  # insufficient resources
    "57": "connectivity",  # operator intervention, such as an admin shutdown
    "40": "concurrency",  # serialization failure or deadlock
    "55": "concurrency",  # lock not available
    "23": "integrity",  # constraint violation
    "22": "data",  # data exception
    "42": "programming",  # syntax error or access rule violation
}
_CONNECTIVITY_ERRORS = (
    sa_exc.DisconnectionError,
    sa_exc.InterfaceError,
    sa_exc.OperationalError,
    sa_exc.TimeoutError,
)


def database_error_details(error: sa_exc.SQLAlchemyError) -> dict:
    """Safe facts about a database error for logs and operators.

    Only the exception class, a category and the five-character SQLSTATE are returned.
    The message, SQL text and bound parameters can contain data or credentials and are
    never included.
    """
    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    if not (isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate)):
        sqlstate = None
    category = _SQLSTATE_CATEGORIES.get(sqlstate[:2]) if sqlstate else None
    if category is None:
        category = (
            "connectivity"
            if isinstance(error, _CONNECTIVITY_ERRORS)
            else "integrity"
            if isinstance(error, sa_exc.IntegrityError)
            else "data"
            if isinstance(error, sa_exc.DataError)
            else "programming"
            if isinstance(error, sa_exc.ProgrammingError)
            else "database"
        )
    return {"error_type": type(error).__name__, "category": category, "sqlstate": sqlstate}
