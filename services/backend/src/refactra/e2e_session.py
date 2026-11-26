"""Issue or revoke a Refactra session for the live end-to-end test.

Operator tool for a protected local or CI environment with direct database access; it is not
exposed through the API. It replaces only GitHub's interactive sign-in page: every repository
request made with the session still rechecks the allowlist and the user's current GitHub write
access. The token is written to a file, never printed, and the session expires after 8 hours.
"""

import argparse
import os
from pathlib import Path

from refactra.auth_sessions import GitHubIdentity, issue_session, revoke_session
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.repository_access import RepositoryAccessPolicy


def _user_id(requested: int | None) -> int:
    allowed = RepositoryAccessPolicy.from_env().allowed_user_ids
    if requested is not None:
        if requested not in allowed:
            raise SystemExit("The user is not in REFACTRA_ALLOWED_GITHUB_USER_IDS.")
        return requested
    if len(allowed) != 1:
        raise SystemExit("Pass --user-id: the allowlist does not name exactly one user.")
    return next(iter(allowed))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--issue", type=Path, metavar="TOKEN_FILE", help="Write a new token here.")
    action.add_argument("--revoke", type=Path, metavar="TOKEN_FILE", help="Revoke and delete.")
    parser.add_argument("--user-id", type=int, help="Allowlisted GitHub user ID.")
    args = parser.parse_args(argv)
    if args.issue:
        _issue(args.issue, _user_id(args.user_id))
    else:
        _revoke(args.revoke)


def _issue(path: Path, user_id: int) -> None:
    # Claim the file first so a session is never created without a place to keep its token.
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f"{path} already exists; revoke or remove it first.") from None
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        try:
            engine = make_engine(Settings.from_env().database_url)
            try:
                with sessions(engine).begin() as session:
                    token = issue_session(session, GitHubIdentity(user_id, "e2e-test"))
            finally:
                engine.dispose()
            output.write(token)
        except BaseException:
            output.close()
            path.unlink()
            raise
    print(f"Session written to {path}; it expires in 8 hours.")


def _revoke(path: Path) -> None:
    token = path.read_text(encoding="ascii").strip()
    engine = make_engine(Settings.from_env().database_url)
    try:
        with sessions(engine).begin() as session:
            revoke_session(session, token)
    finally:
        engine.dispose()
    path.unlink()
    print("Session revoked.")


if __name__ == "__main__":
    main()
