import json
import os
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
import refactra.repository_plan_worker as planning
import refactra.repository_validation as validation
import refactra.repository_workspace as workspace
from refactra.analysis_context import prepare_analysis_context, save_analysis_context
from refactra.auth_sessions import GitHubIdentity
from refactra.candidate_commit import CandidateCommitError, prepare_candidate_commit
from refactra.candidate_patch import CandidatePatch, CandidatePatchError, apply_candidate_patch
from refactra.candidate_results import save_validated_candidate
from refactra.models import (
    RunCandidateResult,
    RunCommand,
    RunEvent,
    RunGeneratedCandidate,
    RunPublicationTarget,
)
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.publication_target import (
    PUBLISH_CANDIDATE,
    PublicationTargetError,
    approve_publication_target,
    save_publication_target,
)
from refactra.refactoring_plan import (
    APPLY_PLAN_APPROVAL,
    ASSET_CHECK,
    PATCH_CHECK,
    TYPESCRIPT_CHECK,
    approve_refactoring_plan,
    save_refactoring_plan,
)
from refactra.repository_analysis_worker import (
    GENERATE_CANDIDATE,
    WORKFLOW,
    enqueue_repository_analysis,
    process_repository_plan_approval,
)
from refactra.repository_candidate import (
    VALIDATE_CANDIDATE,
    VALIDATION_REPAIR_EVENT,
    generate_candidate,
    process_repository_candidate,
)
from refactra.repository_revision import RepositoryRevision
from refactra.repository_runs import create_repository_run
from refactra.repository_snapshot import SourceFile, SourceSnapshot
from refactra.repository_validation import ValidationResult
from refactra.worker import claim_command

from .test_analysis_context import prepare
from .test_candidate_results import passing_patch
from .test_openai_analysis import response
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args])


@pytest.mark.skipif(shutil.which("git") is None, reason="Requires local Git.")
@pytest.mark.parametrize("status", ["PASSED", "FAILED", "UNAVAILABLE"])
def test_worker_validates_generated_patch_and_recovers_commit_preparation(
    pinned_source, tmp_path, session_factory, proposal, monkeypatch, status
):
    can_review = status != "FAILED"
    source, base = pinned_source
    run, _, context = prepare(session_factory, base_sha=base)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    proposal["changes"].append(
        {"path": "tests/test_cart.py", "action": "CREATE", "intent": "Cover cart calculation."}
    )
    proposal["changes"].append(
        {"path": "unchanged.md", "action": "MODIFY", "intent": "Update instructions if needed."}
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
    snapshot = SourceSnapshot(
        42,
        base,
        (
            SourceFile("src/cart.py", b"def cart(): return 1\n"),
            SourceFile("unchanged.md", b"Preserve this\n"),
        ),
    )
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
            {"path": "unchanged.md", "action": "MODIFY", "content": "Preserve this\n"},
        ],
    )
    payload = response()
    payload["output"][0]["content"][0]["text"] = json.dumps(
        {"edits": {edit.path: edit.content for edit in patch.edits}}
    )

    def provider_response(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        return httpx.Response(200, json=payload)

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(provider_response))
    assert generate_candidate(session_factory, run.id, provider) == patch
    calls = []

    def docker(*_):
        if status == "UNAVAILABLE":
            raise validation.RepositoryValidationError("Offline image missing.")
        return "fixture"

    def validate(staged, check, targets, image_id, owner):
        assert owner.command == str(job) and owner.attempt >= 1
        assert (staged.root / "src/cart.py").read_text().strip() == "def cart(): return 2"
        calls.append(staged.base_sha)
        return ValidationResult(
            check.check_id,
            check.paths,
            check.required,
            status,
            0 if can_review else 1,
            10,
            "",
            None,
            image_id,
        )

    monkeypatch.setattr(validation, "_docker", docker)
    monkeypatch.setattr(validation, "_validate_check", validate)
    job = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=VALIDATE_CANDIDATE)
    assert job is not None
    process = validation_worker.process_candidate_validation
    options = {"source": source, "artifacts": tmp_path}
    worker = "worker"
    if can_review:

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
    assert calls == ([] if status == "UNAVAILABLE" else [base])
    with session_factory() as session:
        result = session.scalar(
            select(RunCandidateResult).where(RunCandidateResult.run_id == run.id)
        )
        # A first failed check queues one repair attempt, so the validation job completes too.
        assert session.get(RunCommand, job).status == "DONE"
        if can_review:
            checks = json.loads(result.document)["validation"]
            assert [(check["check_id"], check["status"]) for check in checks] == [
                ("python.pytest", status),
                ("repository.patch", "PASSED"),
            ]
            if status == "UNAVAILABLE":
                assert "offline Docker" in checks[0]["reason"]
            target = session.get(RunPublicationTarget, result.id)
            receipt = json.loads(target.document)
            artifact = tmp_path / f"{run.id}-r1" / "repository.git"
            parent = git(artifact, "show", "-s", "--format=%P", receipt["commit_sha"])
            assert parent.strip().decode() == base
            assert "unchanged.md" not in json.loads(result.document)["diff"]
            assert not git(artifact, "diff", base, receipt["commit_sha"], "--", "unchanged.md")
        else:
            assert result is None
            assert session.scalar(select(RunPublicationTarget)) is None
            repair = session.scalar(
                select(RunEvent).where(RunEvent.event_type == VALIDATION_REPAIR_EVENT)
            )
            assert repair.payload["validation_command_id"] == str(job)
            repair_job = session.get(RunCommand, UUID(repair.payload["command_id"]))
            assert (repair_job.kind, repair_job.status) == (GENERATE_CANDIDATE, "PENDING")


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


