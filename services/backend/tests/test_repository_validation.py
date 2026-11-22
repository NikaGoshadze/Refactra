import json
import os
import subprocess
import sys

import pytest

from refactra import repository_validation as validation
from refactra.python_test_support import (
    PYTEST_ENTRY,
    declared_test_imports,
    python_unavailable_reason,
)
from refactra.refactoring_plan import ProposedChange, ValidationCheck
from refactra.repository_workspace import ApprovedWorkspace


def test_validation_preflights_explicit_paths_before_docker(tmp_path, monkeypatch):
    check = ValidationCheck(
        check_id="python.pytest", paths=["--collect-only.py"], required=True, criteria_ids=["AC1"]
    )
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 0, (), (check,))
    monkeypatch.setattr(validation, "_docker", lambda *args: pytest.fail("Docker was called"))
    unavailable, baseline = validation.validate_candidate(workspace)
    assert unavailable.status == "UNAVAILABLE" and "staged Python files" in unavailable.reason
    assert baseline.status == "PASSED" and baseline.check_id == "repository.patch"
    assert not validation.blocks_candidate([unavailable, baseline])
    (tmp_path / "--collect-only.py").touch()
    assert validation._targets(workspace, check) == ["./--collect-only.py"]


def test_validation_refuses_to_start_with_network_access():
    with pytest.raises(validation.RepositoryValidationError, match="required validation isolation"):
        validation._verify_isolation({"HostConfig": {"NetworkMode": "bridge"}, "Mounts": []})


@pytest.mark.parametrize(
    ("contents", "secondary", "unavailable"),
    [
        ("import fixture_missing_dependency", None, "fixture-missing-dependency"),
        ("# no tests here", None, "collected no tests"),
        ("import fixture_missing_dependency", "import missing_repo_module", None),
        ("import missing_repo_module", None, None),
        ("def test_broken(): assert False", None, None),
    ],
)
def test_pytest_distinguishes_unavailable_checks_from_real_errors(
    tmp_path, contents, secondary, unavailable
):
    (tmp_path / "requirements.txt").write_text("fixture-missing-dependency>=1\n")
    files = ["test_first.py"]
    (tmp_path / files[0]).write_text(contents)
    if secondary:
        files.append("test_second.py")
        (tmp_path / files[1]).write_text(secondary)
    declared = declared_test_imports(tmp_path, files)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            PYTEST_ENTRY,
            json.dumps(declared),
            "-q",
            "--noconftest",
            "-p",
            "no:cacheprovider",
            *files,
        ],
        cwd=tmp_path,
        env=os.environ | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    reason = python_unavailable_reason(completed.returncode, completed.stdout, declared)
    if unavailable:
        assert reason and unavailable in reason, completed.stdout
    else:
        assert reason is None and completed.returncode in {1, 2}, completed.stdout


def test_declared_imports_include_nested_manifest_and_exclude_local_modules(tmp_path):
    package = tmp_path / "app"
    package.mkdir()
    (tmp_path / "requirements.txt").write_text("flask\npython-dotenv>=1\nlocal-module\n")
    (tmp_path / "local_module.py").touch()
    (package / "pyproject.toml").write_text(
        '[project]\ndependencies = ["Pillow>=1"]\n[dependency-groups]\ntest = ["PyYAML"]\n'
    )
    assert declared_test_imports(tmp_path, ["app/test_route.py"]) == {
        "flask": "flask",
        "dotenv": "python-dotenv",
        "PIL": "pillow",
        "yaml": "pyyaml",
    }


@pytest.mark.skipif(os.getenv("REFACTRA_TEST_PYTHON_DOCKER") != "1", reason="Requires Docker.")
def test_cached_web_dependencies_run_inside_the_offline_container(tmp_path):
    path = "test_web.py"
    (tmp_path / path).write_text(
        "from dotenv import dotenv_values\n"
        "from flask import Flask\n"
        "def test_web_ready():\n"
        "    assert Flask(__name__).test_client().get('/').status_code == 404\n"
        "    assert callable(dotenv_values)\n"
    )
    change = ProposedChange(path=path, action="CREATE", intent="Verify web dependencies.")
    check = ValidationCheck(
        check_id="python.pytest", paths=[path], required=True, criteria_ids=["AC1"]
    )
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 1, (change,), (check,))
    pytest_result, baseline = validation.validate_candidate(workspace)
    assert pytest_result.status == baseline.status == "PASSED", pytest_result.output
