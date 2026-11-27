import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from refactra import repository_validation as validation
from refactra.models import Run, RunCommand
from refactra.python_dependency_cache import PythonLockError, pinned_python_requirements
from refactra.python_test_support import (
    PYTEST_ENTRY,
    declared_test_imports,
    python_unavailable_reason,
)
from refactra.refactoring_plan import FLASK_CHECK, ProposedChange, ValidationCheck
from refactra.repository_workspace import ApprovedWorkspace


def test_flask_check_never_collects_application_modules_as_tests(tmp_path, monkeypatch):
    check = ValidationCheck(
        check_id=FLASK_CHECK,
        paths=["web.py"],
        required=True,
        criteria_ids=["AC1"],
    )
    (tmp_path / "web.py").touch()
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 0, (), (check,))
    monkeypatch.setattr(validation, "_docker", lambda *args: pytest.fail("Docker was called"))
    route, baseline = validation.validate_candidate(workspace)
    assert route.status == "UNAVAILABLE" and "pytest test modules" in route.reason
    assert not validation.blocks_candidate([route, baseline])


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


def runner_readable(path):
    """Let the non-root runner read a test workspace; pytest creates it 0700 on Linux."""
    path.chmod(0o755)


@pytest.mark.skipif(os.getenv("REFACTRA_TEST_PYTHON_DOCKER") != "1", reason="Requires Docker.")
def test_cached_web_dependencies_run_inside_the_offline_container(tmp_path):
    runner_readable(tmp_path)
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
    runner_readable(tmp_path)
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


def _scope(checks) -> list[tuple]:
    return [(check.check_id, check.paths, check.required) for check in checks]


def test_approved_checks_keep_their_scope_and_the_patch_check_runs_last():
    changes = [ProposedChange(path="src/cart.py", action="MODIFY", intent="Change the cart.")]
    approved = [
        ValidationCheck(
            check_id="python.pytest",
            paths=["tests/test_cart.py"],
            required=True,
            criteria_ids=["AC1"],
        ),
        ValidationCheck(
            check_id="typescript.tsc",
            paths=["tsconfig.json"],
            required=False,
            criteria_ids=["AC1"],
        ),
    ]
    assert _scope(validation.candidate_checks(changes, approved)) == [
        ("python.pytest", ["tests/test_cart.py"], True),
        ("typescript.tsc", ["tsconfig.json"], False),
        ("repository.patch", ["src/cart.py"], True),
    ]


def test_a_check_without_a_runner_is_reported_unavailable_without_docker(tmp_path, monkeypatch):
    # Go support was removed; older plans may still name go.test.
    change = ProposedChange(path="main.go", action="MODIFY", intent="Change the tool.")
    check = ValidationCheck(
        check_id="go.test", paths=["main.go"], required=True, criteria_ids=["AC1"]
    )
    (tmp_path / "main.go").write_text("package main\n")
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 1, (change,), (check,))
    monkeypatch.setattr(validation, "_docker", lambda *args, **kwargs: pytest.fail("Docker"))
    unavailable, baseline = validation.validate_candidate(workspace)
    assert (unavailable.check_id, unavailable.status) == ("go.test", "UNAVAILABLE")
    assert unavailable.reason == "Refactra has no runner for this check."
    assert baseline.check_id == "repository.patch"


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


@pytest.mark.parametrize("runtime", [None, "runsc"])
def test_a_configured_runtime_is_requested_and_verified(tmp_path, monkeypatch, runtime):
    calls = []
    inspected = {"runtime": "runc"}

    def docker(*args, **_kwargs):
        calls.append(args)
        if args[0] == "create":
            return "runner-id"
        if args[0] == "inspect":
            return json.dumps({"HostConfig": {"Runtime": inspected["runtime"]}, "Mounts": []})
        return ""

    if runtime:
        monkeypatch.setenv(validation.RUNTIME_ENV, runtime)
    else:
        monkeypatch.delenv(validation.RUNTIME_ENV, raising=False)
    monkeypatch.setattr(validation, "_docker", docker)
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 0, ())
    check = validation.CandidateCheck("typescript.tsc", ["tsconfig.json"], True)
    owner = validation.ContainerOwner("instance-a", "command-1", 1)
    with pytest.raises(validation.RepositoryValidationError, match="isolation"):
        validation._validate_check(workspace, check, ["./tsconfig.json"], "sha256:image", owner)
    create = calls[0]
    if runtime:
        assert create[create.index("--runtime") + 1] == "runsc"
    else:
        assert "--runtime" not in create
    # A container that ended up on another runtime is rejected before it runs.
    if runtime:
        info = {"HostConfig": {"Runtime": "runc"}}
        with pytest.raises(validation.RepositoryValidationError, match="isolation"):
            validation._verify_isolation({**info, "Mounts": [], "Config": {"User": ""}})


