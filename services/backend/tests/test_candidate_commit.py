import json
import shutil
import subprocess
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

import refactra.publication_branch as publication
from refactra.analysis_context import save_analysis_context
from refactra.candidate_commit import CandidateCommitError, prepare_candidate_commit
from refactra.candidate_patch import CandidatePatch
from refactra.candidate_results import save_validated_candidate
from refactra.models import RunEvent, RunPublicationTarget
from refactra.publication_target import (
    PublicationTargetError,
    approve_publication_target,
    save_publication_target,
)
from refactra.refactoring_plan import approve_refactoring_plan, save_refactoring_plan
from refactra.repository_analysis_worker import enqueue_repository_analysis
from refactra.repository_validation import ValidationResult

from .test_analysis_context import prepare
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args])


@pytest.mark.skipif(shutil.which("git") is None, reason="Requires local Git.")
def test_prepares_the_saved_diff_as_one_pinned_commit(
    tmp_path, session_factory, proposal, monkeypatch
):
    source = tmp_path / "clone"
    source.mkdir()
    git(source, "init", "--template=", "--object-format=sha1", "--initial-branch=main")
    (source / "src").mkdir()
    (source / "src" / "cart.py").write_bytes(b"def cart(): return 1\n")
    (source / "old.py").write_bytes(b"# no longer needed")
    (source / "unchanged.md").write_bytes(b"Preserve this\n")
    git(source, "add", ".")
    git(source, "update-index", "--chmod=+x", "src/cart.py")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
        "-m",
        "Base",
    )
    base = git(source, "rev-parse", "HEAD").strip().decode()
    unrelated = b"# operator's uncommitted changes\n"
    (source / "src" / "cart.py").write_bytes(unrelated)
    run, _, context = prepare(session_factory, base_sha=base)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    proposal["changes"] += [
        {"path": "tests/test_cart.py", "action": "CREATE", "intent": "Add test module."},
        {"path": "old.py", "action": "DELETE", "intent": "Remove old module."},
    ]
    with session_factory.begin() as session:
        plan = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        approve_refactoring_plan(
            session,
            run.id,
            plan_id=plan.id,
            revision=1,
            sha256=plan.sha256,
            actor_id=run.requested_by,
        )
        candidate = CandidatePatch(
            base_sha=base,
            plan_sha256=plan.sha256,
            edits=[
                {"path": "src/cart.py", "action": "MODIFY", "content": "def cart(): return 2\n"},
                {"path": "tests/test_cart.py", "action": "CREATE", "content": ""},
                {"path": "old.py", "action": "DELETE", "content": None},
            ],
        )
        diff = (
            b"--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-# no longer needed\n"
            b"\\ No newline at end of file\n"
            b"--- a/src/cart.py\n+++ b/src/cart.py\n@@ -1 +1 @@\n"
            b"-def cart(): return 1\n+def cart(): return 2\n"
            b"--- /dev/null\n+++ b/tests/test_cart.py\n"
        )
        check = ValidationResult(
            "python.pytest",
            ["tests/test_cart.py"],
            True,
            "PASSED",
            0,
            50,
            "3 passed",
            None,
            "fixture",
        )
        saved = save_validated_candidate(session, run.id, candidate, diff, [check])

    output = tmp_path / "prepared"
    receipt = prepare_candidate_commit(session_factory, run.id, 1, source, output)
    assert json.loads((output / "receipt.json").read_bytes()) == receipt
    assert receipt["result_sha256"] == saved.sha256
    assert receipt["diff_sha256"] == sha256(diff).hexdigest()
    branch = f"refactra/{run.id}/r1"
    assert receipt["destination"] == {
        "base_sha": base,
        "base_branch": "main",
        "repository_id": 42,
        "head_branch": branch,
    }
    repository = output / "repository.git"
    commit = receipt["commit_sha"]
    assert git(repository, "rev-parse", branch).strip() == commit.encode()
    assert git(repository, "cat-file", "-p", commit).splitlines()[1] == f"parent {base}".encode()
    assert git(repository, "ls-tree", commit, "src/cart.py").startswith(b"100755 blob ")
    assert git(repository, "show", f"{commit}:src/cart.py") == b"def cart(): return 2\n"
    assert git(repository, "show", f"{commit}:tests/test_cart.py") == b""
    assert git(repository, "ls-tree", commit, "old.py") == b""
    assert git(repository, "diff-tree", "--name-only", "-r", base, commit).splitlines() == [
        b"old.py",
        b"src/cart.py",
        b"tests/test_cart.py",
    ]
    assert (source / "src" / "cart.py").read_bytes() == unrelated
    assert git(source, "rev-parse", "HEAD").strip() == base.encode()
    assert (
        prepare_candidate_commit(session_factory, run.id, 1, source, tmp_path / "retry") == receipt
    )

    registered = prepare_candidate_commit(
        session_factory, run.id, 1, source, output, save_target=True
    )
    assert (
        prepare_candidate_commit(session_factory, run.id, 1, source, output, save_target=True)
        == registered
    )
    with session_factory.begin() as session:
        target = session.get(RunPublicationTarget, UUID(receipt["result_id"]))
        assert json.loads(target.document) == {"schema_version": "1", **receipt}
        assert target.sha256 == registered["publication_target_sha256"]
        assert sha256(target.document).hexdigest() == target.sha256
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == "PUBLICATION_TARGET_SAVED")
        ).all()
        assert len(events) == 1 and events[0].payload["target_sha256"] == target.sha256
        with pytest.raises(PublicationTargetError, match="different publication target"):
            save_publication_target(session, {**receipt, "commit_sha": "f" * 40})

    check_branch_publication(session_factory, run, source, output, registered, monkeypatch)

    (output / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CandidateCommitError, match="Existing artifact"):
        prepare_candidate_commit(session_factory, run.id, 1, source, output, save_target=True)

    edit = candidate.edits[0].model_copy(update={"content": "def cart(): return 3\n"})
    mismatch = candidate.model_copy(update={"edits": [edit, *candidate.edits[1:]]})
    with session_factory.begin() as session:
        save_validated_candidate(session, run.id, mismatch, diff, [check])
    invalid_output = tmp_path / "invalid"
    with pytest.raises(CandidateCommitError, match="reviewed diff"):
        prepare_candidate_commit(session_factory, run.id, 2, source, invalid_output)
    assert not invalid_output.exists()


