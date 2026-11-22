"""Identify declared Python dependencies and report collection failures offline."""

import json
import re
import tomllib
from pathlib import Path, PurePosixPath

_MANIFEST_LIMIT = 64 * 1024
_MAX_DECLARED_IMPORTS = 128
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?=\s*(?:\[|[<>=!~;@#]|$))")
_ALIASES = {"python-dotenv": "dotenv", "pyyaml": "yaml", "pillow": "PIL", "scikit-learn": "sklearn"}
_MISSING_PREFIX = "REFACTRA_MISSING_PYTHON:"
_MISSING_EXIT = 76

PYTEST_ENTRY = f"""
import importlib.util
import json
import sys

sys.path.insert(0, '/locked-deps')
import pytest

declared = json.loads(sys.argv.pop(1))
missing = set()
for name in declared:
    try:
        if importlib.util.find_spec(name) is None:
            missing.add(name)
    except (ImportError, ValueError):
        pass

class Collection:
    failed = 0
    missing_modules = []

    def pytest_collectreport(self, report):
        if report.failed:
            self.failed += 1

    def pytest_exception_interact(self, node, call, report):
        if isinstance(report, pytest.CollectReport):
            error = call.excinfo.value
            if type(error) is pytest.Collector.CollectError:
                error = error.__cause__
            if type(error) is ModuleNotFoundError and error.name in missing:
                self.missing_modules.append(error.name)

collection = Collection()
sys.path.insert(0, '/workspace')
code = pytest.main(sys.argv[1:], plugins=[collection])
if code == pytest.ExitCode.INTERRUPTED and collection.failed:
    if collection.failed == len(collection.missing_modules):
        print({_MISSING_PREFIX!r} + json.dumps(sorted(set(collection.missing_modules))), flush=True)
        raise SystemExit({_MISSING_EXIT})
raise SystemExit(code)
"""


def _manifest(path: Path) -> str:
    try:
        if not path.is_file() or path.resolve() != path:
            return ""
        with path.open("rb") as stream:
            data = stream.read(_MANIFEST_LIMIT + 1)
        return data.decode("utf-8") if len(data) <= _MANIFEST_LIMIT else ""
    except (OSError, UnicodeError):
        return ""


def _project_requirements(path: Path) -> list[str]:
    try:
        document = tomllib.loads(_manifest(path))
    except ValueError:
        return []
    project = document.get("project", {})
    if not isinstance(project, dict):
        return []
    groups = [project.get("dependencies")]
    for table in (project.get("optional-dependencies"), document.get("dependency-groups")):
        if isinstance(table, dict):
            groups.extend(table.values())
    return [
        value
        for group in groups
        if isinstance(group, list)
        for value in group
        if isinstance(value, str)
    ]


def declared_test_imports(root: Path, targets: list[str]) -> dict[str, str]:
    root = root.resolve(strict=True)
    directories = {root / parent for name in targets for parent in PurePosixPath(name).parents}
    declared = {}
    for directory in sorted(directories):
        requirements = _manifest(directory / "requirements.txt").splitlines()
        requirements += _project_requirements(directory / "pyproject.toml")
        for requirement in requirements:
            if match := _REQUIREMENT.match(requirement):
                distribution = re.sub(r"[-_.]+", "-", match[1]).lower()
                name = _ALIASES.get(distribution, distribution.replace("-", "_"))
                if name.isidentifier() and not any(
                    (local / f"{name}.py").is_file() or (local / name).is_dir()
                    for local in directories
                ):
                    declared[name] = distribution
                if len(declared) == _MAX_DECLARED_IMPORTS:
                    return declared
    return declared


def python_unavailable_reason(
    exit_code: int | None, output: str, declared: dict[str, str]
) -> str | None:
    if exit_code == 5:  # pytest.ExitCode.NO_TESTS_COLLECTED
        return "Pytest collected no tests from the approved files."
    if exit_code != _MISSING_EXIT:
        return None
    line = output.rstrip().rpartition("\n")[2]
    if not line.startswith(_MISSING_PREFIX):
        return None
    try:
        modules = json.loads(line[len(_MISSING_PREFIX) :])
    except ValueError:
        return None
    if (
        not isinstance(modules, list)
        or not modules
        or any(not isinstance(name, str) or name not in declared for name in modules)
    ):
        return None
    dependencies = ", ".join(sorted({declared[name] for name in modules}))
    return f"Pytest could not collect tests because the offline runner lacks: {dependencies}."
