"""Check approved text edits and run pytest in a restricted Docker container."""

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic, time
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from refactra.config import Settings
from refactra.diagnostics import workflow_log
from refactra.models import RunCommand
from refactra.python_dependency_cache import PythonLockError, pinned_python_requirements
from refactra.python_test_support import (
    NO_ROUTE_REQUESTS,
    PYTEST_ENTRY,
    declared_test_imports,
    python_unavailable_reason,
)
from refactra.refactoring_plan import (
    ASSET_CHECK,
    FLASK_CHECK,
    PATCH_CHECK,
    TYPESCRIPT_CHECK,
    ProposedChange,
    ValidationCheck,
    approved_file_operation,
    is_typescript_config,
    plan_edit_kind,
)

if TYPE_CHECKING:
    from refactra.repository_workspace import ApprovedWorkspace


CHECK_TIMEOUT = 180
MAX_OUTPUT_BYTES = 256 * 1024
IMAGE = "refactra-backend:local"
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CACHE_LABEL = "org.refactra.python-lock-cache"
USER = "10001:10001"
TMPFS = "rw,noexec,nosuid,nodev,size=64m"
SECURITY_OPTIONS = ["no-new-privileges", "seccomp=builtin"]
# A Docker runtime for check containers. Production uses gVisor ("runsc"): repository tests
# then run against gVisor's user-space kernel instead of the host kernel. Unset uses Docker's
# default runtime, which Docker Desktop requires.
RUNTIME_ENV = "REFACTRA_VALIDATION_RUNTIME"
_RUNTIME_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_PYTHON_CHECKS = {"python.pytest", FLASK_CHECK}
CONTAINER_LABEL = "org.refactra.validation"
# The runner kills its own command after this many seconds, so a container started by a
# worker that was killed still stops. It outlasts the worker's CHECK_TIMEOUT.
CONTAINER_DEADLINE = CHECK_TIMEOUT + 60
OPERATOR_COMMAND = "operator"


class RepositoryValidationError(ValueError):
    """Safe message about unavailable or invalid validation setup."""


class ValidationCleanupError(RepositoryValidationError):
    """The isolated runner could not be removed after use."""


def validation_runtime() -> str | None:
    """The configured check-container runtime, or None for Docker's default."""
    value = os.getenv(RUNTIME_ENV, "").strip()
    if not value:
        return None
    if _RUNTIME_NAME.fullmatch(value) is None:
        raise RepositoryValidationError(f"{RUNTIME_ENV} must be a Docker runtime name.")
    return value


def require_runtime_available() -> str | None:
    """Fail before claiming validation work if Docker lacks the configured runtime."""
    runtime = validation_runtime()
    if runtime is not None:
        try:
            available = json.loads(_docker("info", "--format", "{{json .Runtimes}}"))
        except (RepositoryValidationError, ValueError):
            raise RepositoryValidationError("Docker could not list its runtimes.") from None
        if not isinstance(available, dict) or runtime not in available:
            raise RepositoryValidationError(
                f"Docker has no {runtime} runtime; install it or unset {RUNTIME_ENV}."
            )
    return runtime


@dataclass(frozen=True)
class ContainerOwner:
    """Labels that let a replacement worker find runners left by an interrupted attempt."""

    instance: str
    command: str
    attempt: int


def validation_instance() -> str:
    """Identify this Refactra database so cleanup never touches another installation's runners."""
    url = make_url(Settings.from_env().database_url)
    return sha256(f"{url.host}:{url.port}/{url.database}".encode()).hexdigest()[:16]


def operator_owner() -> ContainerOwner:
    return ContainerOwner(validation_instance(), OPERATOR_COMMAND, 0)