@pytest.mark.parametrize(
    ("path", "template_path", "extra_path"),
    [
        (".env", None, None),
        (".env", ".env.example", None),
        (".env", ".env-example", None),
        ("apps/api/.env.production", "apps/api/.env.sample", ".prettierrc"),
        ("logo.png", None, None),
    ],
)
def test_approved_file_operations_prepare_exact_commit(
    pinned_source, tmp_path, session_factory, proposal, monkeypatch, path, template_path, extra_path
):
    source, _ = pinned_source
    replace_asset = path == "logo.png"
    data = b"\x89PNG\r\n\x1a\noriginal" if replace_asset else b"KEY=synthetic-secret-marker"
    replacement = b"\x89PNG\r\n\x1a\nreplacement"
    (source / path).parent.mkdir(parents=True, exist_ok=True)
    (source / path).write_bytes(data)
    git(source, "add", "--force", "--", path)
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Add file operation fixture",
    )
    base = git(source, "rev-parse", "HEAD").decode().strip()
    snapshot = SourceSnapshot(42, base, (SourceFile(path, data),))
    with session_factory.begin() as session:
        run = create_repository_run(
            session,
            identity=GitHubIdentity(123, "fixture"),
            installation_id=77,
            revision=RepositoryRevision(42, "fixture/repo", "main", "main", base),
            task_text=f"{'Replace' if replace_asset else 'Delete'} {path}",
        )
        context = prepare_analysis_context(run, snapshot)
        assert data not in context.document
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    action = "MODIFY" if replace_asset else "DELETE"
    check_id = ASSET_CHECK if replace_asset else PATCH_CHECK
    proposal["changes"] = [{"path": path, "action": action, "intent": "Exact file operation."}]
    proposal["validation_checks"] = [
        dict(proposal["validation_checks"][0], check_id=check_id, paths=[path])
    ]
    with session_factory.begin() as session:
        plan = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        if template_path:
            for new_path in filter(None, (template_path, extra_path)):
                proposal["changes"].append(
                    {"path": new_path, "action": "CREATE", "intent": "Add the requested template."}
                )
                proposal["validation_checks"][0]["paths"].append(new_path)
            plan = save_refactoring_plan(session, run.id, proposal, expected_revision=1)
        approve_refactoring_plan(
            session,
            run.id,
            plan_id=plan.id,
            revision=plan.revision,
            sha256=plan.sha256,
            actor_id=run.requested_by,
        )
    if replace_asset:
        (tmp_path / f"{run.id}.asset").write_bytes(replacement)
    approval = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=APPLY_PLAN_APPROVAL)
    assert process_repository_plan_approval(session_factory, approval, "worker")
    monkeypatch.setattr(workspace, "authorize_plan", lambda _: (None, None))
    monkeypatch.setattr(workspace, "fetch_source_snapshot", lambda *_, **__: snapshot)
    generation = claim_command(
        session_factory, "worker", workflow=WORKFLOW, kind=GENERATE_CANDIDATE
    )
    if template_path:
        assert generation is not None
        template = {"path": template_path, "action": "CREATE", "content": "KEY=\n"}
        edits = [{"path": path, "action": "DELETE", "content": None}, template]
        if extra_path:
            edits.append({"path": extra_path, "action": "CREATE", "content": "{}\n"})
        patch = CandidatePatch(
            base_sha=base,
            plan_sha256=plan.sha256,
            edits=edits,
        )
        with workspace.prepare_approved_workspace(session_factory, run.id) as staged:
            bad_patch = CandidatePatch(
                base_sha=base,
                plan_sha256=plan.sha256,
                edits=[patch.edits[0], {**template, "content": data.decode()}, *patch.edits[2:]],
            )
            with pytest.raises(CandidatePatchError, match="empty values"):
                apply_candidate_patch(staged, bad_patch)
            assert (staged.root / path).read_bytes() == b""
            assert not (staged.root / template_path).exists()

        def provider_response(request):
            assert data not in request.content
            assert json.loads(json.loads(request.content)["input"])["source"] == {}
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(
                    200, json={"object": "response.input_tokens", "input_tokens": 100}
                )
            payload = response()
            payload["output"][0]["content"][0]["text"] = json.dumps(
                {"edits": {edit.path: edit.content for edit in patch.edits}}
            )
            return httpx.Response(200, json=payload)

        provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(provider_response))
        assert process_repository_candidate(session_factory, generation, "worker", provider)
    else:
        assert generation is None
    job = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=VALIDATE_CANDIDATE)
    assert validation_worker.process_candidate_validation(
        session_factory, job, "worker", source=source, artifacts=tmp_path
    )
    with session_factory() as session:
        assert session.get(RunCommand, job).status == "DONE"
        assert (session.get(RunGeneratedCandidate, run.id) is not None) == bool(template_path)
        result = session.scalar(
            select(RunCandidateResult).where(RunCandidateResult.run_id == run.id)
        )
        assert data not in result.document
        report = json.loads(result.document)
        assert path in report["diff"] and report["validation"][0]["status"] == "PASSED"

    artifact = tmp_path / f"{run.id}-r1"
    receipt = json.loads((artifact / "receipt.json").read_bytes())
    repository, commit = artifact / "repository.git", receipt["commit_sha"]
    expected = {value.encode() for value in (path, template_path, extra_path) if value}
    assert (
        set(git(repository, "diff-tree", "-r", "--name-only", base, commit).splitlines())
        == expected
    )
    if replace_asset:
        assert git(repository, "show", f"{commit}:{path}") == replacement
        assert sha256(replacement).hexdigest() in report["diff"]
    else:
        assert git(repository, "ls-tree", commit, path) == b""
        assert "Rotate affected credentials" in report["validation"][0]["output"]
        if template_path:
            assert git(repository, "show", f"{commit}:{template_path}") == b"KEY=\n"
        if extra_path:
            assert git(repository, "show", f"{commit}:{extra_path}") == b"{}\n"


