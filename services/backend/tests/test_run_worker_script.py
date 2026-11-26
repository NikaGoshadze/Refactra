"""Run scripts/run-worker.ps1 with a fake uv to check its stop, restart and backoff behaviour."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# The repository root is a few levels up; the backend image ships without scripts/.
SCRIPT = next(
    (
        parent / "scripts" / "run-worker.ps1"
        for parent in Path(__file__).resolve().parents
        if (parent / "scripts" / "run-worker.ps1").is_file()
    ),
    Path("scripts/run-worker.ps1"),
)
PWSH = shutil.which("pwsh")

# Records each call's arguments and exits with the next scripted code.
FAKE_UV = """
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_UV_STATE"])
codes = json.loads(state.read_text())
with Path(os.environ["FAKE_UV_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
state.write_text(json.dumps(codes[1:]))
sys.exit(codes[0])
"""


def plain(output):
    """Drop colour codes and join PowerShell's wrapped error lines into one string."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    return " ".join(re.sub(r"\s*\|\s+", " ", text).split())


@pytest.fixture
def run_worker(tmp_path):
    if PWSH is None or not SCRIPT.is_file():
        reason = "Requires PowerShell 7 (pwsh) and the repository scripts."
        if os.getenv("GITHUB_ACTIONS"):
            pytest.fail(reason)  # CI runners have pwsh; never skip there silently.
        pytest.skip(reason)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fake_uv.py"
    fake.write_text(FAKE_UV, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "uv.cmd").write_text(
            f'@"{sys.executable}" "{fake}" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding="utf-8"
        )
    else:
        uv = bin_dir / "uv"
        uv.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        uv.chmod(0o755)

    def run(codes, *args):
        state, log = tmp_path / "codes.json", tmp_path / "calls.jsonl"
        state.write_text(json.dumps(codes))
        log.write_text("")
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_UV_STATE": str(state),
            "FAKE_UV_LOG": str(log),
            "REFACTRA_WORKER_RESTART_SECONDS": "1",
            "NO_COLOR": "1",
        }
        completed = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return completed, plain(completed.stdout + completed.stderr), calls

    return run


def test_a_clean_exit_stops_the_loop_and_passes_arguments_through(run_worker):
    completed, output, calls = run_worker([0], "--once", "--repository-workflow")
    assert completed.returncode == 0, output
    assert "Worker stopped cleanly." in output
    (arguments,) = calls
    project = arguments[arguments.index("--project") + 1]
    env_file = arguments[arguments.index("--env-file") + 1]
    # uv reads backslashes in --env-file as escapes (#108), so paths must use forward slashes.
    assert "\\" not in project + env_file
    assert project.endswith("/services/backend") and env_file.endswith("/.env")
    assert arguments[0] == "run"
    assert arguments[-5:] == ["python", "-m", "refactra.worker", "--once", "--repository-workflow"]


def test_unexpected_exits_restart_with_a_doubling_delay(run_worker):
    completed, output, calls = run_worker([1, 3, 0])
    assert completed.returncode == 0, output
    assert len(calls) == 3
    first = output.index("exited with code 1. Restarting in 1 seconds")
    second = output.index("exited with code 3. Restarting in 2 seconds")
    assert first < second and "Worker stopped cleanly." in output


def test_a_configuration_error_stops_without_restarting(run_worker):
    completed, output, calls = run_worker([2, 0])
    assert completed.returncode != 0
    assert len(calls) == 1
    assert "not restarting" in output and "Restarting in" not in output