def reconcile_validation_containers(session_factory, instance: str | None = None) -> int:
    """Remove this instance's runners whose attempt is no longer active or whose deadline passed.

    A runner stays while its command is still claimed with the same attempt and an unexpired
    lease, so an active attempt (including another worker's) keeps running. Containers without
    this instance's labels are never listed.
    """
    instance = instance or validation_instance()
    labels = ("command", "attempt", "deadline")
    fields = "|".join(f'{{{{.Label "{CONTAINER_LABEL}.{name}"}}}}' for name in labels)
    try:
        listing = _docker(
            "ps",
            "--all",
            "--filter",
            f"label={CONTAINER_LABEL}=1",
            "--filter",
            f"label={CONTAINER_LABEL}.instance={instance}",
            "--format",
            f"{{{{.ID}}}}|{fields}",
        )
    except RepositoryValidationError:
        workflow_log("validation_reconcile_skipped", warning=True, reason="docker_unavailable")
        return 0
    removed = 0
    with session_factory() as session:
        database_now = session.scalar(select(func.now()))
        for line in filter(None, listing.splitlines()):
            container, command, attempt, deadline = (line.split("|") + ["", "", ""])[:4]
            try:
                expired = int(deadline) <= time()
            except ValueError:
                expired = True
            active = not expired and command == OPERATOR_COMMAND
            if not expired and command != OPERATOR_COMMAND:
                try:
                    claim = session.get(RunCommand, UUID(command))
                except ValueError:
                    claim = None
                active = (
                    claim is not None
                    and claim.status == "CLAIMED"
                    and str(claim.attempts) == attempt
                    and claim.lease_until > database_now
                )
            if active:
                continue
            try:
                _docker("rm", "-f", container)
                removed += 1
            except RepositoryValidationError:
                workflow_log(
                    "validation_container_cleanup_failed", warning=True, container=container[:12]
                )
    if removed:
        workflow_log("validation_containers_reconciled", removed=removed)
    return removed


@dataclass(frozen=True)
class CandidateCheck:
    check_id: str
    paths: list[str]
    required: bool


@dataclass(frozen=True)
class ValidationResult(CandidateCheck):
    status: str
    exit_code: int | None
    duration_ms: int
    output: str
    reason: str | None
    image_id: str


def candidate_checks(
    changes: Sequence[ProposedChange], checks: Sequence[ValidationCheck]
) -> list[CandidateCheck]:
    if approved_file_operation(changes, checks) == "asset_replace":
        return [CandidateCheck(ASSET_CHECK, [changes[0].path], True)]
    # Keep every approved check with its approved paths and required flag; the built-in
    # patch check always runs last.
    result = [
        CandidateCheck(check.check_id, check.paths, check.required)
        for check in checks
        if check.check_id != PATCH_CHECK
    ]
    return [*result, CandidateCheck(PATCH_CHECK, [edit.path for edit in changes], True)]


def _pytest_file(path: str) -> bool:
    file = PurePosixPath(path)
    return file.suffix == ".py" and (file.stem.startswith("test_") or file.stem.endswith("_test"))


def blocks_candidate(checks: list[ValidationResult]) -> bool:
    # candidate_checks always puts the built-in verification last.
    if not checks or checks[-1].status != "PASSED":
        return True
    return any(
        check.required
        and (
            (check.exit_code is not None or not check.reason)
            if check.status == "UNAVAILABLE"
            else (check.status != "PASSED" or check.exit_code != 0 or check.reason is not None)
        )
        for check in checks
    )


def _docker(*args: str, timeout: int = 30) -> str:
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            check=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise RepositoryValidationError(
            f"Docker {args[0]} failed during validation setup or cleanup."
        ) from None


def _locked_python_image(lock: bytes, base_id: str) -> str:
    requirements = pinned_python_requirements(lock)
    if not _IMAGE_ID.fullmatch(base_id):
        raise RepositoryValidationError("The offline runner has an invalid image identity.")
    key = sha256(b"refactra-python-v1\0" + base_id.encode() + b"\0" + requirements).hexdigest()
    tag = f"refactra-python-validation:{key}"
    template = '{{ index .Config.Labels "' + _CACHE_LABEL + '" }} {{ .Id }}'

    def cached_image() -> str | None:
        try:
            identity = _docker("image", "inspect", "--format", template, tag)
        except RepositoryValidationError:
            return None
        digest, _, image = identity.partition(" ")
        return image if digest == key and _IMAGE_ID.fullmatch(image) else None

    if cached := cached_image():
        workflow_log("python_dependency_cache", status="hit", cache_key=key, image_id=cached)
        return cached

    base_tag = f"refactra-python-base:{base_id.removeprefix('sha256:')}"
    try:
        _docker("tag", base_id, base_tag)
        with TemporaryDirectory(prefix="refactra-python-cache-") as directory:
            context = Path(directory)
            (context / "requirements.lock").write_bytes(requirements)
            (context / "Dockerfile").write_text(
                f"FROM {base_tag}\n"
                f"LABEL {_CACHE_LABEL}={key}\n"
                "USER root\n"
                "COPY requirements.lock /tmp/refactra-requirements.lock\n"
                "RUN uv --no-config pip install --python /app/.venv/bin/python "
                "--target /locked-deps --no-deps --only-binary :all: --require-hashes "
                "--default-index https://pypi.org/simple -r /tmp/refactra-requirements.lock\n"
                f"USER {USER}\n",
                encoding="ascii",
            )
            _docker("build", "--pull=false", "--tag", tag, str(context), timeout=120)
    except (RepositoryValidationError, OSError):
        raise RepositoryValidationError("Could not prepare the locked Python wheels.") from None
    if image := cached_image():
        workflow_log("python_dependency_cache", status="prepared", cache_key=key, image_id=image)
        return image
    raise RepositoryValidationError("The prepared Python image could not be verified.")


