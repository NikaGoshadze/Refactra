import sys
from base64 import urlsafe_b64encode

import pytest
from fastapi.testclient import TestClient

import refactra.worker as worker
from refactra.api import create_app
from refactra.config import Settings
from refactra.production import (
    ENVIRONMENT_ENV,
    ProductionConfigError,
    environment,
    production_problems,
    require_production_ready,
)

SECRETS = {
    "REFACTRA_DATABASE_URL": "postgresql+psycopg://refactra:prod-password-123@db:5432/refactra",
    "REFACTRA_GITHUB_CLIENT_SECRET": "client-secret-value",
    "REFACTRA_OAUTH_STATE_SECRET": "s" * 43,
    "REFACTRA_TOKEN_ENCRYPTION_KEY": urlsafe_b64encode(bytes(range(32))).decode(),
}


@pytest.fixture
def production(monkeypatch, tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("pem")
    settings = {
        ENVIRONMENT_ENV: "production",
        "REFACTRA_ENABLE_MOCK": "false",
        "REFACTRA_ALLOWED_GITHUB_USER_IDS": "12345",
        "REFACTRA_GITHUB_CLIENT_ID": "Iv1.client",
        "REFACTRA_GITHUB_PRIVATE_KEY_PATH": str(key),
        "REFACTRA_GITHUB_CALLBACK_URL": "https://refactra.example/api/v1/auth/github/callback",
        "REFACTRA_GITHUB_APP_SLUG": "refactra-app",
        "REFACTRA_VALIDATION_RUNTIME": "runsc",
        **SECRETS,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_development_is_the_default_and_unknown_values_are_rejected(monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_ENV, raising=False)
    assert environment() == "development"
    assert require_production_ready("api") == "development"
    monkeypatch.setenv(ENVIRONMENT_ENV, "staging")
    with pytest.raises(ProductionConfigError, match="development or production"):
        environment()


def test_safe_production_settings_start(production):
    assert require_production_ready("api") == "production"
    assert require_production_ready("worker") == "production"


@pytest.mark.parametrize(
    "name,value,message",
    [
        ("REFACTRA_ENABLE_MOCK", "true", "REFACTRA_ENABLE_MOCK"),
        (
            "REFACTRA_DATABASE_URL",
            "postgresql+psycopg://refactra:refactra_local@db/refactra",
            "REFACTRA_DATABASE_URL",
        ),
        ("REFACTRA_ALLOWED_GITHUB_USER_IDS", "", "at least one user"),
        ("REFACTRA_ALLOWED_GITHUB_USER_IDS", "octocat", "numeric"),
        ("REFACTRA_GITHUB_PRIVATE_KEY_PATH", "missing.pem", "private key"),
        ("REFACTRA_GITHUB_CALLBACK_URL", "http://refactra.example/cb", "https://"),
        ("REFACTRA_GITHUB_CALLBACK_URL", "https://localhost/cb", "https://"),
        ("REFACTRA_GITHUB_CLIENT_SECRET", "", "CLIENT_SECRET"),
        ("REFACTRA_OAUTH_STATE_SECRET", "short", "32 random"),
        ("REFACTRA_TOKEN_ENCRYPTION_KEY", "", "TOKEN_ENCRYPTION_KEY must be set"),
        ("REFACTRA_TOKEN_ENCRYPTION_KEY", "bad", "URL-safe base64"),
        ("REFACTRA_GITHUB_APP_SLUG", "", "APP_SLUG must be set"),
        ("REFACTRA_GITHUB_APP_SLUG", "../x", "URL name"),
    ],
)
def test_each_unsafe_setting_stops_the_api_without_leaking_values(production, name, value, message):
    production.setenv(name, value)
    with pytest.raises(ProductionConfigError) as error:
        require_production_ready("api")
    assert message in str(error.value)
    assert not any(secret in str(error.value) for secret in SECRETS.values())


def test_the_worker_needs_only_its_own_settings(production):
    for name in ("REFACTRA_GITHUB_CALLBACK_URL", "REFACTRA_GITHUB_CLIENT_SECRET"):
        production.delenv(name)
    assert production_problems("worker") == []
    assert len(production_problems("api")) == 2


@pytest.mark.parametrize("value", ["", "runc"])
def test_the_production_worker_requires_gvisor(production, value):
    production.setenv("REFACTRA_VALIDATION_RUNTIME", value)
    assert any(
        "REFACTRA_VALIDATION_RUNTIME" in problem for problem in production_problems("worker")
    )
    assert not any("VALIDATION_RUNTIME" in problem for problem in production_problems("api"))


def test_the_api_refuses_to_start_and_reports_its_environment(production, database):
    production.setenv("REFACTRA_ENABLE_MOCK", "true")
    with pytest.raises(ProductionConfigError):
        create_app(Settings("unused"), database)
    production.setenv(ENVIRONMENT_ENV, "development")
    with TestClient(create_app(Settings("unused"), database)) as client:
        assert client.get("/api/v1/capabilities").json()["environment"] == "development"


def test_the_worker_exits_with_the_configuration_code(production, capsys):
    production.setenv("REFACTRA_ENABLE_MOCK", "true")
    production.setattr(sys, "argv", ["refactra-worker", "--once"])
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert stopped.value.code == 2
    assert "REFACTRA_ENABLE_MOCK" in capsys.readouterr().err
