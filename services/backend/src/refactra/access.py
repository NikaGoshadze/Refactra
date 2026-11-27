"""Manage invites and the waitlist.

    python -m refactra.access waitlist          # people waiting, oldest first
    python -m refactra.access list              # everyone with an access row
    python -m refactra.access invite LOGIN|ID   # give access (waitlisted or any GitHub user)
    python -m refactra.access remove LOGIN|ID   # take access away; they cannot rejoin the waitlist

Owners in REFACTRA_ALLOWED_GITHUB_USER_IDS always have access and need no row. A user who is
not on the waitlist yet is looked up through GitHub's public user API.
"""

import argparse
import re
import sys

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.models import UserAccess

_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class AccessError(ValueError):
    """An operator-facing reason the command could not complete."""


def github_user(who: str, transport: httpx.BaseTransport | None = None) -> tuple[int, str]:
    """(numeric ID, login) from GitHub's public user API."""
    path = f"/user/{who}" if who.isdigit() else f"/users/{who}"
    try:
        with httpx.Client(
            base_url="https://api.github.com",
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"},
        ) as client:
            response = client.get(path)
        if response.status_code == 404:
            raise AccessError(f"GitHub has no user {who}.")
        response.raise_for_status()
        data = response.json()
        user_id, login = data["id"], data["login"]
        if type(user_id) is not int or user_id <= 0 or not isinstance(login, str):
            raise ValueError
        return user_id, login
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise AccessError("GitHub could not confirm that user. Try again later.") from None


def resolve(session: Session, who: str, transport=None) -> tuple[int, str]:
    if not (who.isdigit() and int(who) > 0) and _LOGIN.fullmatch(who) is None:
        raise AccessError("Give a GitHub login or numeric user ID.")
    known = session.scalar(
        select(UserAccess).where(
            UserAccess.github_user_id == int(who)
            if who.isdigit()
            else func.lower(UserAccess.github_login) == who.lower()
        )
    )
    if known is not None:
        return known.github_user_id, known.github_login
    return github_user(who, transport)


def set_status(session: Session, user_id: int, login: str, status: str) -> None:
    session.execute(
        insert(UserAccess)
        .values(github_user_id=user_id, github_login=login, status=status, decided_at=func.now())
        .on_conflict_do_update(
            index_elements=[UserAccess.github_user_id],
            set_={"github_login": login, "status": status, "decided_at": func.now()},
        )
    )


def rows(session: Session, status: str | None = None) -> list[UserAccess]:
    statement = select(UserAccess).order_by(UserAccess.requested_at, UserAccess.github_user_id)
    if status:
        statement = statement.where(UserAccess.status == status)
    return list(session.scalars(statement))


def run(argv: list[str], session_factory, transport=None, out=sys.stdout) -> None:
    parser = argparse.ArgumentParser(prog="python -m refactra.access", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("waitlist", help="People waiting for an invite, oldest first.")
    commands.add_parser("list", help="Everyone with an access row.")
    for name in ("invite", "remove"):
        commands.add_parser(name).add_argument("who", help="GitHub login or numeric user ID.")
    args = parser.parse_args(argv)

    if args.command in {"waitlist", "list"}:
        with session_factory() as session:
            found = rows(session, "waitlisted" if args.command == "waitlist" else None)
        if not found:
            print("No one." if args.command == "list" else "The waitlist is empty.", file=out)
        for row in found:
            print(
                f"{row.github_login} ({row.github_user_id})  {row.status}  "
                f"since {row.requested_at:%Y-%m-%d %H:%M} UTC",
                file=out,
            )
        return
    with session_factory.begin() as session:
        user_id, login = resolve(session, args.who, transport)
        status = "invited" if args.command == "invite" else "removed"
        set_status(session, user_id, login, status)
    verb = "Invited" if args.command == "invite" else "Removed access for"
    print(f"{verb} {login} ({user_id}).", file=out)


def main() -> None:
    engine = make_engine(Settings.from_env().database_url)
    try:
        run(sys.argv[1:], sessions(engine))
    except AccessError as error:
        raise SystemExit(str(error)) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
