import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

import refactra.candidate_validation_worker as validation_worker
import refactra.publication_branch as publication
import refactra.publication_pr as draft
import refactra.repository_workspace as workspace
from refactra.analysis_context import save_analysis_context
from refactra.candidate_commit import CandidateCommitError, prepare_candidate_commit
from refactra.candidate_patch import CandidatePatch
from refactra.candidate_results import save_validated_candidate
from refactra.github_app import GitHubAppError
from refactra.models import RunCandidateResult, RunCommand, RunEvent, RunPublicationTarget
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.publication_target import (
    PublicationTargetError,
    approve_publication_target,
    save_publication_target,
)
from refactra.refactoring_plan import (
    APPLY_PLAN_APPROVAL,
    approve_refactoring_plan,
    save_refactoring_plan,
)
from refactra.repository_analysis_worker import (
    WORKFLOW,
    enqueue_repository_analysis,
    process_repository_plan_approval,
)
from refactra.repository_candidate import VALIDATE_CANDIDATE, generate_candidate
from refactra.repository_snapshot import SourceFile, SourceSnapshot
from refactra.repository_validation import ValidationResult
from refactra.worker import claim_command

from .test_analysis_context import prepare
from .test_openai_analysis import response
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args])


@pytest.mark.skipif(shutil.which("git") is None, reason="Requires local Git.")
@pytest.mark.parametrize("passes", [True, False])
def test_worker_validates_generated_patch_and_recovers_commit_preparation(
    pinned_source, tmp_path, session_factory, proposal, monkeypatch, passes
):
    source, base = pinned_source
    run, _, context = prepare(session_factory, base_sha=base)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    proposal["changes"].append(
        {"path": "tests/test_cart.py", "action": "CREATE", "intent": "Cover cart calculation."}
    )
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
    approval = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=APPLY_PLAN_APPROVAL)
    assert process_repository_plan_approval(session_factory, approval, "worker")
    snapshot = SourceSnapshot(42, base, (SourceFile("src/cart.py", b"def cart(): return 1\n"),))
    monkeypatch.setattr(workspace, "authorize_plan", lambda _: (None, None))
    monkeypatch.setattr(workspace, "fetch_source_snapshot", lambda *_, **__: snapshot)
    patch = CandidatePatch(
        base_sha=base,
        plan_sha256=plan.sha256,
        edits=[
            {"path": "src/cart.py", "action": "MODIFY", "content": "def cart(): return 2\n"},
            {
                "path": "tests/test_cart.py",
                "action": "CREATE",
                "content": "def test_cart(): pass\n",
            },
        ],
    )
    payload = response()
    payload["output"][0]["content"][0]["text"] = patch.model_dump_json()

    def provider_response(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        return httpx.Response(200, json=payload)

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(provider_response))
    assert generate_candidate(session_factory, run.id, provider) == patch
    calls = []

    def validate(staged):
        assert (staged.root / "src/cart.py").read_text().strip() == "def cart(): return 2"
        calls.append(staged.base_sha)
        status, exit_code = ("PASSED", 0) if passes else ("FAILED", 1)
        return [
            ValidationResult(
                "python.pytest",
                ["tests/test_cart.py"],
                True,
                status,
                exit_code,
                10,
                "",
                None,
                "fixture",
            )
        ]

    monkeypatch.setattr(validation_worker, "validate_candidate", validate)
    job = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=VALIDATE_CANDIDATE)
    assert job is not None
    process = validation_worker.process_candidate_validation
    options = {"source": source, "artifacts": tmp_path}
    worker = "worker"
    if passes:

        def interrupt(*_, **__):
            raise KeyboardInterrupt

        with monkeypatch.context() as interrupted:
            interrupted.setattr(validation_worker, "prepare_candidate_commit", interrupt)
            with pytest.raises(KeyboardInterrupt):
                process(session_factory, job, worker, **options)
        with session_factory.begin() as session:
            session.get(RunCommand, job).lease_until = datetime.now(UTC) - timedelta(minutes=1)
        assert (
            claim_command(session_factory, "restart", workflow=WORKFLOW, kind=VALIDATE_CANDIDATE)
            == job
        )
        assert not process(session_factory, job, worker, **options)
        worker = "restart"
    assert process(session_factory, job, worker, **options)
    assert calls == [base]
    with session_factory() as session:
        result = session.scalar(
            select(RunCandidateResult).where(RunCandidateResult.run_id == run.id)
        )
        assert session.get(RunCommand, job).status == ("DONE" if passes else "FAILED")
        if passes:
            target = session.get(RunPublicationTarget, result.id)
            receipt = json.loads(target.document)
            artifact = tmp_path / f"{run.id}-r1" / "repository.git"
            parent = git(artifact, "show", "-s", "--format=%P", receipt["commit_sha"])
            assert parent.strip().decode() == base
        else:
            assert result is None
            assert session.scalar(select(RunPublicationTarget)) is None


