import pytest

from refactra.candidate_request import CandidateSourceWithheld, approved_source
from refactra.refactoring_plan import ProposedChange
from refactra.repository_workspace import ApprovedWorkspace

SHA = "a" * 40


def _workspace(root, files: dict[str, bytes], actions: dict[str, str]) -> ApprovedWorkspace:
    for path, content in files.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    changes = tuple(
        ProposedChange(path=path, action=action, intent="Change the file.")
        for path, action in actions.items()
    )
    return ApprovedWorkspace(root, SHA, 1, "b" * 64, len(files), changes)


def test_ordinary_sources_and_template_names_reach_the_model(tmp_path):
    workspace = _workspace(
        tmp_path,
        {
            "src/cart.py": b"def cart():\n    return 2\n",
            ".env.example": b'API_KEY="changeme"\nDEBUG=true\n',
        },
        {"src/cart.py": "MODIFY", ".env.example": "MODIFY", "tests/test_cart.py": "CREATE"},
    )
    assert approved_source(workspace) == {
        "src/cart.py": "def cart():\n    return 2\n",
        ".env.example": "API_KEY=\nDEBUG=\n",
    }


@pytest.mark.parametrize("action", ["MODIFY", "DELETE"])
@pytest.mark.parametrize(
    "secret",
    [
        b'API_KEY = "synthetic-edit-secret"\n',
        b"-----BEGIN PRIVATE KEY-----\nc3ludGhldGlj\n-----END PRIVATE KEY-----\n",
        b'URL = "https://user:synthetic-password@example.com/"\n',
    ],
)
def test_edit_targets_matching_the_disclosure_policy_are_withheld(tmp_path, action, secret):
    workspace = _workspace(
        tmp_path,
        {"src/cart.py": b"def cart():\n    return 2\n", "src/settings.py": secret},
        {"src/cart.py": "MODIFY", "src/settings.py": action},
    )
    with pytest.raises(CandidateSourceWithheld) as caught:
        approved_source(workspace)
    assert caught.value.path == "src/settings.py"
    assert caught.value.reason == "possible_secret"
    assert b"synthetic" not in str(caught.value).encode()
