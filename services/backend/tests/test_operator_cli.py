"""Smoke tests for the operator command-line tools documented in the README."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import refactra.candidate_commit as commit_cli
import refactra.publication_branch as branch_cli
import refactra.publication_pr as draft_cli
import refactra.repository_candidate as candidate_cli
import refactra.repository_plan_worker as plan_cli
import refactra.repository_workspace as workspace_cli
from refactra.candidate_commit import CandidateCommitError
from refactra.candidate_patch import CandidatePatch, CandidatePatchError
from refactra.db import make_engine
from refactra.publication_target import PublicationTargetError
from refactra.refactoring_plan import PlanContent, ProposedChange
from refactra.repository_plan_worker import PlanGenerationError
from refactra.repository_workspace import ApprovedWorkspace, WorkspacePreparationError

from .test_refactoring_plan import proposal as proposal

SECRET = "ghs_secret-token-value"
RUN = str(uuid4())
SHA = "a" * 40
PLAN_SHA = "b" * 64


@pytest.fixture
def cli(monkeypatch, capsys):
    """Run a tool's main() with an unconnected engine and report exit, output and disposal."""
    monkeypatch.setenv("REFACTRA_OPENAI_API_KEY", "fixture-key")
    state = {"disposed": 0, "engines": 0}

    def engine_for(_url):
        state["engines"] += 1
        engine = make_engine("postgresql+psycopg://refactra:hunter2@127.0.0.1:1/refactra")
        real_dispose = engine.dispose

        def dispose(*args, **kwargs):
            state["disposed"] += 1
            return real_dispose(*args, **kwargs)

        engine.dispose = dispose
        return engine

    def run(module, *argv):
        monkeypatch.setattr(module, "make_engine", engine_for)
        monkeypatch.setattr("sys.argv", [module.__name__, *argv])
        code = 0
        try:
            module.main()
        except SystemExit as stopped:
            code = stopped.code
        return code, capsys.readouterr().out, state

    return run


def plan_document(proposal):
    content = PlanContent(
        **proposal,
        schema_version="1",
        run_id=UUID(RUN),
        repository={"repository_id": 42, "base_branch": "main", "base_sha": SHA},
    )
    return content.model_dump_json()


def patch():
    edit = {"path": "src/cart.py", "action": "MODIFY", "content": "def cart(): return 2\n"}
    return CandidatePatch(base_sha=SHA, plan_sha256=PLAN_SHA, edits=[edit])


# module, arguments, patched function, known error, generic message for unexpected errors
TOOLS = {
    "workspace": (
        workspace_cli,
        [RUN],
        "prepare_approved_workspace",
        WorkspacePreparationError,
        "Workspace preparation stopped. Check repository access and local setup.",
    ),
    "candidate": (
        candidate_cli,
        [RUN, "--output", "{tmp}/candidate.json"],
        "generate_candidate",
        CandidatePatchError,
        "Candidate generation stopped. Check run access and the model receipt.",
    ),
    "plan": (
        plan_cli,
        [RUN],
        "generate_initial_plan",
        PlanGenerationError,
        "Plan generation stopped. Check run access and the model receipt.",
    ),
    "commit": (
        commit_cli,
        [RUN, "1", "--source", "{tmp}", "--output", "{tmp}/artifact"],
        "prepare_candidate_commit",
        CandidateCommitError,
        "Commit preparation stopped. Check the saved revision and local setup.",
    ),
    "branch": (
        branch_cli,
        [RUN, "--artifact", "{tmp}/artifact", "--source", "{tmp}"],
        "push_approved_branch",
        PublicationTargetError,
        "Branch publication stopped. Check repository access and the artifact.",
    ),
    "draft": (
        draft_cli,
        [RUN],
        "open_approved_draft",
        PublicationTargetError,
        "Draft PR publication stopped. Check repository access and publication state.",
    ),
}


def arguments(tool, tmp_path):
    return [item.format(tmp=tmp_path) for item in TOOLS[tool][1]]


@pytest.mark.parametrize("tool", TOOLS)
def test_known_errors_surface_their_own_message(cli, monkeypatch, tmp_path, tool):
    module, _, target, known, _ = TOOLS[tool]

    def fail(*_args, **_kwargs):
        raise known("The saved revision is not ready.")

    monkeypatch.setattr(module, target, fail)
    code, output, state = cli(module, *arguments(tool, tmp_path))
    assert code == "The saved revision is not ready." and output == ""
    assert state["disposed"] == state["engines"] == 1


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("error", [ValueError, OSError, KeyError])
def test_unexpected_errors_print_only_a_generic_message(cli, monkeypatch, tmp_path, tool, error):
    module, _, target, _, generic = TOOLS[tool]

    def fail(*_args, **_kwargs):
        raise error(f"Authorization: Bearer {SECRET}")

    monkeypatch.setattr(module, target, fail)
    code, output, state = cli(module, *arguments(tool, tmp_path))
    assert code == generic
    assert SECRET not in str(code) + output
    assert state["disposed"] == 1


