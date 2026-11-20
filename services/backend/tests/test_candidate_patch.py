import os

import pytest

import refactra.candidate_patch as patching
from refactra.candidate_patch import (
    CandidatePatch,
    CandidatePatchError,
    apply_candidate_patch,
    read_candidate_patch,
)
from refactra.refactoring_plan import ProposedChange
from refactra.repository_workspace import ApprovedWorkspace

BASE = "a" * 40
PLAN = "b" * 64


@pytest.fixture
def patch_fixture(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "cart.py").write_bytes(b"return 1")
    (root / "old.py").write_bytes(b"")
    (root / "unrelated.py").write_bytes(b"keep\n")
    edits = [
        {"path": "cart.py", "action": "MODIFY", "content": "return 2\n"},
        {"path": "tests/test_cart.py", "action": "CREATE", "content": "assert True\n"},
        {"path": "old.py", "action": "DELETE", "content": None},
    ]
    changes = tuple(
        ProposedChange(path=edit["path"], action=edit["action"], intent="Test action")
        for edit in edits
    )
    return ApprovedWorkspace(root, BASE, 2, PLAN, 3, changes), edits


def test_candidate_applies_exact_scope_and_reports_the_actual_diff(tmp_path, patch_fixture):
    workspace, edits = patch_fixture
    path = tmp_path / "candidate.json"
    path.write_text(CandidatePatch(base_sha=BASE, plan_sha256=PLAN, edits=edits).model_dump_json())
    diff = apply_candidate_patch(workspace, read_candidate_patch(path))
    assert (workspace.root / "cart.py").read_bytes() == b"return 2\n"
    assert (workspace.root / "tests/test_cart.py").read_bytes() == b"assert True\n"
    assert not (workspace.root / "old.py").exists()
    assert (workspace.root / "unrelated.py").read_bytes() == b"keep\n"
    assert b"-return 1\n\\ No newline at end of file\n+return 2\n" in diff
    assert b"--- /dev/null\n+++ b/tests/test_cart.py\n" in diff
    assert b"--- a/old.py\n+++ /dev/null\n" in diff
    assert b"unrelated.py" not in diff


@pytest.mark.parametrize(
    "invalid",
    [
        "revision",
        "outside",
        "missing",
        "duplicate",
        "action",
        "delete_text",
        "file_limit",
        "diff_limit",
    ],
)
def test_invalid_candidates_leave_the_whole_workspace_untouched(
    patch_fixture, invalid, monkeypatch
):
    workspace, edits = patch_fixture
    if invalid == "outside":
        edits[1]["path"] = "unrelated.py"
    elif invalid == "missing":
        edits.pop()
    elif invalid == "duplicate":
        edits.append(edits[0])
    elif invalid == "action":
        edits[0]["action"] = "CREATE"
    elif invalid == "delete_text":
        edits[2]["content"] = "invalid deletion"
    elif invalid == "file_limit":
        edits[1]["content"] = "é" * (patching.MAX_EDIT_FILE_BYTES // 2 + 1)
    elif invalid == "diff_limit":
        monkeypatch.setattr(patching, "MAX_DIFF_BYTES", 100)
    candidate = CandidatePatch(
        base_sha=BASE, plan_sha256="c" * 64 if invalid == "revision" else PLAN, edits=edits
    )
    with pytest.raises(CandidatePatchError):
        apply_candidate_patch(workspace, candidate)
    assert (workspace.root / "cart.py").read_bytes() == b"return 1"
    assert (workspace.root / "old.py").exists()
    assert not (workspace.root / "tests").exists()


def test_candidate_rejects_a_linked_file(tmp_path, patch_fixture):
    workspace, edits = patch_fixture
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside\n")
    destination = workspace.root / "cart.py"
    destination.unlink()
    os.link(outside, destination)
    candidate = CandidatePatch(base_sha=BASE, plan_sha256=PLAN, edits=edits)
    with pytest.raises(CandidatePatchError, match="non-regular file"):
        apply_candidate_patch(workspace, candidate)
    assert outside.read_bytes() == b"outside\n"
