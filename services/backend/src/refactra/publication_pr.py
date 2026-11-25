"""Open or recover the draft pull request for an approved GitHub branch."""

import argparse
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.diagnostics import diagnostic_id
from refactra.github_app import GitHubAppError
from refactra.models import Run, RunEvent
from refactra.publication_branch import load_approved_target, push_approved_branch
from refactra.publication_target import (
    PUBLICATION_BRANCH_PUSHED,
    PUBLICATION_PR_OPENED,
    PUBLISH_CANDIDATE,
    PublicationTargetError,
    load_publication_approval,
    publication_command_key,
)
from refactra.repository_analysis_worker import _claimed_command
from refactra.repository_plan_worker import authorize_run_repository
from refactra.repository_revision import resolve_revision


def _publication_event(session: Session, run_id: UUID, kind: str) -> dict | None:
    return session.scalar(
        select(RunEvent.payload)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == kind)
        .order_by(RunEvent.id)
        .limit(1)
    )


def _draft_number(pr: dict, run: Run, head: str, commit: str) -> int:
    number = pr["number"]
    if (
        type(number) is not int
        or number <= 0
        or pr["state"] != "open"
        or pr["draft"] is not True
        or (pr["head"]["repo"]["id"], pr["head"]["ref"], pr["head"]["sha"])
        != (run.github_repository_id, head, commit)
        or (pr["base"]["repo"]["id"], pr["base"]["ref"], pr["base"]["sha"])
        != (run.github_repository_id, run.base_branch, run.base_sha)
    ):
        raise PublicationTargetError("The GitHub pull request does not match the approved draft.")
    return number


def _open_or_find_draft(
    client: httpx.Client,
    repository: str,
    run: Run,
    head: str,
    commit: str,
    validation_warnings: str,
) -> int:
    path = f"/repos/{repository}/pulls"
    params = {"state": "all", "head": f"{repository.split('/')[0]}:{head}", "per_page": 2}

    def existing_draft() -> int | None:
        response = client.get(path, params=params)
        response.raise_for_status()
        entries = response.json()
        if not isinstance(entries, list) or len(entries) > 1:
            raise PublicationTargetError("GitHub returned an ambiguous pull request listing.")
        return _draft_number(entries[0], run, head, commit) if entries else None

    try:
        if number := existing_draft():
            return number
        response = client.post(
            path,
            json={
                "title": f"Refactra candidate {commit[:12]}",
                "head": head,
                "base": run.base_branch,
                "draft": True,
                "body": f"Approved commit `{commit}`\n\nPinned base `{run.base_sha}`"
                + validation_warnings,
            },
        )
        if response.status_code == 201:
            return _draft_number(response.json(), run, head, commit)
        if response.status_code == 422 and (number := existing_draft()):
            return number
    except (httpx.HTTPError, KeyError, TypeError):
        pass
    raise GitHubAppError(
        "GitHub could not confirm the draft. Retry to check the existing branch."
    ) from None


