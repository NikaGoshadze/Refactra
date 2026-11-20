"""Stage a verified plan's pinned source in a disposable local directory."""

import argparse
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from refactra.candidate_patch import (
    CandidatePatchError,
    apply_candidate_patch,
    read_candidate_patch,
)
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.github_app import GitHubAppError
from refactra.models import Run, RunCommand
from refactra.refactoring_plan import (
    APPLY_PLAN_APPROVAL,
    PlanContent,
    PlanRevisionConflict,
    ProposedChange,
    ValidationCheck,
    approval_command_key,
    load_approved_plan,
)
from refactra.repository_plan_worker import authorize_plan
from refactra.repository_selection import is_excluded_source_path
from refactra.repository_snapshot import SourceSnapshot, fetch_source_snapshot
from refactra.repository_validation import RepositoryValidationError, validate_candidate

PROTECTED_PYTHON_FILES = frozenset(
    {"conftest.py", "setup.py", "sitecustomize.py", "usercustomize.py"}
)


class WorkspacePreparationError(ValueError):
    """An operator-safe explanation for a workspace that could not be prepared."""


@dataclass(frozen=True)
class ApprovedWorkspace:
    root: Path
    base_sha: str
    revision: int
    sha256: str
    file_count: int
    changes: tuple[ProposedChange, ...]
    validation_checks: tuple[ValidationCheck, ...] = ()


def _workspace_path(value: str) -> PurePosixPath:
    parts = value.split("/")
    if (
        len(value) > 1024
        or re.search(r'[\x00-\x1f\x7f\\:<>"|?*]', value)
        or any(
            not part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
            for part in parts
        )
    ):
        raise WorkspacePreparationError("Source or plan contains an unsupported filesystem path.")
    return PurePosixPath(value)


def _workspace_files(snapshot: SourceSnapshot, content: PlanContent) -> dict[PurePosixPath, bytes]:
    if (snapshot.repository_id, snapshot.commit_sha) != (
        content.repository.repository_id,
        content.repository.base_sha,
    ):
        raise WorkspacePreparationError("The source snapshot does not match the approved commit.")

    paths = [_workspace_path(file.path) for file in snapshot.files]
    if len(paths) != len(set(paths)):
        raise WorkspacePreparationError("The source snapshot contains duplicate paths.")
    files = {
        path: file.content
        for path, file in zip(paths, snapshot.files, strict=True)
        if not is_excluded_source_path(path)
    }
    expected_paths = set(files)
    for change in content.changes:
        path = _workspace_path(change.path)
        if (
            is_excluded_source_path(path)
            or any(part.startswith(".") for part in path.parts)
            or path.suffix not in {".py", ".pyi"}
            or path.name.casefold() in PROTECTED_PYTHON_FILES
        ):
            raise WorkspacePreparationError(
                "Approved changes must target editable Python source or tests."
            )
        if (change.action == "CREATE") == (path in files):
            raise WorkspacePreparationError(
                "An approved file action does not match the pinned source."
            )
        expected_paths.add(path)

    names: dict[str, PurePosixPath] = {}
    for path in expected_paths:
        for entry in (path, *path.parents):
            if names.setdefault(entry.as_posix().casefold(), entry) != entry:
                raise WorkspacePreparationError(
                    "Workspace paths collide on case-insensitive filesystems."
                )
        if any(parent in expected_paths for parent in path.parents):
            raise WorkspacePreparationError("A workspace file would overlap a directory.")
    return files


@contextmanager
def prepare_approved_workspace(session_factory, run_id: UUID) -> Iterator[ApprovedWorkspace]:
    with session_factory.begin() as session:
        run = session.get(Run, run_id, with_for_update=True)
        if run is None:
            raise WorkspacePreparationError("Repository run could not be found.")
        try:
            plan = load_approved_plan(session, run)
        except PlanRevisionConflict:
            raise WorkspacePreparationError(
                "A matching approval is required before staging source."
            ) from None
        verified = session.scalar(
            select(RunCommand.id).where(
                RunCommand.run_id == run_id,
                RunCommand.kind == APPLY_PLAN_APPROVAL,
                RunCommand.dedupe_key == approval_command_key(run_id, plan.revision),
                RunCommand.status == "DONE",
            )
        )
        if verified is None:
            raise WorkspacePreparationError("The worker must verify the approved plan first.")
        content = PlanContent.model_validate_json(plan.document)

    token, transport = authorize_plan(run)
    files = _workspace_files(fetch_source_snapshot(run, token, transport=transport), content)
    with TemporaryDirectory(prefix="refactra-workspace-") as directory:
        root = Path(directory) / "source"
        root.mkdir()
        root.chmod(0o755)
        for path, data in files.items():
            destination = root.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(data)
        yield ApprovedWorkspace(
            root,
            run.base_sha,
            plan.revision,
            plan.sha256,
            len(files),
            tuple(content.changes),
            tuple(content.validation_checks),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    parser.add_argument(
        "--candidate", type=Path, help="Apply a structured patch and report its diff."
    )
    parser.add_argument(
        "--validate", action="store_true", help="Run the candidate's approved pytest checks."
    )
    args = parser.parse_args()
    if args.validate and args.candidate is None:
        parser.error("--validate requires --candidate")
    engine = make_engine(Settings.from_env().database_url)
    try:
        candidate = read_candidate_patch(args.candidate) if args.candidate is not None else None
        with prepare_approved_workspace(sessions(engine), args.run_id) as workspace:
            report = {
                "run_id": str(args.run_id),
                "base_sha": workspace.base_sha,
                "revision": workspace.revision,
                "sha256": workspace.sha256,
                "file_count": workspace.file_count,
                "approved_changes": [
                    {"path": change.path, "action": change.action} for change in workspace.changes
                ],
            }
            if candidate is not None:
                diff = apply_candidate_patch(workspace, candidate)
                report.update(diff=diff.decode("utf-8"), diff_sha256=sha256(diff).hexdigest())
                if args.validate:
                    checks = validate_candidate(workspace)
                    report.update(
                        validation=[asdict(check) for check in checks],
                        validation_passed=all(
                            check.status == "PASSED" for check in checks if check.required
                        ),
                    )
        print(json.dumps(report))
        if report.get("validation_passed") is False:
            raise SystemExit(1)
    except (WorkspacePreparationError, CandidatePatchError, RepositoryValidationError) as error:
        raise SystemExit(str(error)) from None
    except (GitHubAppError, HTTPException, KeyError, OSError, SQLAlchemyError, ValueError):
        raise SystemExit(
            "Workspace preparation stopped. Check repository access and local setup."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
