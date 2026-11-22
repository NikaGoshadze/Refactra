"""Structured workflow diagnostics using metadata already available to the backend."""

import json
import logging
import re
from datetime import UTC, datetime

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
