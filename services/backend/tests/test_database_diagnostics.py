import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc

from refactra.api import create_app
from refactra.config import Settings
from refactra.diagnostics import database_error_details

SECRET = "secret-value"
# An unreachable database whose URL carries a password that must never be logged.
UNREACHABLE = "postgresql+psycopg://refactra:hunter2-password@127.0.0.1:1/refactra"


class DriverError(Exception):
    def __init__(self, sqlstate):
        super().__init__(f"driver detail containing {SECRET}")
        self.sqlstate = sqlstate


def wrapped(kind, sqlstate=None):
    statement = "SELECT token FROM runs WHERE token = %(token)s"
    return kind(statement, {"token": SECRET}, DriverError(sqlstate))


@pytest.mark.parametrize(
    ("error", "category", "sqlstate"),
    [
        (wrapped(sa_exc.OperationalError), "connectivity", None),
        (wrapped(sa_exc.OperationalError, "08006"), "connectivity", "08006"),
        (wrapped(sa_exc.OperationalError, "40P01"), "concurrency", "40P01"),
        (wrapped(sa_exc.IntegrityError, "23505"), "integrity", "23505"),
        (wrapped(sa_exc.ProgrammingError, "42P01"), "programming", "42P01"),
        (wrapped(sa_exc.DataError, "22P02"), "data", "22P02"),
        (wrapped(sa_exc.IntegrityError, "bad state"), "integrity", None),
        (sa_exc.SQLAlchemyError(f"message with {SECRET}"), "database", None),
    ],
)
def test_details_classify_errors_without_exposing_sql_or_values(error, category, sqlstate):
    details = database_error_details(error)
    assert details == {
        "error_type": type(error).__name__,
        "category": category,
        "sqlstate": sqlstate,
    }
    assert SECRET not in json.dumps(details) and "SELECT" not in json.dumps(details)


def database_events(caplog):
    events = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except ValueError:
            continue
        if payload.get("event") == "database_error":
            events.append(payload)
    return events


def test_unreachable_database_is_logged_as_connectivity_with_a_request_id(caplog):
    with TestClient(create_app(Settings(UNREACHABLE, enable_mock=True))) as client:
        response = client.get(f"/api/v1/mock/runs/{uuid4()}")
        supplied = client.get(
            f"/api/v1/mock/runs/{uuid4()}", headers={"X-Request-ID": "trace-12345678"}
        )
        unsafe = client.get(
            f"/api/v1/mock/runs/{uuid4()}", headers={"X-Request-ID": "bad id\nforged"}
        )
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "database_unavailable"
    assert body["request_id"] == response.headers["x-request-id"]
    assert supplied.headers["x-request-id"] == "trace-12345678"
    assert unsafe.headers["x-request-id"] != "bad id\nforged"
    event = database_events(caplog)[0]
    assert event["request_id"] == body["request_id"]
    assert (event["method"], event["route"]) == ("GET", "/api/v1/mock/runs/{run_id}")
    assert event["category"] == "connectivity" and event["error_type"] == "OperationalError"
    assert "hunter2" not in caplog.text and "hunter2" not in response.text


def test_integrity_errors_are_reported_as_server_errors_without_data(caplog):
    app = create_app(Settings(UNREACHABLE))

    @app.get("/test/integrity")
    def integrity_failure():
        raise wrapped(sa_exc.IntegrityError, "23505")

    with TestClient(app) as client:
        response = client.get("/test/integrity")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "database_error"
    (event,) = database_events(caplog)
    assert (event["category"], event["sqlstate"], event["route"]) == (
        "integrity",
        "23505",
        "/test/integrity",
    )
    assert SECRET not in caplog.text and "SELECT" not in caplog.text
    assert SECRET not in response.text
