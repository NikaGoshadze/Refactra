import json
import logging
from uuid import uuid4

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

import refactra.worker as worker
from refactra.publication_target import PUBLISH_CANDIDATE
from refactra.refactoring_plan import APPLY_PLAN_APPROVAL, GENERATE_PLAN
from refactra.repository_analysis_worker import GENERATE_CANDIDATE, WORKFLOW
from refactra.repository_candidate import VALIDATE_CANDIDATE
from refactra.snapshot_preparation import PREPARE_SNAPSHOT
from refactra.worker_health import record_heartbeat


def run_main(monkeypatch, engine, *args, lock=None):
    # Without an engine, any database connection attempt fails the test.
    monkeypatch.setattr(
        worker, "make_engine", (lambda _url: engine) if engine is not None else no_database
    )
    monkeypatch.setattr(worker, "WORKER_LOCK", lock or uuid4().int >> 65)
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    monkeypatch.setattr("sys.argv", ["worker", *args])
    worker.main()


def no_database(_url):
    pytest.fail("Startup errors must be reported before connecting to the database.")


@pytest.fixture
def directories(tmp_path):
    source, artifacts = tmp_path / "clone", tmp_path / "artifacts"
    source.mkdir()
    artifacts.mkdir()
    return ["--candidate-source", str(source), "--candidate-artifacts", str(artifacts)]


@pytest.fixture
def recorded(monkeypatch):
    """Record what the loop claims and how it reports its mode, without real processing."""
    calls = {"claims": [], "modes": [], "reconciled": 0}

    def claim(_session_factory, _worker_id, *, workflow, kind):
        calls["claims"].append((workflow, kind))

    def reconcile(_session_factory):
        calls["reconciled"] += 1
        return 0

    monkeypatch.setattr(worker, "claim_command", claim)
    monkeypatch.setattr(
        worker, "record_heartbeat", lambda _f, _w, mode, **_: calls["modes"].append(mode)
    )
    monkeypatch.setattr(worker, "clear_heartbeat", lambda *_: None)
    monkeypatch.setattr(worker, "reconcile_validation_containers", reconcile)
    monkeypatch.setattr(worker, "OpenAIAnalysisProvider", lambda key: ("provider", key))
    monkeypatch.setenv("REFACTRA_OPENAI_API_KEY", "fixture-key")
    monkeypatch.setenv("REFACTRA_ENABLE_MOCK", "true")
    return calls


@pytest.mark.parametrize(
    "args",
    [
        ["--repository-workflow"],
        ["--repository-analysis", "--candidate-source", "."],
        ["--repository-candidate", "--candidate-source", "missing", "--candidate-artifacts", "x"],
        ["--repository-analysis", "--repository-approval"],
    ],
)
def test_invalid_mode_and_directory_flags_exit_before_connecting(monkeypatch, args):
    with pytest.raises(SystemExit) as stopped:
        run_main(monkeypatch, None, *args)
    assert stopped.value.code == 2


