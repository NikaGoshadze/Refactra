"""Prepare a local Git commit from a saved, validated candidate revision."""

import argparse
import json
import os
import subprocess
from functools import partial
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from refactra.candidate_patch import MAX_EDIT_FILE_BYTES, CandidatePatch, apply_candidate_patch
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.models import Run, RunCandidateResult
from refactra.refactoring_plan import PlanContent, load_approved_plan
from refactra.repository_snapshot import SourceFile, SourceSnapshot
from refactra.repository_workspace import (
    ApprovedWorkspace,
    WorkspacePreparationError,
    _workspace_files,
)


class CandidateCommitError(ValueError):
    """An operator-safe reason the local commit could not be prepared."""


def _git(repository: Path, *args: str, data: bytes | None = None, date: str = "") -> bytes:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "REFACTRA_"))
    }
    env.update(
        GIT_ALLOW_PROTOCOL="file",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_LITERAL_PATHSPECS="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    if date:
        env.update(
            GIT_AUTHOR_NAME="Refactra",
            GIT_AUTHOR_EMAIL="refactra@localhost",
            GIT_COMMITTER_NAME="Refactra",
            GIT_COMMITTER_EMAIL="refactra@localhost",
            GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_DATE=date,
        )
    try:
        return subprocess.run(
            ["git", f"--git-dir={repository}", *args],
            input=data if data is not None else b"",
            capture_output=True,
            check=True,
            timeout=60,
            env=env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise CandidateCommitError(
            f"Git could not complete {args[0]} for the saved candidate."
        ) from None


def prepare_candidate_commit(
    session_factory, run_id: UUID, revision: int, source: Path, output: Path
) -> dict:
    head_branch = f"refactra/{run_id}/r{revision}"
    with session_factory() as session:
        run = session.get(Run, run_id)
        result = session.scalar(
            select(RunCandidateResult).where(
                RunCandidateResult.run_id == run_id, RunCandidateResult.revision == revision
            )
        )
        if run is None or result is None:
            raise CandidateCommitError("Saved candidate revision not found.")
        plan = load_approved_plan(session, run)
        content = PlanContent.model_validate_json(plan.document)
        saved = json.loads(result.document)
        plan_ref = {"id": str(plan.id), "revision": plan.revision, "sha256": plan.sha256}
        if (
            saved["approved_plan"] != plan_ref
            or saved["repository"] != content.repository.model_dump()
        ):
            raise CandidateCommitError(
                "The saved result does not match the approved plan and base."
            )
        candidate = CandidatePatch.model_validate(saved["candidate"])
        if (candidate.base_sha, candidate.plan_sha256) != (
            content.repository.base_sha,
            plan.sha256,
        ):
            raise CandidateCommitError("The candidate does not match the approved commit and plan.")
        diff_sha = saved["diff_sha256"]
        receipt = {
            "run_id": str(run_id),
            "result_id": str(result.id),
            "result_revision": result.revision,
            "result_sha256": result.sha256,
            "diff_sha256": diff_sha,
            "approved_plan": plan_ref,
            "destination": {
                **content.repository.model_dump(),
                "head_branch": head_branch,
            },
        }
        date = f"@{int(result.created_at.timestamp())} +0000"

    source = source.resolve(strict=True)
    if (
        not source.is_dir()
        or not output.parent.is_dir()
        or output.exists()
        or output.is_symlink()
        or output.resolve().is_relative_to(source)
    ):
        raise CandidateCommitError("Provide a local Git repository and a new output directory.")
    base = content.repository.base_sha
    with TemporaryDirectory(prefix="refactra-commit-", dir=output.parent) as temporary:
        staging = Path(temporary) / "artifact"
        staging.mkdir()
        git = partial(_git, staging / "repository.git")
        git("init", "--bare", "--template=", "--object-format=sha1")
        git(
            "fetch",
            "--depth=1",
            "--no-write-fetch-head",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            str(source),
            base,
        )
        if git("cat-file", "-t", base).strip() != b"commit":
            raise CandidateCommitError("The pinned revision is not a Git commit.")

        workspace = ApprovedWorkspace(
            Path(temporary) / "source",
            base,
            plan_ref["revision"],
            plan_ref["sha256"],
            0,
            tuple(content.changes),
        )
        modes = {}
        source_files = []
        for edit in candidate.edits:
            entry = git("ls-tree", "-z", base, "--", edit.path)
            if not entry:
                modes[edit.path] = b"100644"
                continue
            mode, kind, oid = entry.split(b"\t", 1)[0].split()
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise CandidateCommitError("The pinned Git tree has a non-regular candidate path.")
            if int(git("cat-file", "-s", oid.decode())) > MAX_EDIT_FILE_BYTES:
                raise CandidateCommitError("A candidate path exceeds the source file size limit.")
            modes[edit.path] = mode
            source_files.append(SourceFile(edit.path, git("cat-file", "blob", oid.decode())))
        snapshot = SourceSnapshot(content.repository.repository_id, base, tuple(source_files))
        workspace.root.mkdir()
        for path, data in _workspace_files(snapshot, content).items():
            file = workspace.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)

        diff = apply_candidate_patch(workspace, candidate)
        if diff != saved["diff"].encode() or sha256(diff).hexdigest() != diff_sha:
            raise CandidateCommitError(
                "The Git base and candidate do not reproduce the reviewed diff."
            )

        git("read-tree", base)
        records = []
        for edit in candidate.edits:
            if edit.action == "DELETE":
                mode, oid = b"0", b"0" * 40
            else:
                mode = modes[edit.path]
                oid = git(
                    "hash-object", "-w", "--stdin", data=(workspace.root / edit.path).read_bytes()
                )
                oid = oid.strip()
            records.append(mode + b" " + oid + b"\t" + edit.path.encode() + b"\0")
        git("update-index", "-z", "--index-info", data=b"".join(records))
        tree = git("write-tree").strip().decode()
        paths = git("diff-tree", "-r", "--no-renames", "--name-only", "-z", base, tree)
        if set(paths.split(b"\0")[:-1]) != {edit.path.encode() for edit in candidate.edits}:
            raise CandidateCommitError(
                "Staging changed files outside the validated candidate scope."
            )
        commit = git(
            "commit-tree",
            tree,
            "-p",
            base,
            "-m",
            f"Refactra candidate {run_id} r{revision}",
            date=date,
        )
        receipt["commit_sha"] = commit.strip().decode()
        git("update-ref", f"refs/heads/{head_branch}", receipt["commit_sha"])
        (staging / "receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(output)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    parser.add_argument("revision", type=int)
    parser.add_argument("--source", type=Path, required=True, help="Local Git clone of the target.")
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory for the artifact."
    )
    args = parser.parse_args()
    engine = make_engine(Settings.from_env().database_url)
    try:
        receipt = prepare_candidate_commit(
            sessions(engine), args.run_id, args.revision, args.source, args.output
        )
        print(json.dumps(receipt))
    except (CandidateCommitError, WorkspacePreparationError) as error:
        raise SystemExit(str(error)) from None
    except (OSError, SQLAlchemyError, KeyError, TypeError, ValueError):
        raise SystemExit(
            "Commit preparation stopped. Check the saved revision and local setup."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