@pytest.fixture
def pinned_source(tmp_path):
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
    return source, base


@pytest.mark.skipif(shutil.which("git") is None, reason="Requires local Git.")
@pytest.mark.parametrize("lost_response", [False, True])
def test_prepares_the_saved_diff_as_one_pinned_commit(
    tmp_path, session_factory, proposal, monkeypatch, lost_response, pinned_source
):
    source, base = pinned_source
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

    check_branch_publication(
        session_factory, run, source, output, registered, monkeypatch, lost_response
    )

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


def check_branch_publication(
    session_factory, run, source, artifact, registered, monkeypatch, lost_response
):
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
    with pytest.raises(PublicationTargetError, match="Push the approved branch"):
        draft.open_approved_draft(session_factory, run.id)

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
    monkeypatch.setattr(draft, "resolve_revision", resolve)
    check_draft_publication(session_factory, run, pushed, monkeypatch, lost_response)
    ref = f"refs/heads/{pushed['head_branch']}"
    git(upstream, "update-ref", ref, run.base_sha)
    with pytest.raises(CandidateCommitError, match="Git could not complete push"):
        push(session_factory, run.id, artifact)
    assert git(upstream, "rev-parse", ref).decode().strip() == run.base_sha


def check_draft_publication(session_factory, run, pushed, monkeypatch, lost_response):
    head = pushed["head_branch"]
    commit = pushed["commit_sha"]
    pr = {
        "number": 18,
        "state": "open",
        "draft": True,
        "base": {"repo": {"id": 42}, "ref": "main", "sha": run.base_sha},
        "head": {"repo": {"id": 42}, "ref": head, "sha": commit},
    }
    methods = []
    wrong_head = True

    def handler(request):
        assert request.url.host == "api.github.com"
        assert request.url.path == "/repos/fixture/repo/pulls"
        assert request.headers["authorization"] == "Bearer fixture-pr-token"
        if request.method == "GET":
            assert request.url.params["head"] == f"fixture:{head}"
            assert request.url.params["state"] == "all"
            if wrong_head:
                entries = [{**pr, "head": {**pr["head"], "sha": run.base_sha}}]
            else:
                entries = [pr] if "POST" in methods else []
            methods.append("GET")
            return httpx.Response(200, json=entries)
        assert request.method == "POST"
        body = json.loads(request.content)
        assert (body["head"], body["base"], body["draft"]) == (head, "main", True)
        methods.append("POST")
        if lost_response:
            raise httpx.ReadTimeout("Fixture lost the creation response", request=request)
        return httpx.Response(201, json=pr)

    def mint(installation_id, repository_id):
        assert (installation_id, repository_id) == (run.github_installation_id, 42)
        return SimpleNamespace(value="fixture-pr-token")

    app = SimpleNamespace(transport=httpx.MockTransport(handler), mint_pull_request_token=mint)
    monkeypatch.setattr(draft, "authorize_run_repository", lambda _run: (app, object()))
    with pytest.raises(PublicationTargetError, match="does not match the approved draft"):
        draft.open_approved_draft(session_factory, run.id)
    assert methods == ["GET"]
    wrong_head = False
    methods.clear()
    if lost_response:
        with pytest.raises(GitHubAppError, match="Retry to check"):
            draft.open_approved_draft(session_factory, run.id)
    published = draft.open_approved_draft(session_factory, run.id)
    assert published["pull_request_url"] == "https://github.com/fixture/repo/pull/18"
    assert published["target_sha256"] == pushed["target_sha256"]
    assert draft.open_approved_draft(session_factory, run.id) == published
    assert methods == (["GET", "POST", "GET"] if lost_response else ["GET", "POST"])
    with session_factory() as session:
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == draft.PUBLICATION_PR_OPENED)
        ).all()
        assert [event.payload for event in events] == [published]