def check_branch_publication(session_factory, run, source, artifact, registered, monkeypatch):
    push = publication.push_approved_branch
    with pytest.raises(PublicationTargetError, match="owner must approve"):
        push(session_factory, run.id, artifact)
    with session_factory.begin() as session:
        approve_publication_target(
            session,
            run.id,
            result_id=UUID(registered["result_id"]),
            target_sha256=registered["publication_target_sha256"],
            actor_id=run.requested_by,
        )

    upstream = artifact.parent / "upstream.git"
    git(source, "clone", "--bare", str(source), str(upstream))
    current_base, commit = run.base_sha, registered["commit_sha"]
    requested_scope = []

    def mint(installation_id, repository_id):
        requested_scope.append((installation_id, repository_id))
        return SimpleNamespace(value="fixture-token")

    def resolve(_reader, *, base_branch, transport):
        sha = (
            current_base
            if base_branch == "main"
            else git(upstream, "rev-parse", base_branch).decode().strip()
        )
        return SimpleNamespace(full_name="fixture/repo", commit_sha=sha)

    real_git = publication._git

    def isolated_git(path, *args, https_auth=None):
        if args[0] == "push":
            assert https_auth == ("https://github.com/fixture/repo.git", "fixture-token")
            args = (*args[:-2], str(upstream), args[-1])
        return real_git(path, *args)

    app = SimpleNamespace(transport=None, mint_push_token=mint)
    monkeypatch.setattr(publication, "authorize_run_repository", lambda _run: (app, object()))
    monkeypatch.setattr(publication, "resolve_revision", resolve)
    monkeypatch.setattr(publication, "_git", isolated_git)
    current_base = "f" * 40
    with pytest.raises(PublicationTargetError, match="base branch moved"):
        push(session_factory, run.id, artifact)
    assert not requested_scope
    current_base = run.base_sha
    pushed = push(session_factory, run.id, artifact)
    assert push(session_factory, run.id, artifact) == pushed
    assert requested_scope == [(run.github_installation_id, run.github_repository_id)] * 2
    assert pushed["commit_sha"] == commit
    with session_factory() as session:
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == publication.PUBLICATION_BRANCH_PUSHED)
        ).all()
        assert [event.payload for event in events] == [pushed]
    ref = f"refs/heads/{pushed['head_branch']}"
    git(upstream, "update-ref", ref, run.base_sha)
    with pytest.raises(CandidateCommitError, match="Git could not complete push"):
        push(session_factory, run.id, artifact)
    assert git(upstream, "rev-parse", ref).decode().strip() == run.base_sha