def _targets(workspace: "ApprovedWorkspace", check: CandidateCheck) -> list[str]:
    if check.check_id == TYPESCRIPT_CHECK:
        if len(check.paths) != 1 or not is_typescript_config(check.paths[0]):
            raise RepositoryValidationError("TypeScript validation needs one tsconfig file.")
    elif check.check_id in _PYTHON_CHECKS:
        if not check.paths:
            reason = (
                "No Flask route test file was approved."
                if check.check_id == FLASK_CHECK
                else "Pytest needs at least one explicit test file."
            )
            raise RepositoryValidationError(reason)
        if check.check_id == FLASK_CHECK and not all(_pytest_file(path) for path in check.paths):
            raise RepositoryValidationError("Flask validation needs pytest test modules.")
    elif check.check_id == ASSET_CHECK:
        raise RepositoryValidationError("Asset validation needs one approved binary replacement.")
    else:
        raise RepositoryValidationError("Refactra has no runner for this check.")
    root = workspace.root.resolve(strict=True)
    for path in check.paths:
        target = root.joinpath(*PurePosixPath(path).parts)
        if (
            (check.check_id in _PYTHON_CHECKS and target.suffix != ".py")
            or not target.is_relative_to(root)
            or target.resolve() != target
            or not target.is_file()
        ):
            raise RepositoryValidationError(
                "The approved check must target staged Python files or a staged tsconfig."
            )
    return [f"./{path}" for path in dict.fromkeys(check.paths)]


def _inspect(container: str) -> dict:
    return json.loads(_docker("inspect", "--format", "{{json .}}", container))


def _verify_isolation(info: dict, tmpfs: str = TMPFS) -> None:
    limits = {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Privileged": False,
        "CapDrop": ["ALL"],
        "SecurityOpt": SECURITY_OPTIONS,
        "NanoCpus": 2_000_000_000,
        "Memory": 2 * 1024**3,
        "MemorySwap": 2 * 1024**3,
        "PidsLimit": 128,
        "Tmpfs": {"/tmp": tmpfs},
        "Init": True,
        "PidMode": "",
        "PortBindings": {},
    }
    host = info["HostConfig"]
    mounts = [(item["Type"], item["Destination"], item["RW"]) for item in info["Mounts"]]
    runtime = validation_runtime()
    if (
        any(host.get(key) != value for key, value in limits.items())
        # A configured runtime must really be in use, never a silent fallback to runc.
        or (runtime is not None and host.get("Runtime") != runtime)
        or any(host.get(key) for key in ("CapAdd", "Devices", "DeviceRequests"))
        or info["Config"]["User"] != USER
        or host["LogConfig"]["Type"] != "none"
        or mounts != [("bind", "/workspace", False)]
    ):
        raise RepositoryValidationError("Docker did not apply the required validation isolation.")