def test_template_update_keeps_original_values_private_and_preserves_bytes(
    pinned_source, tmp_path, session_factory, proposal, monkeypatch
):
    source, _ = pinned_source
    path = ".env-example"
    original = b"# private fixture comment\r\nGOOGLE_API_KEY=synthetic-secret-marker"
    (source / path).write_bytes(original)
    git(source, "-c", "core.autocrlf=false", "add", "--force", "--", path)
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Add example config fixture",
    )
    base = git(source, "rev-parse", "HEAD").decode().strip()
    snapshot = SourceSnapshot(42, base, (SourceFile(path, original),))
    with session_factory.begin() as session:
        run = create_repository_run(
            session,
            identity=GitHubIdentity(123, "fixture"),
            installation_id=77,
            revision=RepositoryRevision(42, "fixture/repo", "main", "main", base),
            task_text="Add BOOK_SEARCH_LIMIT=3 to the existing environment example.",
        )
        context = prepare_analysis_context(run, snapshot)
        assert b"synthetic-secret-marker" not in context.document
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    proposal["changes"] = [{"path": path, "action": "MODIFY", "intent": "Add BOOK_SEARCH_LIMIT=3."}]
    proposal["validation_checks"] = [
        dict(proposal["validation_checks"][0], check_id=PATCH_CHECK, paths=[path])
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
    approval = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=APPLY_PLAN_APPROVAL)
    assert process_repository_plan_approval(session_factory, approval, "worker")
    monkeypatch.setattr(workspace, "authorize_plan", lambda _: (None, None))
    monkeypatch.setattr(workspace, "fetch_source_snapshot", lambda *_, **__: snapshot)

    def provider_response(request):
        assert b"synthetic-secret-marker" not in request.content
        assert b"private fixture comment" not in request.content
        body = json.loads(request.content)
        assert json.loads(body["input"])["source"] == {path: "GOOGLE_API_KEY=\n"}
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        schema = body["text"]["format"]["schema"]["properties"]["edits"]["properties"][path]
        assert schema["type"] == "object"
        assert schema["required"] == ["new_assignments"]
        assert schema["properties"]["new_assignments"]["items"]["required"] == ["name", "value"]
        payload = response()
        payload["output"][0]["content"][0]["text"] = json.dumps(
            {"edits": {path: {"new_assignments": [{"name": "BOOK_SEARCH_LIMIT", "value": "3"}]}}}
        )
        return httpx.Response(200, json=payload)

    with workspace.prepare_approved_workspace(session_factory, run.id) as staged:
        for additions, explanation in [
            ("GOOGLE_API_KEY=\n", "preserve existing assignments"),
            ("OTHER_API_KEY=987654\n", "non-credential settings"),
        ]:
            invalid = CandidatePatch(
                base_sha=base,
                plan_sha256=plan.sha256,
                edits=[{"path": path, "action": "MODIFY", "content": additions}],
            )
            with pytest.raises(CandidatePatchError, match=explanation):
                apply_candidate_patch(staged, invalid)
        assert (staged.root / path).read_bytes() == b"GOOGLE_API_KEY=\n"
    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(provider_response))
    generate_candidate(session_factory, run.id, provider)
    job = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=VALIDATE_CANDIDATE)
    assert validation_worker.process_candidate_validation(
        session_factory, job, "worker", source=source, artifacts=tmp_path
    )
    with session_factory() as session:
        assert session.get(RunCommand, job).status == "DONE"
        saved = session.scalar(
            select(RunCandidateResult).where(RunCandidateResult.run_id == run.id)
        )
        assert b"synthetic-secret-marker" not in saved.document
        assert "+BOOK_SEARCH_LIMIT=3" in json.loads(saved.document)["diff"]
    artifact = tmp_path / f"{run.id}-r1"
    receipt = json.loads((artifact / "receipt.json").read_bytes())
    assert git(artifact / "repository.git", "show", f"{receipt['commit_sha']}:{path}") == (
        original + b"\r\nBOOK_SEARCH_LIMIT=3\r\n"
    )


