import json
import shutil
import subprocess
from hashlib import sha256

import pytest

from refactra.analysis_context import save_analysis_context
from refactra.candidate_commit import CandidateCommitError, prepare_candidate_commit
from refactra.candidate_patch import CandidatePatch
from refactra.candidate_results import save_validated_candidate
from refactra.refactoring_plan import approve_refactoring_plan, save_refactoring_plan
from refactra.repository_analysis_worker import enqueue_repository_analysis
from refactra.repository_validation import ValidationResult

from .test_analysis_context import prepare
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args])


@pytest.mark.skipif(shutil.which("git") is None, reason="Requires local Git.")
def test_prepares_the_saved_diff_as_one_pinned_commit(tmp_path, session_factory, proposal):
    source = tmp_path / "clone"
    source.mkdir()
    git(source, "init", "--template=", "--object-format=sha1")
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
        edit = candidate.edits[0].model_copy(update={"content": "def cart(): return 3\n"})
        mismatch = candidate.model_copy(update={"edits": [edit, *candidate.edits[1:]]})
        save_validated_candidate(session, run.id, mismatch, diff, [check])

    output = tmp_path / "prepared"
    with pytest.raises(CandidateCommitError, match="reviewed diff"):
        prepare_candidate_commit(session_factory, run.id, 2, source, output)
    assert not output.exists()
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
