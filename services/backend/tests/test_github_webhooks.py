import hmac
import json
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from refactra.api import create_app
from refactra.auth_sessions import GitHubIdentity
from refactra.config import Settings
from refactra.models import Run, RunCommand, RunEvent
from refactra.publication_target import PUBLICATION_PR_OPENED
from refactra.repository_revision import RepositoryRevision
from refactra.repository_runs import create_repository_run
from refactra.run_history import list_runs

SECRET = "w" * 43
PATH = "/api/v1/github/webhook"


def make_run(session_factory, *, installation=77, repository=42, task="run"):
    with session_factory.begin() as session:
        run = create_repository_run(
            session,
            identity=GitHubIdentity(123, "fixture"),
            installation_id=installation,
            revision=RepositoryRevision(repository, "fixture/repo", "main", "main", "a" * 40),
            task_text=task,
        )
        session.add(RunCommand(run_id=run.id, kind="START", dedupe_key=str(uuid4())))
    return run.id


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setenv("REFACTRA_GITHUB_WEBHOOK_SECRET", SECRET)
    with TestClient(create_app(Settings("unused"), database)) as client:
        yield client


def deliver(client, event, payload, *, secret=SECRET, signature=None):
    body = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), body, sha256).hexdigest()
    return client.post(
        PATH,
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": str(uuid4()),
            "X-Hub-Signature-256": signature if signature is not None else f"sha256={digest}",
            "Content-Type": "application/json",
        },
    )


def statuses(session_factory, *run_ids):
    with session_factory() as session:
        return [session.get(Run, run_id).status for run_id in run_ids]


def test_unsigned_or_wrongly_signed_deliveries_are_refused(client, session_factory):
    run = make_run(session_factory)
    payload = {"action": "deleted", "installation": {"id": 77}}
    assert deliver(client, "installation", payload, signature="").status_code == 401
    assert deliver(client, "installation", payload, secret="other").status_code == 401
    assert deliver(client, "installation", payload, signature="sha1=abc").status_code == 401
    assert statuses(session_factory, run) == ["CREATED"]


def test_webhooks_are_off_without_a_secret(database, monkeypatch):
    monkeypatch.delenv("REFACTRA_GITHUB_WEBHOOK_SECRET", raising=False)
    with TestClient(create_app(Settings("unused"), database)) as client:
        assert deliver(client, "ping", {"zen": "hi"}).status_code == 503


def test_an_uninstall_stops_only_that_installations_unfinished_runs(client, session_factory):
    affected = make_run(session_factory, task="affected")
    elsewhere = make_run(session_factory, installation=88, task="elsewhere")
    published = make_run(session_factory, task="published")
    with session_factory.begin() as session:
        session.add(
            RunEvent(
                run_id=published,
                event_type=PUBLICATION_PR_OPENED,
                payload={"pull_request_number": 1},
            )
        )
    response = deliver(client, "installation", {"action": "deleted", "installation": {"id": 77}})
    assert response.status_code == 200 and response.json() == {
        "status": "processed",
        "stopped_runs": 1,
    }
    assert statuses(session_factory, affected, elsewhere, published) == [
        "FAILED",
        "CREATED",
        "CREATED",
    ]
    with session_factory() as session:
        commands = session.scalars(select(RunCommand.status).where(RunCommand.run_id == affected))
        assert set(commands) == {"FAILED"}
        page = list_runs(session, 123)
    stopped = next(item for item in page.runs if item.task_summary == "affected")
    assert stopped.stage == "failed"
    assert stopped.stop_reason == "Refactra's GitHub App was removed from this repository."
    # A redelivery is harmless.
    again = deliver(client, "installation", {"action": "deleted", "installation": {"id": 77}})
    assert again.json()["stopped_runs"] == 0


def test_removing_repositories_stops_only_their_runs(client, session_factory):
    removed = make_run(session_factory, repository=42)
    kept = make_run(session_factory, repository=43)
    payload = {
        "action": "removed",
        "installation": {"id": 77},
        "repositories_removed": [{"id": 42, "full_name": "fixture/repo"}, {"id": "bad"}],
    }
    assert deliver(client, "installation_repositories", payload).json()["stopped_runs"] == 1
    assert statuses(session_factory, removed, kept) == ["FAILED", "CREATED"]


@pytest.mark.parametrize(
    "event,payload",
    [
        ("ping", {"zen": "Keep it logically awesome."}),
        ("installation", {"action": "created", "installation": {"id": 77}}),
        ("installation_repositories", {"action": "added", "installation": {"id": 77}}),
        ("push", {"installation": {"id": 77}}),
    ],
)
def test_other_events_are_acknowledged_and_ignored(client, session_factory, event, payload):
    run = make_run(session_factory)
    assert deliver(client, event, payload).status_code == 202
    assert statuses(session_factory, run) == ["CREATED"]


def test_unreadable_payloads_and_oversized_deliveries_are_refused(client, session_factory):
    assert deliver(client, "installation", {"action": "deleted"}).status_code == 400
    assert (
        deliver(
            client, "installation", {"action": "deleted", "installation": {"id": "x"}}
        ).status_code
        == 400
    )
    many = [{"id": n, "full_name": f"org/repository-number-{n}"} for n in range(1, 600)]
    large = {"action": "removed", "installation": {"id": 77}, "repositories_removed": many}
    assert deliver(client, "installation_repositories", large).status_code == 200  # Over 16 KiB.
    huge = {"action": "removed", "installation": {"id": 77}, "padding": "x" * (1024 * 1024)}
    assert deliver(client, "installation_repositories", huge).status_code == 413
