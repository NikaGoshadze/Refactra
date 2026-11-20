"""Run approved pytest targets in a restricted local Docker container."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING

from refactra.refactoring_plan import ValidationCheck

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
    if check.check_id != "python.pytest" or not check.paths:
        raise RepositoryValidationError(
            "Validation requires an approved pytest check with explicit files."
        )
    root = workspace.root.resolve(strict=True)
    for path in check.paths:
        target = root.joinpath(*PurePosixPath(path).parts)
        if (
            target.suffix != ".py"
            or not target.is_relative_to(root)
            or target.resolve() != target
            or not target.is_file()
        ):
            raise RepositoryValidationError(
                "The approved pytest check must target staged Python files."
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
        "/app/.venv/bin/python",
        image_id,
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
    targets = [_targets(workspace, check) for check in workspace.validation_checks]
    image_id = _docker("image", "inspect", "--format", "{{.Id}}", IMAGE)
    return [
        _validate_check(workspace, check, files, image_id)
        for check, files in zip(workspace.validation_checks, targets, strict=True)
    ]