@pytest.mark.parametrize(
    ("args", "env", "message"),
    [
        (["--repository-analysis"], {}, "Repository model work requires REFACTRA_OPENAI_API_KEY."),
        ([], {"REFACTRA_ENABLE_MOCK": "false"}, "Mock mode is disabled."),
    ],
)
def test_missing_key_or_disabled_mock_mode_stop_with_a_clear_message(
    monkeypatch, args, env, message
):
    monkeypatch.delenv("REFACTRA_OPENAI_API_KEY", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as stopped:
        run_main(monkeypatch, None, *args)
    assert stopped.value.code == message


def test_health_flag_reports_the_heartbeat(monkeypatch, database, session_factory):
    with pytest.raises(SystemExit) as missing:
        run_main(monkeypatch, database, "--health")
    assert missing.value.code == 1
    record_heartbeat(session_factory, "worker-a", "mock", started=True)
    with pytest.raises(SystemExit) as running:
        run_main(monkeypatch, database, "--health")
    assert running.value.code == 0


@pytest.mark.parametrize(
    ("flags", "with_directories", "mode", "kinds"),
    [
        ([], False, "mock", ["START"]),
        (["--repository-analysis"], False, "repository_analysis", [PREPARE_SNAPSHOT, "START"]),
        (["--repository-approval"], False, "repository_approval", [APPLY_PLAN_APPROVAL]),
        (
            ["--repository-candidate"],
            False,
            "repository_candidate",
            [GENERATE_CANDIDATE, APPLY_PLAN_APPROVAL],
        ),
        (
            ["--repository-candidate"],
            True,
            "repository_publication",
            [GENERATE_CANDIDATE, VALIDATE_CANDIDATE, PUBLISH_CANDIDATE, APPLY_PLAN_APPROVAL],
        ),
        (
            ["--repository-workflow"],
            True,
            "repository_workflow",
            [
                GENERATE_CANDIDATE,
                VALIDATE_CANDIDATE,
                PUBLISH_CANDIDATE,
                APPLY_PLAN_APPROVAL,
                GENERATE_PLAN,
                PREPARE_SNAPSHOT,
                "START",
            ],
        ),
    ],
)
def test_each_mode_claims_its_stages_in_order(
    monkeypatch, database, recorded, directories, flags, with_directories, mode, kinds
):
    run_main(monkeypatch, database, "--once", *flags, *(directories if with_directories else []))
    workflow = "mock-v0.1" if mode == "mock" else WORKFLOW
    assert recorded["claims"] == [(workflow, kind) for kind in kinds]
    assert recorded["modes"] == [mode]
    # Only workers with an artifact directory run validation, so only they reconcile runners.
    assert recorded["reconciled"] == (1 if with_directories else 0)


@pytest.mark.parametrize(
    ("flags", "kind", "processor"),
    [
        ([], "START", "process_command"),
        (["--repository-analysis"], "START", "process_repository_analysis"),
        (["--repository-workflow"], "START", "process_repository_analysis"),
        (["--repository-workflow"], PREPARE_SNAPSHOT, "process_snapshot_preparation"),
        (["--repository-workflow"], GENERATE_PLAN, "process_initial_plan"),
        (["--repository-workflow"], APPLY_PLAN_APPROVAL, "process_repository_plan_approval"),
        (["--repository-workflow"], GENERATE_CANDIDATE, "process_repository_candidate"),
        (["--repository-workflow"], VALIDATE_CANDIDATE, "process_candidate_validation"),
        (["--repository-workflow"], PUBLISH_CANDIDATE, "process_approved_publication"),
    ],
)
def test_each_claimed_stage_reaches_its_processor(
    monkeypatch, database, recorded, directories, caplog, flags, kind, processor
):
    caplog.set_level(logging.INFO, logger="refactra.worker")
    command_id = uuid4()
    routed = []
    names = [
        "process_command",
        "process_repository_analysis",
        "process_snapshot_preparation",
        "process_initial_plan",
        "process_repository_plan_approval",
        "process_repository_candidate",
        "process_candidate_validation",
        "process_approved_publication",
    ]
    for name in names:

        def record(*args, _name=name, **kwargs):
            routed.append((_name, args, kwargs))
            return False  # Report a lost claim to exercise that log path too.

        monkeypatch.setattr(worker, name, record)

    def claim(_session_factory, _worker_id, *, workflow, kind: str):
        recorded["claims"].append((workflow, kind))
        return command_id if kind == target else None

    target = kind
    monkeypatch.setattr(worker, "claim_command", claim)
    extra = directories if "--repository-workflow" in flags else []
    run_main(monkeypatch, database, "--once", *flags, *extra)

    ((name, args, kwargs),) = routed
    assert name == processor and args[1] == command_id
    if processor in {"process_repository_candidate", "process_initial_plan"}:
        assert args[3] == ("provider", "fixture-key")
    if processor == "process_repository_analysis":
        assert kwargs["queue_plan"] == ("--repository-workflow" in flags)
    if processor in {"process_candidate_validation", "process_approved_publication"}:
        assert kwargs["artifacts"] is not None and kwargs["source"] is not None
    if processor == "process_candidate_validation":
        assert recorded["reconciled"] == 2  # At startup, and again before validating.
    events = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    assert {"event": "command_claim_lost", "stage": kind}.items() <= events[-1].items()


def test_a_held_execution_slot_stops_a_second_worker(monkeypatch, database, recorded):
    lock = uuid4().int >> 65
    with database.connect().execution_options(isolation_level="AUTOCOMMIT") as other:
        assert other.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock})
        with pytest.raises(SystemExit) as stopped:
            run_main(monkeypatch, database, "--once", lock=lock)
        other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})
    assert stopped.value.code == "Another Refactra worker holds the local execution slot."
    assert recorded["claims"] == []


def test_a_database_failure_stops_the_worker_with_safe_details(
    monkeypatch, database, recorded, caplog
):
    def unavailable(*_args, **_kwargs):
        raise sa_exc.OperationalError("SELECT secret_value", {}, Exception("password=hunter2"))

    monkeypatch.setattr(worker, "claim_command", unavailable)
    with pytest.raises(SystemExit) as stopped:
        run_main(monkeypatch, database, "--once")
    assert stopped.value.code == 1
    (stop,) = [
        json.loads(r.getMessage()) for r in caplog.records if "worker_stopped" in r.getMessage()
    ]
    assert (stop["reason"], stop["category"], stop["error_type"]) == (
        "database_error",
        "connectivity",
        "OperationalError",
    )
    assert "hunter2" not in caplog.text and "secret_value" not in caplog.text
