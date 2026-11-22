"""Check approved text edits and run pytest in a restricted Docker container."""

import json
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING

from refactra.refactoring_plan import (
    ASSET_CHECK,
    PATCH_CHECK,
    TYPESCRIPT_CHECK,
    ValidationCheck,
    approved_file_operation,
    is_env_cleanup,
    is_typescript_config,
)

if TYPE_CHECKING:
    from refactra.repository_workspace import ApprovedWorkspace


CHECK_TIMEOUT = 180
MAX_OUTPUT_BYTES = 256 * 1024
IMAGE = "refactra-backend:local"
USER = "10001:10001"
TMPFS = "rw,noexec,nosuid,nodev,size=64m"
SECURITY_OPTIONS = ["no-new-privileges", "seccomp=builtin"]
PYTEST_ENTRY = (
    "import sys\n"
    "import pytest\n"
    "sys.path.insert(0, '/workspace')\n"
    "raise SystemExit(pytest.main(sys.argv[1:]))\n"
)


class RepositoryValidationError(ValueError):
    """Safe message about unavailable or invalid validation setup."""


@dataclass(frozen=True)
class ValidationResult:
    check_id: str
    paths: list[str]
    required: bool
    status: str
    exit_code: int | None
    duration_ms: int
    output: str
    reason: str | None
    image_id: str


def _docker(*args: str) -> str:
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            check=True,
            timeout=30,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise RepositoryValidationError(
            f"Docker {args[0]} failed during validation setup or cleanup."
        ) from None


def _targets(workspace: "ApprovedWorkspace", check: ValidationCheck) -> list[str]:
    if check.check_id == TYPESCRIPT_CHECK:
        if len(check.paths) != 1 or not is_typescript_config(check.paths[0]):
            raise RepositoryValidationError("TypeScript validation needs one tsconfig file.")
    elif check.check_id != "python.pytest" or not check.paths:
        raise RepositoryValidationError(
            "Validation requires an approved pytest check with explicit files."
        )
    root = workspace.root.resolve(strict=True)
    for path in check.paths:
        target = root.joinpath(*PurePosixPath(path).parts)
        if (
            (check.check_id == "python.pytest" and target.suffix != ".py")
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


def _verify_isolation(info: dict) -> None:
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
        "Tmpfs": {"/tmp": TMPFS},
        "PidMode": "",
        "PortBindings": {},
    }
    host = info["HostConfig"]
    mounts = [(item["Type"], item["Destination"], item["RW"]) for item in info["Mounts"]]
    if (
        any(host.get(key) != value for key, value in limits.items())
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
    workspace: "ApprovedWorkspace", check: ValidationCheck, targets: list[str], image_id: str
) -> ValidationResult:
    entrypoint = "/app/.venv/bin/python"
    arguments = [
        "-I",
        "-B",
        "-c",
        PYTEST_ENTRY,
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        "/dev/null",
        "--noconftest",
        "--basetemp=/tmp/pytest",
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
            "--project",
            targets[0],
        ]
    container = _docker(
        "create",
        "--pull",
        "never",
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
        f"/tmp:{TMPFS}",
        "--log-driver",
        "none",
        "--mount",
        f"type=bind,source={workspace.root.resolve()},target=/workspace,readonly",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp",
        "--env",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--entrypoint",
        entrypoint,
        image_id,
        *arguments,
    )
    try:
        _verify_isolation(_inspect(container))
        start = monotonic()
        output, reason = _capture(container)
        duration_ms = round((monotonic() - start) * 1000)
        exit_code = None
        if reason is None:
            state = _inspect(container)["State"]
            if state["Status"] != "exited" or state["Error"]:
                raise RepositoryValidationError("The validation container did not finish normally.")
            exit_code = state["ExitCode"]
        return ValidationResult(
            check.check_id,
            check.paths,
            check.required,
            "PASSED" if exit_code == 0 else "FAILED",
            exit_code,
            duration_ms,
            output.decode("utf-8", errors="replace"),
            reason,
            image_id,
        )
    finally:
        _docker("rm", "-f", container)


def validate_candidate(workspace: "ApprovedWorkspace") -> list[ValidationResult]:
    if not workspace.validation_checks:
        raise RepositoryValidationError("The approved plan has no validation checks.")
    targets = [
        [] if check.check_id in {PATCH_CHECK, ASSET_CHECK} else _targets(workspace, check)
        for check in workspace.validation_checks
    ]
    image_id = _docker("image", "inspect", "--format", "{{.Id}}", IMAGE) if any(targets) else ""
    return [
        _validate_check(workspace, check, files, image_id)
        if files
        else _validate_text_patch(workspace, check)
        for check, files in zip(workspace.validation_checks, targets, strict=True)
    ]


def _validate_text_patch(
    workspace: "ApprovedWorkspace", check: ValidationCheck
) -> ValidationResult:
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
    if is_env_cleanup(workspace.changes, workspace.validation_checks):
        message = (
            "Verified that .env was removed. Rotate affected credentials before publication. "
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
