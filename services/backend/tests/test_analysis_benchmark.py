"""Validate handwritten benchmark inputs/oracles; never invoke a model."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "analysis"
INPUTS = tomllib.loads((ROOT / "cases.toml").read_text(encoding="utf-8"))
GRADING = tomllib.loads((ROOT / "grading.toml").read_text(encoding="utf-8"))


def test_cases_have_complete_separate_grading_and_bounded_inputs():
    cases = INPUTS["cases"]
    assert INPUTS["schema_version"] == GRADING["schema_version"] == 1
    assert len(cases) == len({case["id"] for case in cases}) == 5
    assert {case["id"] for case in cases} == set(GRADING["cases"])
    assert sum(GRADING["weights"].values()) == 100
    assert len(GRADING["critical_failures"]) == 3
    for case in cases:
        assert set(case) == {"id", "language", "task", "files"}
        assert case["task"].strip() and 1 <= len(case["files"]) <= 8
        assert sum(len(source.encode()) for source in case["files"].values()) < 16_000
        for path, source in case["files"].items():
            assert not Path(path).is_absolute() and ".." not in Path(path).parts
            assert source and not source.startswith("\n")
        criteria = GRADING["cases"][case["id"]]
        assert set(criteria) == set(GRADING["weights"])
        assert all(len(checks) == 2 and all(checks) for checks in criteria.values())


@pytest.mark.parametrize(
    "case",
    [case for case in INPUTS["cases"] if any(path.startswith("tests/") for path in case["files"])],
    ids=lambda case: case["id"],
)
def test_python_fixture_baselines_pass(case, tmp_path):
    # Only trusted, handwritten fixture code is executed, not model/repository output.
    for relative, source in case["files"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path / "src"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
