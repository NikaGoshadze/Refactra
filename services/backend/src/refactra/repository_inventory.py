"""Deterministic snapshot inventory. Repository declarations are data, never commands."""

import json
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from refactra.repository_snapshot import SourceSnapshot

MAX_MANIFEST_BYTES = 64 * 1024
LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".html": "HTML",
    ".css": "CSS",
}
CONFIG_NAMES = {
    "pytest.ini",
    "conftest.py",
    "tox.ini",
    "setup.cfg",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
}
LOCK_NAMES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
}
ENTRY_NAMES = {
    "__main__.py",
    "main.py",
    "app.py",
    "main.js",
    "main.jsx",
    "main.ts",
    "main.tsx",
    "index.js",
    "index.ts",
    "index.tsx",
}


@dataclass(frozen=True)
class ManifestSummary:
    path: str
    dependency_names: tuple[str, ...]
    script_names: tuple[str, ...]
    configured_tools: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryInventory:
    repository_id: int
    commit_sha: str
    file_paths: tuple[str, ...]
    text_paths: tuple[str, ...]
    top_level_directories: tuple[str, ...]
    language_counts: tuple[tuple[str, int], ...]
    test_candidates: tuple[str, ...]
    entry_point_candidates: tuple[str, ...]
    configuration_paths: tuple[str, ...]
    manifest_paths: tuple[str, ...]
    lockfile_paths: tuple[str, ...]
    manifests: tuple[ManifestSummary, ...]
    warnings: tuple[str, ...]


def _string_mapping(value) -> tuple[str, ...]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not key or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError("Expected a string mapping")
    return tuple(sorted(value))


def _manifest(path: str, text: str) -> ManifestSummary:
    if path.endswith("package.json"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        dependencies = set()
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            dependencies.update(_string_mapping(data.get(key, {})))
        return ManifestSummary(
            path, tuple(sorted(dependencies)), _string_mapping(data.get("scripts", {})), ()
        )
    data = tomllib.loads(text)
    project, tools = data.get("project", {}), data.get("tool", {})
    if not isinstance(project, dict) or not isinstance(tools, dict):
        raise ValueError("Expected tables")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) for item in dependencies
    ):
        raise ValueError("Expected dependency strings")
    names = set()
    for dependency in dependencies:
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?=\s|\[|[<>=!~;@]|$)", dependency)
        if match is None:
            raise ValueError("Unrecognized dependency name")
        names.add(match[1])
    return ManifestSummary(
        path,
        tuple(sorted(names)),
        _string_mapping(project.get("scripts", {})),
        tuple(sorted(tools)),
    )


def inventory_snapshot(snapshot: SourceSnapshot) -> RepositoryInventory:
    """Inspect bounded snapshot bytes without imports, subprocesses, installs or network.

    Language/test/entry-point detection is heuristic; manifests describe declarations,
    not installed dependencies or approved validation adapters. No source text is returned.
    """
    paths, texts, directories, tests, entries, configs, manifest_paths, locks = (
        [] for _ in range(8)
    )
    languages, manifests, warnings = Counter(), [], []
    for source in sorted(snapshot.files, key=lambda file: file.path):
        path = PurePosixPath(source.path)
        name = path.name
        paths.append(source.path)
        if len(path.parts) > 1:
            directories.append(path.parts[0])
        if name in LOCK_NAMES:
            locks.append(source.path)
        if name in CONFIG_NAMES or name.startswith(
            ("tsconfig.", "vite.config.", "vitest.config.", "eslint.config.", ".eslintrc")
        ):
            configs.append(source.path)
        is_manifest = name in {"package.json", "pyproject.toml"}
        if is_manifest or (name.startswith("requirements") and name.endswith(".txt")):
            manifest_paths.append(source.path)
        try:
            text = source.content.decode("utf-8-sig")
            if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
                raise ValueError("Non-text bytes")
        except (UnicodeError, ValueError):
            if is_manifest:
                warnings.append(f"{source.path}: manifest is not supported UTF-8 text")
            continue
        texts.append(source.path)
        if language := LANGUAGES.get(path.suffix):
            languages[language] += 1
            if (
                "tests" in path.parts[:-1]
                or "test" in path.parts[:-1]
                or name.startswith("test_")
                or re.search(r"(?:_test\.py|\.(?:test|spec)\.[cm]?[jt]sx?)$", name)
            ):
                tests.append(source.path)
            if name in ENTRY_NAMES:
                entries.append(source.path)
        if is_manifest:
            if len(source.content) > MAX_MANIFEST_BYTES:
                warnings.append(f"{source.path}: manifest exceeds the 64 KiB parsing limit")
                continue
            try:
                manifests.append(_manifest(source.path, text))
            except (ValueError, TypeError, RecursionError):
                warnings.append(f"{source.path}: invalid or unsupported manifest structure")
    return RepositoryInventory(
        repository_id=snapshot.repository_id,
        commit_sha=snapshot.commit_sha,
        file_paths=tuple(paths),
        text_paths=tuple(texts),
        top_level_directories=tuple(sorted(set(directories))),
        language_counts=tuple(sorted(languages.items())),
        test_candidates=tuple(tests),
        entry_point_candidates=tuple(entries),
        configuration_paths=tuple(configs),
        manifest_paths=tuple(manifest_paths),
        lockfile_paths=tuple(locks),
        manifests=tuple(manifests),
        warnings=tuple(warnings),
    )
