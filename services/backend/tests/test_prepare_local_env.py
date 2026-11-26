"""Run scripts/prepare-local-env.py against a copy of .env.example."""

import importlib.util
import os
import shutil
from base64 import urlsafe_b64decode
from pathlib import Path

import pytest

# The repository root is a few levels up; the backend image ships without scripts/.
ROOT = next(
    (
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "scripts" / "prepare-local-env.py").is_file()
    ),
    None,
)


@pytest.fixture
def prepare():
    if ROOT is None:
        if os.getenv("GITHUB_ACTIONS"):
            pytest.fail("CI must run the local environment script test.")
        pytest.skip("Requires the repository scripts.")
    spec = importlib.util.spec_from_file_location(
        "prepare_local_env", ROOT / "scripts" / "prepare-local-env.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.prepare_local_env


def settings(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def test_both_keys_are_generated_once_and_the_rest_is_copied(prepare, tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    assert prepare(tmp_path) is True
    example, created = settings(tmp_path / ".env.example"), settings(tmp_path / ".env")
    keys = ("REFACTRA_OAUTH_STATE_SECRET", "REFACTRA_TOKEN_ENCRYPTION_KEY")
    for key in keys:
        assert example[key] == ""
        assert len(urlsafe_b64decode(created[key] + "=")) == 32
    assert created[keys[0]] != created[keys[1]]
    assert {k: v for k, v in created.items() if k not in keys} == {
        k: v for k, v in example.items() if k not in keys
    }

    first = (tmp_path / ".env").read_bytes()
    assert prepare(tmp_path) is False
    assert (tmp_path / ".env").read_bytes() == first


def test_a_template_without_the_key_placeholder_is_rejected(prepare, tmp_path):
    (tmp_path / ".env.example").write_text(
        "# Local settings\nREFACTRA_OAUTH_STATE_SECRET=\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="REFACTRA_TOKEN_ENCRYPTION_KEY"):
        prepare(tmp_path)
    assert not (tmp_path / ".env").exists()