def _capture(container: str) -> tuple[bytes, str | None]:
    deadline = monotonic() + CHECK_TIMEOUT
    with (
        subprocess.Popen(
            ["docker", "start", "--attach", container],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process,
        ThreadPoolExecutor(max_workers=1) as reader,
    ):
        output = reader.submit(process.stdout.read, MAX_OUTPUT_BYTES + 1)
        try:
            data = output.result(timeout=CHECK_TIMEOUT)
            if len(data) > MAX_OUTPUT_BYTES:
                return data[:MAX_OUTPUT_BYTES], "output_limit"
            process.wait(timeout=max(0, deadline - monotonic()))
            return data, None
        except (TimeoutError, subprocess.TimeoutExpired):
            return b"", "timeout"
        finally:
            if process.poll() is None:
                process.kill()


def _validate_check(
    workspace: "ApprovedWorkspace",
    check: CandidateCheck,
    targets: list[str],
    image_id: str,
    owner: ContainerOwner | None = None,
) -> ValidationResult:
    owner = owner or operator_owner()
    entrypoint = "/app/.venv/bin/python"
    workdir, tmpfs = "/workspace", TMPFS
    environment = ["--env", "HOME=/tmp", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1"]
    declared = (
        declared_test_imports(workspace.root, check.paths)
        if check.check_id in _PYTHON_CHECKS
        else {}
    )
    flask_args = []
    if check.check_id == FLASK_CHECK:
        flask_args = ["--refactra-flask"] + [
            f"--refactra-route-file={edit.path}"
            for edit in workspace.changes
            if edit.path.endswith(".py")
            and edit.action != "DELETE"
            and edit.path not in check.paths
        ]
    arguments = [
        "-I",
        "-B",
        "-c",
        PYTEST_ENTRY,
        json.dumps(declared),
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        "/dev/null",
        "--noconftest",
        "--basetemp=/tmp/pytest",
        *flask_args,
        "--",
        *targets,
    ]
    if check.check_id == TYPESCRIPT_CHECK:
        entrypoint = "/usr/local/bin/node"
        arguments = [
            "/node_modules/typescript/bin/tsc",
            "--noEmit",
            "--pretty",
            "false",
            "--incremental",
            "false",
            # TypeScript 6 rejects options it deprecated (for example target ES5 or baseUrl).
            # A repository's older tsconfig is not a candidate defect, so only type errors fail.
            "--ignoreDeprecations",
            "6.0",
            "--project",
            targets[0],
        ]
    labels = {
        CONTAINER_LABEL: "1",
        f"{CONTAINER_LABEL}.instance": owner.instance,
        f"{CONTAINER_LABEL}.command": owner.command,
        f"{CONTAINER_LABEL}.attempt": str(owner.attempt),
        # Reconciliation may remove the runner after its own deadline plus a margin.
        f"{CONTAINER_LABEL}.deadline": str(int(time()) + CONTAINER_DEADLINE + 30),
    }
    runtime = validation_runtime()
    container = _docker(
        "create",
        "--pull",
        "never",
        *(("--runtime", runtime) if runtime else ()),
        "--init",
        *(
            argument
            for name, value in labels.items()
            for argument in ("--label", f"{name}={value}")
        ),
        "--network",
        "none",
        "--read-only",
        "--user",
        USER,
        "--cap-drop",
        "ALL",
        "--security-opt",
        SECURITY_OPTIONS[0],
        "--security-opt",
        SECURITY_OPTIONS[1],
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--memory-swap",
        "2g",
        "--pids-limit",
        "128",
        "--tmpfs",
        f"/tmp:{tmpfs}",
        "--log-driver",
        "none",
        "--mount",
        f"type=bind,source={workspace.root.resolve()},target=/workspace,readonly",
        "--workdir",
        workdir,
        *environment,
        "--entrypoint",
        "timeout",
        image_id,
        "-s",
        "KILL",
        str(CONTAINER_DEADLINE),
        entrypoint,
        *arguments,
    )
    try:
        _verify_isolation(_inspect(container), tmpfs)
        start = monotonic()
        output, reason = _capture(container)
        duration_ms = round((monotonic() - start) * 1000)
        exit_code = None
        if reason is None:
            state = _inspect(container)["State"]
            if state["Status"] != "exited" or state["Error"]:
                raise RepositoryValidationError("The validation container did not finish normally.")
            exit_code = state["ExitCode"]
        text = output.decode("utf-8", errors="replace")
        unavailable = None
        if check.check_id == FLASK_CHECK and exit_code == NO_ROUTE_REQUESTS:
            unavailable = "The tests did not send a request to a changed Flask route."
        elif check.check_id in _PYTHON_CHECKS:
            unavailable = python_unavailable_reason(exit_code, text, declared)
        if unavailable:
            return ValidationResult(
                check.check_id,
                check.paths,
                check.required,
                "UNAVAILABLE",
                None,
                duration_ms,
                text,
                unavailable,
                image_id,
            )
        return ValidationResult(
            check.check_id,
            check.paths,
            check.required,
            "PASSED" if exit_code == 0 else "FAILED",
            exit_code,
            duration_ms,
            text,
            reason,
            image_id,
        )
    finally:
        try:
            _docker("rm", "-f", container)
        except RepositoryValidationError as error:
            raise ValidationCleanupError(str(error)) from error


def validate_candidate(
    workspace: "ApprovedWorkspace", owner: ContainerOwner | None = None
) -> list[ValidationResult]:
    operation = approved_file_operation(workspace.changes, workspace.validation_checks)
    results = []
    image_id = ""
    image_error = None
    python_image = ""
    python_image_error = None
    for check in candidate_checks(workspace.changes, workspace.validation_checks):
        if check.check_id == PATCH_CHECK or operation == "asset_replace":
            results.append(_validate_text_patch(workspace, check))
            continue
        try:
            targets = _targets(workspace, check)
            if image_error:
                raise RepositoryValidationError(image_error)
            if not image_id:
                try:
                    image_id = _docker("image", "inspect", "--format", "{{.Id}}", IMAGE)
                except RepositoryValidationError:
                    image_error = "The offline Docker validation image is unavailable."
                    raise RepositoryValidationError(image_error) from None
            runner = image_id
            if check.check_id in _PYTHON_CHECKS and workspace.python_lock is not None:
                if python_image_error:
                    raise RepositoryValidationError(python_image_error)
                if not python_image:
                    try:
                        python_image = _locked_python_image(workspace.python_lock, image_id)
                    except (PythonLockError, RepositoryValidationError) as error:
                        python_image_error = str(error)
                        raise RepositoryValidationError(python_image_error) from None
                runner = python_image
            results.append(_validate_check(workspace, check, targets, runner, owner))
        except ValidationCleanupError:
            raise
        except RepositoryValidationError as error:
            results.append(
                ValidationResult(
                    check.check_id,
                    check.paths,
                    check.required,
                    "UNAVAILABLE",
                    None,
                    0,
                    "",
                    str(error),
                    "",
                )
            )
    return results


def _validate_text_patch(workspace: "ApprovedWorkspace", check: CandidateCheck) -> ValidationResult:
    started = monotonic()
    expected = {edit.path: edit.action for edit in workspace.changes}
    operation = approved_file_operation(workspace.changes, workspace.validation_checks)
    if check.check_id == ASSET_CHECK and operation != "asset_replace":
        raise RepositoryValidationError("The candidate is outside the approved asset scope.")
    if set(check.paths) != set(expected):
        raise RepositoryValidationError("Patch validation must cover the approved paths.")
    error = None
    for path in check.paths:
        target = workspace.root.joinpath(*PurePosixPath(path).parts)
        try:
            if expected[path] == "DELETE":
                if target.exists() or target.is_symlink():
                    raise ValueError
                continue
            if target.is_symlink() or not target.is_file():
                raise ValueError
            if operation == "asset_replace":
                if not target.stat().st_size:
                    raise ValueError
                continue
            text = target.read_text(encoding="utf-8")
            if target.suffix == ".json":
                json.loads(text)
            elif target.suffix == ".toml":
                tomllib.loads(text)
        except (ValueError, OSError):
            error = f"Could not verify the approved change to {path}."
            break
    message = "Approved paths and applicable JSON/TOML syntax verified."
    if any(plan_edit_kind(path, action) == "env_delete" for path, action in expected.items()):
        message = (
            "Verified environment file removal. Rotate affected credentials before publication. "
            "GitHub's diff can display deleted lines, and Git history still contains the original."
        )
    elif operation == "asset_replace":
        message = "Verified the approved binary replacement."
    return ValidationResult(
        check.check_id,
        check.paths,
        check.required,
        "FAILED" if error else "PASSED",
        1 if error else 0,
        int((monotonic() - started) * 1000),
        error or message,
        error,
        "builtin",
    )
