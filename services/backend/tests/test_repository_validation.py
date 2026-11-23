import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from refactra import repository_validation as validation
from refactra.python_dependency_cache import PythonLockError, pinned_python_requirements
from refactra.python_test_support import (
    PYTEST_ENTRY,
    declared_test_imports,
    python_unavailable_reason,
)
from refactra.refactoring_plan import FLASK_CHECK, ProposedChange, ValidationCheck
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


@pytest.mark.skipif(os.getenv("REFACTRA_TEST_PYTHON_DOCKER") != "1", reason="Requires Docker.")
@pytest.mark.parametrize(
    "mode, expected",
    [("fixed", "PASSED"), ("wrong_type", "FAILED"), ("helper_only", "UNAVAILABLE")],
)
def test_flask_smoke_checks_a_changed_route_with_fake_dependencies(tmp_path, mode, expected):
    argument = "search" if mode == "wrong_type" else "store"
    (tmp_path / "web_app.py").write_text(
        "from flask import Flask, request\n"
        "app = Flask(__name__)\n"
        "def search(query, backend):\n    return backend.similarity_search(query)\n"
        "store = None\n"
        "@app.post('/search')\n"
        f"def index():\n    return search(request.form['query'], {argument})\n"
    )
    body = """from types import SimpleNamespace
import web_app

def test_search(monkeypatch):
    fake = SimpleNamespace(similarity_search=lambda query: 'Found ' + query)
    monkeypatch.setattr(web_app, 'store', fake)
    response = web_app.app.test_client().post('/search', data={'query': 'Ada'})
    assert response.status_code == 200
    assert response.data == b'Found Ada'
"""
    if mode == "helper_only":
        body = "import web_app\ndef test_helper(): assert callable(web_app.search)\n"
    name = "test_web_app.py"
    (tmp_path / name).write_text(body)
    change = ProposedChange(path="web_app.py", action="MODIFY", intent="Update the Flask route.")
    check = ValidationCheck(check_id=FLASK_CHECK, paths=[name], required=True, criteria_ids=["AC1"])
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 2, (change,), (check,))
    result, baseline = validation.validate_candidate(workspace)
    assert result.status == expected and baseline.status == "PASSED", (result, baseline)
    assert validation.blocks_candidate([result, baseline]) == (mode == "wrong_type")


def lock_for(version: str = "1.0") -> bytes:
    return f"example-wheel=={version} --hash=sha256:{'a' * 64}\n".encode()


@pytest.mark.parametrize(
    "content", [b"flask>=3\n", b"flask==3.0\n", b"-r other.txt\n", b"thing @ https://example.org"]
)
def test_python_lock_rejects_unpinned_or_non_registry_requirements(content):
    with pytest.raises(PythonLockError):
        pinned_python_requirements(content)


def test_python_lock_accepts_hash_continuations_and_comments():
    lock = b"# generated lock\n" + lock_for().replace(b" --hash", b" \\\n    --hash")
    assert pinned_python_requirements(lock) == lock_for()


def test_python_cache_reuses_only_identical_lock_and_runner(monkeypatch):
    images = {}
    built = []
    cached_id = "sha256:" + "c" * 64

    def docker(*args, **kwargs):
        if args[0] == "image":
            if args[-1] not in images:
                raise validation.RepositoryValidationError("Image missing.")
            return images[args[-1]]
        if args[0] == "build":
            root = Path(args[-1])
            assert {path.name for path in root.iterdir()} == {"Dockerfile", "requirements.lock"}
            assert "--only-binary :all: --require-hashes" in (root / "Dockerfile").read_text()
            built.append(args)
            key = args[-2].split(":")[1]
            images[args[-2]] = f"{key} {cached_id}"
        return ""

    monkeypatch.setattr(validation, "_docker", docker)
    base = "sha256:" + "a" * 64
    assert validation._locked_python_image(lock_for(), base) == cached_id
    assert validation._locked_python_image(lock_for(), base) == cached_id
    assert len(built) == 1
    validation._locked_python_image(lock_for("2.0"), base)
    validation._locked_python_image(lock_for(), "sha256:" + "b" * 64)
    assert len(built) == 3


def test_failed_lock_preparation_leaves_patch_review_available(tmp_path, monkeypatch):
    path = "test_app.py"
    (tmp_path / path).write_text("def test_example(): pass\n")
    change = ProposedChange(path=path, action="CREATE", intent="Add a test.")
    check = ValidationCheck(
        check_id="python.pytest", paths=[path], required=True, criteria_ids=["AC1"]
    )
    workspace = ApprovedWorkspace(
        tmp_path, "a" * 40, 1, "b" * 64, 1, (change,), (check,), b"flask>=3"
    )
    monkeypatch.setattr(validation, "_docker", lambda *args: "sha256:" + "a" * 64)
    checks = validation.validate_candidate(workspace)
    assert checks[0].status == "UNAVAILABLE" and "exact package versions" in checks[0].reason
    assert checks[1].status == "PASSED" and not validation.blocks_candidate(checks)