def test_the_runtime_setting_is_validated_and_checked_against_docker(monkeypatch):
    monkeypatch.setenv(validation.RUNTIME_ENV, "runsc; rm -rf /")
    with pytest.raises(validation.RepositoryValidationError, match="runtime name"):
        validation.validation_runtime()
    monkeypatch.setenv(validation.RUNTIME_ENV, "runsc")
    runtimes = {"runc": {}, "io.containerd.runc.v2": {}}
    monkeypatch.setattr(validation, "_docker", lambda *args, **_kw: json.dumps(runtimes))
    with pytest.raises(validation.RepositoryValidationError, match="no runsc runtime"):
        validation.require_runtime_available()
    runtimes["runsc"] = {"path": "/usr/local/bin/runsc"}
    assert validation.require_runtime_available() == "runsc"
    monkeypatch.delenv(validation.RUNTIME_ENV)
    assert validation.require_runtime_available() is None


def test_runners_are_labeled_for_reconciliation_and_stop_themselves(tmp_path, monkeypatch):
    calls = []

    def docker(*args, **_kwargs):
        calls.append(args)
        if args[0] == "create":
            return "runner-id"
        if args[0] == "inspect":
            return json.dumps({"HostConfig": {"NetworkMode": "bridge"}, "Mounts": []})
        return ""

    monkeypatch.setattr(validation, "_docker", docker)
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 0, ())
    check = validation.CandidateCheck("typescript.tsc", ["tsconfig.json"], True)
    owner = validation.ContainerOwner("instance-a", "command-1", 2)
    with pytest.raises(validation.RepositoryValidationError, match="isolation"):
        validation._validate_check(workspace, check, ["./tsconfig.json"], "sha256:image", owner)
    create = calls[0]
    assert create[0] == "create" and "--init" in create
    labels = {create[index + 1] for index, item in enumerate(create) if item == "--label"}
    prefix = validation.CONTAINER_LABEL
    assert {
        f"{prefix}=1",
        f"{prefix}.instance=instance-a",
        f"{prefix}.command=command-1",
        f"{prefix}.attempt=2",
    } <= labels
    deadline = next(int(item.split("=")[1]) for item in labels if ".deadline=" in item)
    assert deadline > time.time() + validation.CONTAINER_DEADLINE
    image = create.index("sha256:image")
    assert create[-4:-2] == ("--ignoreDeprecations", "6.0")
    assert create[image - 2 : image] == ("--entrypoint", "timeout")
    assert create[image + 1 : image + 5] == (
        "-s",
        "KILL",
        str(validation.CONTAINER_DEADLINE),
        "/usr/local/bin/node",
    )
    assert calls[-1] == ("rm", "-f", "runner-id")


def _command(session, *, status, attempts, lease_seconds):
    run = Run(task_text="Reconcile runners")
    session.add(run)
    session.flush()
    now = session.scalar(select(func.now()))
    command = RunCommand(
        run_id=run.id,
        kind="VALIDATE_CANDIDATE",
        dedupe_key=f"validation:{run.id}",
        status=status,
        attempts=attempts,
        claimed_by="worker" if status == "CLAIMED" else None,
        lease_until=now + timedelta(seconds=lease_seconds) if status == "CLAIMED" else None,
    )
    session.add(command)
    session.flush()
    return str(command.id)


