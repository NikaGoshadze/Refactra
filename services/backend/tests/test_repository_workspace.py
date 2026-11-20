from uuid import uuid4

import pytest

from refactra import repository_workspace as workspace
from refactra.analysis_context import save_analysis_context
from refactra.refactoring_plan import (
    APPLY_PLAN_APPROVAL,
    PlanContent,
    approve_refactoring_plan,
    save_refactoring_plan,
)
from refactra.repository_analysis_worker import (
    WORKFLOW,
    enqueue_repository_analysis,
    process_repository_plan_approval,
)
from refactra.repository_snapshot import SourceFile, SourceSnapshot
from refactra.worker import claim_command

from .test_analysis_context import prepare
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal
from .test_repository_snapshot import SHA, transport_for, zip_bytes
from .test_repository_snapshot import token as token


@pytest.mark.parametrize(
    ("path", "action", "sources", "error"),
    [
        ("src/cart.py", "CREATE", ["src/cart.py"], "action does not match"),
        ("src/cart.py", "DELETE", [], "action does not match"),
        ("pyproject.toml", "MODIFY", ["pyproject.toml"], "editable Python"),
        ("tests/conftest.py", "CREATE", [], "editable Python"),
        (".github/handler.py", "CREATE", [], "editable Python"),
        ("src/cart.py", "CREATE", ["src/cart.py/inside.py"], "overlap a directory"),
        ("src/cart.py", "CREATE", ["SRC/other.py"], "paths collide"),
        ("src/cart.py", "CREATE", ["src/../escape.py"], "unsupported filesystem path"),
        ("src/cart.py", "CREATE", ["aux.txt"], "unsupported filesystem path"),
    ],
)
def test_workspace_rejects_incompatible_paths(proposal, path, action, sources, error):
    proposal["changes"] = [{"path": path, "action": action, "intent": "Change a file."}]
    content = PlanContent(
        **proposal,
        schema_version="1",
        run_id=uuid4(),
        repository={"repository_id": 42, "base_branch": "main", "base_sha": SHA},
    )
    snapshot = SourceSnapshot(42, SHA, tuple(SourceFile(name, b"data") for name in sources))
    with pytest.raises(workspace.WorkspacePreparationError, match=error):
        workspace._workspace_files(snapshot, content)


def test_workspace_requires_verified_approval_and_stages_pinned_source(
    session_factory, proposal, token, monkeypatch
):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    archive = zip_bytes([("root/src/cart.py", b"pinned"), ("root/.env", b"secret")])
    calls = []

    def authorize(persisted_run):
        calls.append(persisted_run.base_sha)
        assert persisted_run.id == run.id
        return token, transport_for(archive)

    monkeypatch.setattr(workspace, "authorize_plan", authorize)
    with pytest.raises(workspace.WorkspacePreparationError, match="matching approval"):
        with workspace.prepare_approved_workspace(session_factory, run.id):
            pytest.fail("Unexpected workspace")
    proposal["changes"].append(
        {"path": "tests/test_cart.py", "action": "CREATE", "intent": "Cover cart extraction."}
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
    with pytest.raises(workspace.WorkspacePreparationError, match="worker must verify"):
        with workspace.prepare_approved_workspace(session_factory, run.id):
            pytest.fail("Unexpected workspace")
    assert calls == []

    command = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=APPLY_PLAN_APPROVAL)
    assert process_repository_plan_approval(session_factory, command, "worker")
    with workspace.prepare_approved_workspace(session_factory, run.id) as prepared:
        root = prepared.root
        assert prepared.base_sha == run.base_sha and prepared.sha256 == plan.sha256
        assert prepared.file_count == 1 and (root / "src/cart.py").read_bytes() == b"pinned"
        assert not (root / ".env").exists()
        assert not (root / "tests/test_cart.py").exists()
        assert [(change.path, change.action) for change in prepared.changes] == [
            ("src/cart.py", "MODIFY"),
            ("tests/test_cart.py", "CREATE"),
        ]
    assert calls == [SHA] and not root.exists()