@pytest.mark.skipif(
    os.getenv("REFACTRA_TEST_TYPESCRIPT_DOCKER") != "1",
    reason="Requires a rebuilt local backend image and Docker.",
)
@pytest.mark.parametrize("passes", [True, False])
def test_typescript_feature_discovery_through_review_and_draft(
    pinned_source, tmp_path, session_factory, proposal, monkeypatch, passes
):
    source, _ = pinned_source
    path, config = "web/cart.tsx", "web/tsconfig.json"
    files = {
        path: b"export const Cart = () => <h1>Shopping cart</h1>;\n",
        config: (
            b'{"compilerOptions":{"jsx":"react-jsx","strict":true,"target":"ES2022",'
            b'"module":"NodeNext"},"include":["cart.tsx"]}'
        ),
    }
    (source / "web").mkdir()
    for name, data in files.items():
        (source / name).write_bytes(data)
    git(source, "add", "web")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Add frontend fixture",
    )
    base = git(source, "rev-parse", "HEAD").decode().strip()
    snapshot = SourceSnapshot(42, base, tuple(SourceFile(*item) for item in files.items()))
    with session_factory.begin() as session:
        run = create_repository_run(
            session,
            identity=GitHubIdentity(123, "fixture"),
            installation_id=77,
            revision=RepositoryRevision(42, "fixture/repo", "main", "main", base),
            task_text="Update the shopping cart heading to show the order summary.",
        )
        context = prepare_analysis_context(run, snapshot)
        assert path in {item["path"] for item in json.loads(context.document)["selection"]["files"]}
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    check = dict(proposal["validation_checks"][0], check_id=TYPESCRIPT_CHECK, paths=[config])
    proposal["changes"] = [{"path": path, "action": "MODIFY", "intent": "Update the heading."}]
    proposal["validation_checks"] = [check, dict(check, check_id="node.vitest", paths=[])]
    response_text = json.dumps(proposal)

    def model(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        payload = response()
        payload["output"][0]["content"][0]["text"] = response_text
        return httpx.Response(200, json=payload)

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    monkeypatch.setattr(planning, "authorize_plan", lambda _: (None, None))
    monkeypatch.setattr(workspace, "authorize_plan", lambda _: (None, None))
    monkeypatch.setattr(workspace, "fetch_source_snapshot", lambda *_, **__: snapshot)
    plan = planning.generate_initial_plan(session_factory, run.id, provider)
    with session_factory.begin() as session:
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
    changed = "export const Cart = () => <h1>Order summary</h1>;\n"
    if not passes:
        changed += "export const count: number = 'three';\n"
    response_text = json.dumps({"edits": {path: changed}})
    generate_candidate(session_factory, run.id, provider)
    job = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=VALIDATE_CANDIDATE)
    assert validation_worker.process_candidate_validation(
        session_factory, job, "worker", source=source, artifacts=tmp_path
    )
    with session_factory() as session:
        result = session.scalar(
            select(RunCandidateResult).where(RunCandidateResult.run_id == run.id)
        )
        if not passes:
            assert result is None and session.get(RunCommand, job).status == "FAILED"
            return
        assert result is not None
        report = json.loads(result.document)
        assert [(item["check_id"], item["status"]) for item in report["validation"]] == [
            (TYPESCRIPT_CHECK, "PASSED"),
            ("node.vitest", "UNAVAILABLE"),
            (PATCH_CHECK, "PASSED"),
        ]
        assert "Order summary" in report["diff"]
    artifact = tmp_path / f"{run.id}-r1"
    receipt = prepare_candidate_commit(
        session_factory, run.id, 1, source, artifact, save_target=True
    )
    check_branch_publication(
        session_factory,
        run,
        source,
        artifact,
        receipt,
        monkeypatch,
        False,
        expected_warning="`node.vitest` · UNAVAILABLE",
    )


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
        patch_check = passing_patch(*(item.path for item in candidate.edits))
        saved = save_validated_candidate(session, run.id, candidate, diff, [check, patch_check])

    output = tmp_path / f"{run.id}-r1"
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
        save_validated_candidate(session, run.id, mismatch, diff, [check, patch_check])
    invalid_output = tmp_path / "invalid"
    with pytest.raises(CandidateCommitError, match="reviewed diff"):
        prepare_candidate_commit(session_factory, run.id, 2, source, invalid_output)
    assert not invalid_output.exists()


