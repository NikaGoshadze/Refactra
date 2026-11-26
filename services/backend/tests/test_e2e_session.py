import pytest
from sqlalchemy import func, select

import refactra.e2e_session as e2e_session
from refactra.auth_sessions import resolve_session
from refactra.models import AuthSession


@pytest.fixture
def run(database, monkeypatch):
    monkeypatch.setattr(e2e_session, "make_engine", lambda url: database)
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "4242")
    return e2e_session.main


def stored_sessions(session_factory):
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(AuthSession))


def test_issue_writes_a_working_token_and_revoke_ends_it(run, session_factory, tmp_path, capsys):
    token_file = tmp_path / "session.token"
    run(["--issue", str(token_file)])
    token = token_file.read_text(encoding="ascii")
    assert token not in capsys.readouterr().out
    with session_factory() as session:
        identity = resolve_session(session, token)
    assert identity is not None and identity.user_id == 4242

    run(["--revoke", str(token_file)])
    assert not token_file.exists()
    with session_factory() as session:
        assert resolve_session(session, token) is None


def test_only_an_allowlisted_user_gets_a_session(run, session_factory, tmp_path, monkeypatch):
    token_file = tmp_path / "session.token"
    with pytest.raises(SystemExit, match="not in REFACTRA_ALLOWED_GITHUB_USER_IDS"):
        run(["--issue", str(token_file), "--user-id", "7"])
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "4242,7")
    with pytest.raises(SystemExit, match="Pass --user-id"):
        run(["--issue", str(token_file)])
    assert not token_file.exists() and stored_sessions(session_factory) == 0

    run(["--issue", str(token_file), "--user-id", "7"])
    with session_factory() as session:
        assert resolve_session(session, token_file.read_text(encoding="ascii")).user_id == 7


def test_an_existing_token_file_is_never_overwritten(run, session_factory, tmp_path):
    token_file = tmp_path / "session.token"
    token_file.write_text("keep", encoding="ascii")
    with pytest.raises(SystemExit, match="already exists"):
        run(["--issue", str(token_file)])
    assert token_file.read_text(encoding="ascii") == "keep"
    assert stored_sessions(session_factory) == 0


def test_a_failed_issue_leaves_no_token_file(run, tmp_path, monkeypatch):
    def fail(session, identity):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(e2e_session, "issue_session", fail)
    token_file = tmp_path / "session.token"
    with pytest.raises(RuntimeError):
        run(["--issue", str(token_file)])
    assert not token_file.exists()