def open_approved_draft(session_factory, run_id: UUID) -> dict:
    with session_factory() as session:
        run, target_sha, commit, head, result = load_approved_target(session, run_id)
        branch = _publication_event(session, run_id, PUBLICATION_BRANCH_PUSHED)
        warnings = [
            f"- `{check_id}` · {check['status']}"
            for check in json.loads(result.document)["validation"]
            if check["status"] in {"UNAVAILABLE", "FAILED"}
            and (check_id := diagnostic_id(check.get("check_id")))
        ]
        validation_warnings = (
            "\n\n### Validation warnings\n\n" + "\n".join(warnings) if warnings else ""
        )
    if branch is None or (branch["target_sha256"], branch["commit_sha"], branch["head_branch"]) != (
        target_sha,
        commit,
        head,
    ):
        raise PublicationTargetError("Push the approved branch before opening its draft PR.")
    app, reader = authorize_run_repository(run)
    with session_factory.begin() as session:
        # Serialize draft creation and its receipt for this run.
        session.get(Run, run_id, with_for_update=True)
        receipt = _publication_event(session, run_id, PUBLICATION_PR_OPENED)
        if receipt is not None:
            if any(receipt.get(key) != value for key, value in branch.items()):
                raise PublicationTargetError(
                    "The recorded pull request does not match the approved branch."
                )
            return receipt

        base = resolve_revision(reader, base_branch=run.base_branch, transport=app.transport)
        tip = resolve_revision(reader, base_branch=head, transport=app.transport)
        if (base.full_name, tip.full_name, base.commit_sha, tip.commit_sha) != (
            branch["repository"],
            branch["repository"],
            run.base_sha,
            commit,
        ):
            raise PublicationTargetError(
                "The GitHub branches no longer match the approved commits."
            )
        writer = app.mint_pull_request_token(run.github_installation_id, run.github_repository_id)
        with httpx.Client(
            base_url="https://api.github.com",
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
            transport=app.transport,
            headers={
                "Authorization": f"Bearer {writer.value}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        ) as client:
            number = _open_or_find_draft(
                client, base.full_name, run, head, commit, validation_warnings
            )
        receipt = {
            **branch,
            "pull_request_number": number,
            "pull_request_url": f"https://github.com/{base.full_name}/pull/{number}",
        }
        session.add(RunEvent(run_id=run_id, event_type=PUBLICATION_PR_OPENED, payload=receipt))
    return receipt


def process_approved_publication(
    session_factory,
    command_id: UUID,
    worker_id: str,
    *,
    artifacts: Path,
    source: Path | None = None,
) -> bool:
    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        now = session.scalar(select(func.now()))
        if command is None or command.kind != PUBLISH_CANDIDATE or command.lease_until <= now:
            return False
        run_id, attempt = command.run_id, command.attempts
        approval = load_publication_approval(session, run_id)
        valid = approval is not None and command.dedupe_key == publication_command_key(
            run_id, approval.target_sha256
        )
        pushed = _publication_event(session, run_id, PUBLICATION_BRANCH_PUSHED)

    code = "publication_approval_mismatch"
    success = retry = False
    if valid:
        try:
            if pushed is None:
                code = "publication_push_failed"
                artifact = artifacts / f"{run_id}-r{approval.result_revision}"
                push_approved_branch(session_factory, run_id, artifact, source=source)
            code = "publication_draft_failed"
            open_approved_draft(session_factory, run_id)
            success = True
        except GitHubAppError:
            retry = attempt < 2
        except (HTTPException, KeyError, OSError, TypeError, ValueError):
            # Persist the stage without copying credentials or upstream responses.
            pass

    with session_factory.begin() as session:
        command = _claimed_command(session, command_id, worker_id)
        if command is None or command.attempts != attempt:
            return False
        now = session.scalar(select(func.now()))
        command.lease_until = None
        if retry:
            command.status = "PENDING"
            command.claimed_by = None
            command.available_at = now + timedelta(seconds=5)
        else:
            command.status = "DONE" if success else "FAILED"
            command.completed_at = now
        if not success:
            session.add(
                RunEvent(
                    run_id=run_id,
                    event_type="PUBLICATION_RETRY_SCHEDULED" if retry else "PUBLICATION_STOPPED",
                    payload={"command_id": str(command_id), "code": code},
                )
            )
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    args = parser.parse_args()
    engine = make_engine(Settings.from_env().database_url)
    try:
        print(json.dumps(open_approved_draft(sessions(engine), args.run_id)))
    except (PublicationTargetError, GitHubAppError) as error:
        raise SystemExit(str(error)) from None
    except (HTTPException, OSError, SQLAlchemyError, ValueError, KeyError, TypeError):
        raise SystemExit(
            "Draft PR publication stopped. Check repository access and publication state."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
