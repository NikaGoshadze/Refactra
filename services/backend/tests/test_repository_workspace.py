import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5

import httpx
import pytest

from refactra import repository_workspace as workspace
from refactra.analysis_context import save_analysis_context
from refactra.candidate_patch import CandidatePatch, CandidatePatchError
from refactra.models import ModelReservation, RunCommand, RunGeneratedCandidate
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.refactoring_plan import (
    APPLY_PLAN_APPROVAL,
    PATCH_CHECK,
    PlanContent,
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
    candidate_attempt_ids,
    generate_candidate,
    process_repository_candidate,
    queue_candidate_retry,
)
from refactra.repository_snapshot import SourceFile, SourceSnapshot
from refactra.repository_validation import validate_candidate
from refactra.worker import claim_command

from .test_analysis_context import prepare
from .test_openai_analysis import response
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal
from .test_repository_snapshot import SHA, transport_for, zip_bytes
from .test_repository_snapshot import token as token


@pytest.mark.parametrize(
    ("path", "action", "sources", "error"),
    [
        ("src/cart.py", "CREATE", ["src/cart.py"], "action does not match"),
        ("src/cart.py", "DELETE", [], "action does not match"),
        (".env", "MODIFY", [".env"], "outside the edit scope"),
        ("services/.env-prod", "CREATE", [], "outside the edit scope"),
        (".github/.env", "DELETE", [".github/.env"], "outside the edit scope"),
        ("tests/conftest.py", "CREATE", [], "outside the edit scope"),
        (".github/handler.py", "CREATE", [], "outside the edit scope"),
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


@pytest.mark.parametrize("config, expected", [('{"enabled":true}', "PASSED"), ("{", "FAILED")])
def test_frontend_and_config_patch_uses_builtin_validation(tmp_path, proposal, config, expected):
    paths = ["apps/web/page.tsx", "settings.json"]
    original = ["export const title = <h1>Before</h1>;\n", "{}"]
    changes = [{"path": path, "action": "MODIFY", "intent": "Update setting."} for path in paths]
    check = dict(proposal["validation_checks"][0], check_id=PATCH_CHECK, paths=paths)
    plan = PlanContent(
        **(proposal | {"changes": changes, "validation_checks": [check]}),
        schema_version="1",
        run_id=uuid4(),
        repository={"repository_id": 42, "base_branch": "main", "base_sha": SHA},
    )
    snapshot = SourceSnapshot(
        42,
        SHA,
        tuple(SourceFile(p, text.encode()) for p, text in zip(paths, original, strict=True)),
    )
    files = workspace._workspace_files(snapshot, plan)
    for path, content in files.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    approved = workspace.ApprovedWorkspace(
        tmp_path, SHA, 1, "b" * 64, len(files), tuple(plan.changes), tuple(plan.validation_checks)
    )
    patch = CandidatePatch(
        base_sha=SHA,
        plan_sha256=approved.sha256,
        edits=[
            {"path": path, "action": "MODIFY", "content": text}
            for path, text in zip(
                paths, ["export const title = <h1>After</h1>;\n", config], strict=True
            )
        ],
    )
    diff = workspace.apply_candidate_patch(approved, patch)
    assert all(path.encode() in diff for path in paths)
    (result,) = validate_candidate(approved)
    assert (result.status, result.image_id) == (expected, "builtin")


@pytest.mark.parametrize(
    "scenario",
    [
        "valid",
        "recover",
        "outside_scope",
        "repair",
        "manual_retry",
        "reference_budget",
        "missing_import",
        "package_call",
    ],
)
def test_workspace_requires_verified_approval_and_stages_pinned_source(
    session_factory, proposal, token, monkeypatch, scenario
):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    pinned_cart = b"from .pricing import price\nfrom .settings import API_KEY\n"
    pinned_pricing = b"def price(quantity): return quantity * 2\n"
    archive = zip_bytes(
        [
            ("root/src/cart.py", pinned_cart),
            ("root/src/pricing.py", pinned_pricing),
            ("root/src/settings.py", b"API_KEY = 'synthetic-private-value'\n"),
            ("root/.env", b"secret"),
            ("root/src/unrelated.py", b"not approved"),
            ("root/requirements.lock", b"pinned lock metadata"),
        ]
    )
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
        assert prepared.file_count == 4 and (root / "src/cart.py").read_bytes() == pinned_cart
        assert prepared.python_lock == b"pinned lock metadata"
        assert not (root / "requirements.lock").exists()
        assert not (root / ".env").exists()
        assert not (root / "tests/test_cart.py").exists()
        assert [(change.path, change.action) for change in prepared.changes] == [
            ("src/cart.py", "MODIFY"),
            ("tests/test_cart.py", "CREATE"),
        ]
    assert calls == [SHA] and not root.exists()

    edit_path = "src/cart.py"
    patch = {
        "base_sha": run.base_sha,
        "plan_sha256": plan.sha256,
        "edits": [
            {"path": edit_path, "action": "MODIFY", "content": "def cart(): return 2\n"},
            {
                "path": "tests/test_cart.py",
                "action": "CREATE",
                "content": "def test_cart(): pass\n",
            },
        ],
    }
    requests = []

    def model(request):
        requests.append(request.url.path)
        payload = json.loads(request.content)
        assert payload["text"]["format"]["name"] == "candidate_patch"
        assert payload["text"]["format"]["strict"] is True
        schema = payload["text"]["format"]["schema"]["properties"]["edits"]
        assert schema["required"] == ["src/cart.py", "tests/test_cart.py"]
        assert schema["additionalProperties"] is False
        prompt = json.loads(payload["input"])
        assert prompt["source"] == {"src/cart.py": pinned_cart.decode()}
        if scenario == "reference_budget" and len(requests) > 1:
            assert "references" not in prompt
        else:
            assert prompt["references"] == {"src/pricing.py": pinned_pricing.decode()}
        assert "synthetic-private-value" not in payload["input"]
        assert prompt["approved_plan"] == json.loads(plan.document)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200,
                json={
                    "object": "response.input_tokens",
                    "input_tokens": 12001
                    if scenario == "reference_budget" and len(requests) == 1
                    else 100,
                },
            )
        assert payload["tools"] == [] and payload["store"] is False
        result = response()
        retrying = prompt.get("retry_after_rejection", False)
        edits = {edit["path"]: edit["content"] for edit in patch["edits"]}
        if scenario == "outside_scope":
            edits["src/pricing.py"] = edits.pop("src/cart.py")
        if scenario == "repair":
            if retrying:
                assert "unexpected keyword argument 'author'" in prompt["repair_detail"]
                assert "src/cart.py" in prompt["repair_detail"]
            else:
                edits["tests/test_cart.py"] = (
                    "from src.cart import cart\n\ndef test_cart():\n    cart(author='Writer')\n"
                )
        if scenario == "missing_import":
            if retrying:
                assert (
                    "imports get_price, which is not defined in src/pricing.py"
                    in prompt["repair_detail"]
                )
            else:
                edits[edit_path] = (
                    "from src.pricing import get_price\n\ndef cart(): return get_price(2)\n"
                )
        if scenario == "package_call":
            if retrying:
                assert "unexpected keyword argument 'limit'" in prompt["repair_detail"]
                assert "src/pricing.py" in prompt["repair_detail"]
            else:
                edits[edit_path] = (
                    "if __package__:\n    from .pricing import price\n"
                    "else:\n    from pricing import price\n\ndef cart(): return price(2, limit=3)\n"
                )
        if scenario == "manual_retry" and not retrying:
            result.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        result["output"][0]["content"][0]["text"] = json.dumps({"edits": edits})
        return httpx.Response(200, json=result)

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    generation = claim_command(
        session_factory, "worker", workflow=WORKFLOW, kind=GENERATE_CANDIDATE
    )
    assert generation is not None
    if scenario == "recover":
        assert generate_candidate(session_factory, run.id, provider).model_dump() == patch
        with session_factory.begin() as session:
            row = session.get(RunCommand, generation)
            row.lease_until = datetime.now(UTC) - timedelta(minutes=1)
        assert (
            claim_command(session_factory, "restart", workflow=WORKFLOW, kind=GENERATE_CANDIDATE)
            == generation
        )
        assert not process_repository_candidate(session_factory, generation, "worker", provider)
        snapshots = len(calls)
        assert process_repository_candidate(session_factory, generation, "restart", provider)
        assert len(calls) == snapshots
    else:
        assert process_repository_candidate(session_factory, generation, "worker", provider)
    if scenario == "manual_retry":
        with session_factory.begin() as session:
            assert session.get(RunCommand, generation).status == "FAILED"
            retry = queue_candidate_retry(session, run.id)
        with session_factory.begin() as session:
            assert queue_candidate_retry(session, run.id) == retry
        assert (
            claim_command(session_factory, "worker", workflow=WORKFLOW, kind=GENERATE_CANDIDATE)
            == retry
        )
        assert process_repository_candidate(session_factory, retry, "worker", provider)
        generation = retry
    retried = scenario in {
        "outside_scope",
        "repair",
        "manual_retry",
        "missing_import",
        "package_call",
    }
    recount = ["/v1/responses/input_tokens"] if scenario == "reference_budget" else []
    assert requests == recount + ["/v1/responses/input_tokens", "/v1/responses"] * (
        2 if retried else 1
    )
    with session_factory() as session:
        attempt_id = uuid5(run.id, f"repository:candidate:v1:{plan.sha256}")
        assert session.get(ModelReservation, attempt_id).actual_output_tokens == 20
        saved = session.get(RunGeneratedCandidate, run.id)
        command = session.get(RunCommand, generation)
        if scenario == "outside_scope":
            assert saved is None and command.status == "FAILED"
        else:
            assert command.status == "DONE"
            assert saved.reservation_id in candidate_attempt_ids(run.id, plan.sha256)
            assert (saved.reservation_id != attempt_id) == retried
            assert CandidatePatch.model_validate_json(saved.document).model_dump() == patch
    if scenario == "outside_scope":
        with pytest.raises(CandidatePatchError, match="attempt is already recorded"):
            generate_candidate(session_factory, run.id, provider)
    else:
        assert generate_candidate(session_factory, run.id, provider).model_dump() == patch
    assert len(requests) == len(recount) + (4 if retried else 2)
