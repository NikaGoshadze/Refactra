import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from refactra.auth_sessions import GitHubIdentity
from refactra.github_app import GitHubAppClient, InstallationToken
from refactra.models import Run, RunCommand
from refactra.run_access import ReadAccessCache, RepositoryGrant


def grant(minutes_left=60):
    token = InstallationToken("token", 42, datetime.now(UTC) + timedelta(minutes=minutes_left))
    return RepositoryGrant(GitHubIdentity(1, "user"), 7, GitHubAppClient("client", b""), token)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_recent_positive_checks_are_reused_until_they_expire():
    clock, checks = Clock(), []
    cache = ReadAccessCache(ttl_seconds=30, clock=clock)

    def check():
        checks.append(1)
        return grant()

    first = cache.get_or_check(("user", 42), check)
    assert cache.get_or_check(("user", 42), check) is first and len(checks) == 1
    cache.get_or_check(("other-session", 42), check)
    assert len(checks) == 2  # Keys are separate.
    clock.now += 31
    cache.get_or_check(("user", 42), check)
    assert len(checks) == 3


def test_nearly_expired_tokens_and_denials_are_never_reused():
    cache, checks = ReadAccessCache(), []

    def expiring():
        checks.append(1)
        return grant(minutes_left=4)

    cache.get_or_check(("user", 42), expiring)
    cache.get_or_check(("user", 42), expiring)
    assert len(checks) == 2

    def denied():
        checks.append(1)
        raise PermissionError("denied")

    for _ in range(2):
        with pytest.raises(PermissionError):
            cache.get_or_check(("denied", 42), denied)
    assert len(checks) == 4


def test_concurrent_checks_for_one_key_share_a_single_round_trip():
    cache, checks = ReadAccessCache(), []
    release = threading.Event()

    def slow_check():
        checks.append(1)
        release.wait(5)
        return grant()

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(cache.get_or_check(("user", 42), slow_check))
        )
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(checks) == 1 and len(results) == 5 and len({id(item) for item in results}) == 1


def test_command_history_queries_use_the_per_run_index(session_factory):
    with session_factory.begin() as session:
        runs = [Run(task_text="Load") for _ in range(400)]
        session.add_all(runs)
        session.flush()
        session.add_all(
            RunCommand(run_id=run.id, kind=kind, dedupe_key=f"{kind}:{run.id}")
            for run in runs
            for kind in ("APPLY_PLAN_APPROVAL", "GENERATE_CANDIDATE", "VALIDATE_CANDIDATE")
        )
        target = runs[123].id
    with session_factory.begin() as session:
        session.execute(text("ANALYZE run_commands"))
        plan = "\n".join(
            session.scalars(
                text(
                    "EXPLAIN SELECT kind, status FROM run_commands "
                    "WHERE run_id = :run_id "
                    "AND kind IN ('GENERATE_CANDIDATE', 'VALIDATE_CANDIDATE') "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"run_id": target},
            )
        )
    assert "ix_commands_run_created" in plan, plan