def test_each_tool_prints_its_result_on_success(cli, monkeypatch, tmp_path, proposal):
    receipt = {"run_id": RUN, "commit_sha": "c" * 40}
    calls = {}

    def returning(name, value):
        def record(*args, **kwargs):
            calls[name] = (args, kwargs)
            return value

        return record

    monkeypatch.setattr(
        plan_cli,
        "generate_initial_plan",
        returning(
            "plan",
            SimpleNamespace(
                id=uuid4(), revision=1, sha256=PLAN_SHA, document=plan_document(proposal)
            ),
        ),
    )
    code, output, _ = cli(plan_cli, RUN)
    planned = json.loads(output)
    assert code == 0 and planned["revision"] == 1 and planned["readiness"]["outcome"] == "ready"

    monkeypatch.setattr(candidate_cli, "generate_candidate", returning("candidate", patch()))
    code, output, _ = cli(candidate_cli, RUN, "--output", str(tmp_path / "candidate.json"))
    assert code == 0 and output.strip() == "Saved candidate with 1 approved edits."
    saved = json.loads((tmp_path / "candidate.json").read_text(encoding="utf-8"))
    assert saved["edits"][0]["path"] == "src/cart.py"

    for module, target, argv in [
        (commit_cli, "prepare_candidate_commit", arguments("commit", tmp_path)),
        (branch_cli, "push_approved_branch", arguments("branch", tmp_path)),
        (draft_cli, "open_approved_draft", arguments("draft", tmp_path)),
    ]:
        monkeypatch.setattr(module, target, returning(target, receipt))
        code, output, _ = cli(module, *argv)
        assert code == 0 and json.loads(output) == receipt
    assert calls["push_approved_branch"][1]["source"] == tmp_path  # --source reaches the rebuild.

    change = ProposedChange(path="src/cart.py", action="MODIFY", intent="Extract the total.")

    @contextmanager
    def workspace(_session_factory, _run_id):
        yield ApprovedWorkspace(tmp_path, SHA, 2, PLAN_SHA, 1, (change,))

    monkeypatch.setattr(workspace_cli, "prepare_approved_workspace", workspace)
    code, output, _ = cli(workspace_cli, RUN)
    report = json.loads(output)
    assert code == 0 and (report["base_sha"], report["revision"]) == (SHA, 2)
    assert report["approved_changes"] == [{"path": "src/cart.py", "action": "MODIFY"}]


def test_candidate_retry_queues_one_command(cli, monkeypatch):
    command = uuid4()
    monkeypatch.setattr(candidate_cli, "queue_candidate_retry", lambda _session, _run: command)
    code, output, state = cli(candidate_cli, RUN, "--retry")
    assert code == 0 and output.strip() == f"Candidate retry command {command}"
    assert state["disposed"] == 1


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (workspace_cli, ["not-a-run-id"]),
        (workspace_cli, [RUN, "--validate"]),
        (workspace_cli, [RUN, "--candidate", "patch.json", "--save-result"]),
        (candidate_cli, [RUN]),
        (candidate_cli, [RUN, "--output", "x.json", "--retry"]),
        (candidate_cli, [RUN, "--output", "{tmp}/missing-dir/x.json"]),
        (plan_cli, []),
        (commit_cli, [RUN, "1", "--output", "out"]),
        (commit_cli, [RUN, "not-a-number", "--source", ".", "--output", "out"]),
        (branch_cli, [RUN]),
        (draft_cli, ["not-a-run-id"]),
    ],
)
def test_invalid_arguments_exit_before_creating_an_engine(cli, tmp_path, module, argv):
    code, _, state = cli(module, *[item.format(tmp=tmp_path) for item in argv])
    assert code == 2 and state["engines"] == 0


def test_missing_model_key_stops_with_the_generic_message(cli, monkeypatch, tmp_path):
    monkeypatch.delenv("REFACTRA_OPENAI_API_KEY")
    code, _, state = cli(plan_cli, RUN)
    assert code == TOOLS["plan"][4] and state["disposed"] == 1
    code, _, _ = cli(candidate_cli, RUN, "--output", str(tmp_path / "x.json"))
    assert code == TOOLS["candidate"][4]
