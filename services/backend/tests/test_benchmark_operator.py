import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from refactra import benchmark_operator as operator
from refactra.benchmark_summary import summarize
from refactra.models import ModelReservation

from .test_benchmark_runner import providers


@pytest.fixture
def output(tmp_path):
    profiles = {"openai": {"model": "openai-fixture"}, "deepseek": {"model": "deepseek-fixture"}}
    (tmp_path / "manifest.json").write_text(
        json.dumps({"suite_hash": "fixture", "profiles": profiles})
    )
    for name, status, cost, reserved, elapsed in [
        ("openai", "completed", "0.000165", "0.013575", 2),
        ("openai", "provider_failed", None, "0.013575", 4),
        ("deepseek", "completed", "0.000054", "0.00363", 1),
    ]:
        attempt = str(uuid4())
        row = {
            "attempt_id": attempt,
            "provider": name,
            "status": status,
            "estimated_cost_usd": cost,
            "reserved_usd": reserved,
            "elapsed_seconds": elapsed,
            "input_tokens": 100 if cost else None,
            "output_tokens": 20 if cost else None,
            "cached_input_tokens": 10 if name == "openai" and cost else None,
            "text": "DO_NOT_PRINT_RESPONSE",
        }
        (tmp_path / f"{attempt}.json").write_text(json.dumps(row))
    return tmp_path


def test_summary_distinguishes_missing_results_known_cost_and_unknown_holds(output):
    report = summarize(output)
    assert report["recorded_requests"] == 3 and report["quality_score"] is None
    first = report["providers"]["openai"]
    assert first["missing_results"] == 13 and first["completion_rate"] == 1 / 15
    assert first["recorded_settled_estimate_usd"] == "0.000165"
    assert first["recorded_holds_usd"] == "0.013575" and first["usage_unknown_records"] == 1
    assert first["confirmed_input_tokens"] == 100 and first["reported_cached_tokens"] == 10
    assert first["median_attempt_seconds"] == 3 and first["worst_attempt_seconds"] == 4
    assert "DO_NOT_PRINT_RESPONSE" not in json.dumps(report)


def test_summary_command_does_not_load_credentials_or_providers(output, monkeypatch, capsys):
    def forbidden(*args):
        pytest.fail("Summary must not load settings or providers")

    monkeypatch.setattr(operator, "load_settings", forbidden)
    monkeypatch.setattr(operator, "configured_providers", forbidden)
    assert operator.main(["--summary", str(output)]) == 0
    assert (output / "summary.json").exists()
    assert "DO_NOT_PRINT_RESPONSE" not in capsys.readouterr().out


def test_configuration_is_literal_allowlisted_and_environment_takes_precedence(
    tmp_path, monkeypatch
):
    for key in operator.KEYS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        'REFACTRA_DATABASE_URL="postgresql+psycopg://local"\n'
        "REFACTRA_OPENAI_API_KEY='file-key'\nREFACTRA_DEEPSEEK_API_KEY=other-key\n"
        "UNRELATED_SECRET=excluded\n"
    )
    monkeypatch.setenv("REFACTRA_OPENAI_API_KEY", "process-key")
    settings = operator.load_settings(path)
    assert settings["REFACTRA_OPENAI_API_KEY"] == "process-key"
    assert settings["REFACTRA_DATABASE_URL"] == "postgresql+psycopg://local"
    assert "UNRELATED_SECRET" not in settings


def test_explicit_run_is_required_and_failures_do_not_print_credentials(
    tmp_path, monkeypatch, capsys
):
    with pytest.raises(SystemExit) as error:
        operator.main([])
    assert error.value.code == 2
    for key in operator.KEYS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text("REFACTRA_OPENAI_API_KEY=SECRET_SENTINEL\n")
    assert operator.main(["--run", "--env-file", str(path)]) == 1
    captured = capsys.readouterr()
    assert "SECRET_SENTINEL" not in captured.out + captured.err
    with pytest.raises(ValueError, match="local PostgreSQL"):
        operator.execute(
            {"REFACTRA_DATABASE_URL": "postgresql+psycopg://remote.invalid/db"}, tmp_path
        )


def test_operator_migrates_isolated_schema_and_resume_does_not_resend(
    database, tmp_path, monkeypatch, capsys
):
    schema, calls = f"refactra_test_cli_{uuid4().hex}", []
    adapters = providers(calls)
    monkeypatch.setattr(operator, "SCHEMA", schema)
    monkeypatch.setattr(operator, "configured_providers", lambda settings: adapters)
    monkeypatch.chdir(tmp_path)
    for key in operator.KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REFACTRA_DATABASE_URL", database.url.render_as_string(hide_password=False))
    monkeypatch.setenv("REFACTRA_OPENAI_API_KEY", "SECRET_SENTINEL")
    monkeypatch.setenv("REFACTRA_DEEPSEEK_API_KEY", "SECRET_SENTINEL")
    try:
        assert operator.main(["--run"]) == 0
        assert operator.main(["--run"]) == 0
        assert len(calls) == 30
        with database.connect() as connection:
            assert (
                connection.scalar(text(f'SELECT count(*) FROM "{schema}".model_reservations')) == 30
            )
            assert connection.scalar(select(func.count()).select_from(ModelReservation)) == 0
        printed = capsys.readouterr().out
        assert "SECRET_SENTINEL" not in printed and "Untrusted model answer" not in printed
    finally:
        with database.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