def check_branch_publication(
    session_factory,
    run,
    source,
    artifact,
    registered,
    monkeypatch,
    lost_response,
    *,
    expected_warning=None,
):
    push = publication.push_approved_branch
    with pytest.raises(PublicationTargetError, match="owner must approve"):
        push(session_factory, run.id, artifact)
    assert (
        claim_command(session_factory, "worker", workflow=WORKFLOW, kind=PUBLISH_CANDIDATE) is None
    )
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
    check_draft_publication(
        session_factory,
        run,
        pushed,
        monkeypatch,
        lost_response,
        artifacts=artifact.parent,
        expected_warning=expected_warning,
    )
    assert len(requested_scope) == (2 if lost_response else 3)
    ref = f"refs/heads/{pushed['head_branch']}"
    git(upstream, "update-ref", ref, run.base_sha)
    with pytest.raises(CandidateCommitError, match="Git could not complete push"):
        push(session_factory, run.id, artifact)
    assert git(upstream, "rev-parse", ref).decode().strip() == run.base_sha


def check_draft_publication(
    session_factory, run, pushed, monkeypatch, lost_response, *, artifacts, expected_warning
):
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
        if expected_warning:
            assert "Validation warnings" in body["body"] and expected_warning in body["body"]
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
    if not lost_response:
        # Simulate a push that reached the remote before its receipt was saved.
        with session_factory.begin() as session:
            branch = session.scalar(
                select(RunEvent).where(RunEvent.event_type == publication.PUBLICATION_BRANCH_PUSHED)
            )
            session.delete(branch)
    job = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=PUBLISH_CANDIDATE)
    assert job is not None
    process = draft.process_approved_publication
    assert process(session_factory, job, "worker", artifacts=artifacts)
    if lost_response:
        with session_factory.begin() as session:
            command = session.get(RunCommand, job)
            assert command.status == "PENDING" and command.completed_at is None
            command.available_at = datetime.now(UTC) - timedelta(seconds=1)
        assert (
            claim_command(session_factory, "restart", workflow=WORKFLOW, kind=PUBLISH_CANDIDATE)
            == job
        )
        assert not process(session_factory, job, "worker", artifacts=artifacts)
        assert process(session_factory, job, "restart", artifacts=artifacts)
    published = draft.open_approved_draft(session_factory, run.id)
    assert published["pull_request_url"] == "https://github.com/fixture/repo/pull/18"
    assert published["target_sha256"] == pushed["target_sha256"]
    assert draft.open_approved_draft(session_factory, run.id) == published
    assert methods == (["GET", "POST", "GET"] if lost_response else ["GET", "POST"])
    with session_factory() as session:
        assert session.get(RunCommand, job).status == "DONE"
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == draft.PUBLICATION_PR_OPENED)
        ).all()
        assert [event.payload for event in events] == [published]