def test_reconciliation_removes_only_inactive_runners_of_this_instance(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        active = _command(session, status="CLAIMED", attempts=2, lease_seconds=60)
        expired_lease = _command(session, status="CLAIMED", attempts=1, lease_seconds=-5)
        finished = _command(session, status="DONE", attempts=1, lease_seconds=0)
    future, past = int(time.time()) + 600, int(time.time()) - 5
    listing = "\n".join(
        [
            f"keep-active|{active}|2|{future}",
            f"keep-operator|operator|0|{future}",
            f"old-attempt|{active}|1|{future}",
            f"lease-expired|{expired_lease}|1|{future}",
            f"finished|{finished}|1|{future}",
            f"past-deadline|{active}|2|{past}",
            "unlabeled-deadline|not-a-uuid|1|",
        ]
    )
    calls = []

    def docker(*args, **_kwargs):
        calls.append(args)
        return listing if args[0] == "ps" else ""

    monkeypatch.setattr(validation, "_docker", docker)
    assert validation.reconcile_validation_containers(session_factory, "instance-a") == 5
    assert f"label={validation.CONTAINER_LABEL}.instance=instance-a" in calls[0]
    removed = {args[2] for args in calls if args[:2] == ("rm", "-f")}
    assert removed == {
        "old-attempt",
        "lease-expired",
        "finished",
        "past-deadline",
        "unlabeled-deadline",
    }


def test_reconciliation_skips_quietly_without_docker(session_factory, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise validation.RepositoryValidationError("Docker is unavailable.")

    monkeypatch.setattr(validation, "_docker", unavailable)
    assert validation.reconcile_validation_containers(session_factory, "instance-a") == 0


@pytest.mark.skipif(os.getenv("REFACTRA_TEST_PYTHON_DOCKER") != "1", reason="Requires Docker.")
def test_reconciliation_removes_real_orphans_and_leaves_other_containers(session_factory):
    with session_factory.begin() as session:
        finished = _command(session, status="DONE", attempts=1, lease_seconds=0)
        active = _command(session, status="CLAIMED", attempts=1, lease_seconds=120)
    instance = f"test-{time.time_ns()}"
    deadline = str(int(time.time()) + 600)

    def start(name, command, attempt="1", labeled=True):
        labels = []
        if labeled:
            prefix = validation.CONTAINER_LABEL
            for key, value in {
                prefix: "1",
                f"{prefix}.instance": instance,
                f"{prefix}.command": command,
                f"{prefix}.attempt": attempt,
                f"{prefix}.deadline": deadline,
            }.items():
                labels += ["--label", f"{key}={value}"]
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--init",
                "--network",
                "none",
                *labels,
                "--entrypoint",
                "sleep",
                validation.IMAGE,
                "300",
            ],
            check=True,
            capture_output=True,
        )

    names = {
        "orphan": f"{instance}-orphan",
        "active": f"{instance}-active",
        "unrelated": f"{instance}-unrelated",
    }
    try:
        start(names["orphan"], finished)
        start(names["active"], active)
        start(names["unrelated"], finished, labeled=False)
        assert validation.reconcile_validation_containers(session_factory, instance) == 1
        running = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={instance}", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert sorted(running) == sorted([names["active"], names["unrelated"]])
    finally:
        subprocess.run(["docker", "rm", "-f", *names.values()], capture_output=True)


@pytest.mark.skipif(
    os.getenv("REFACTRA_TEST_TYPESCRIPT_DOCKER") != "1",
    reason="Requires a rebuilt local backend image and Docker.",
)
@pytest.mark.parametrize(
    ("source", "expected"), [("number = 2", "PASSED"), ("number = 'two'", "FAILED")]
)
def test_typescript_checks_ignore_deprecated_tsconfig_options(tmp_path, source, expected):
    runner_readable(tmp_path)
    # An older tsconfig that TypeScript 6 would otherwise reject before checking any code.
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions": {"target": "ES5", "moduleResolution": "node", "baseUrl": ".",'
        ' "strict": true}, "include": ["cart.ts"]}'
    )
    (tmp_path / "cart.ts").write_text(f"export const total: {source};\n")
    change = ProposedChange(path="cart.ts", action="MODIFY", intent="Update the total.")
    check = ValidationCheck(
        check_id="typescript.tsc", paths=["tsconfig.json"], required=True, criteria_ids=["AC1"]
    )
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 2, (change,), (check,))
    result, baseline = validation.validate_candidate(workspace)
    assert result.status == expected, result.output
    assert "TS5107" not in result.output and "TS5101" not in result.output
    assert baseline.status == "PASSED"
