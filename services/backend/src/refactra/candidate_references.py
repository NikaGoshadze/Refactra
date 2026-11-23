"""Bounded read-only Python dependencies from the pinned candidate workspace."""

import ast
import warnings
from collections import deque
from itertools import islice
from pathlib import Path, PurePosixPath

from refactra.python_symbols import MAX_NODES_PER_FILE, python_import_module
from refactra.repository_content import SECRET_MARKERS
from refactra.repository_selection import is_excluded_source_path

MAX_REFERENCE_FILES = 4
MAX_REFERENCE_BYTES = 4800


def _import_paths(path: PurePosixPath, text: str):
    if path.suffix != ".py":
        return
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return
    for node in islice(ast.walk(tree), MAX_NODES_PER_FILE):
        if isinstance(node, ast.Import):
            yield from (PurePosixPath(*alias.name.split(".")) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = python_import_module(path, node)
            if module is not None and module.parts:
                yield module
                if node.module is None:
                    yield from (module / alias.name for alias in node.names if alias.name != "*")


def _eligible(path: str, editable: set[str]) -> bool:
    parts = path.split("/")
    ref = PurePosixPath(path)
    return (
        path not in editable
        and len(path) <= 1024
        and path.isprintable()
        and all(part and part not in {".", ".."} for part in parts)
        and not any(character in path for character in "\\:\0")
        and ref.suffix == ".py"
        and not is_excluded_source_path(ref)
    )


def _safe_text(text: str) -> bool:
    return bool(text and "\0" not in text and not SECRET_MARKERS.search(text))


def _reference_paths(source: dict[str, str]):
    for path, text in source.items():
        for module in _import_paths(PurePosixPath(path), text):
            yield module / "__init__.py", module.with_suffix(".py")


def collect_candidate_references(
    root: Path, source: dict[str, str], editable: set[str]
) -> dict[str, str]:
    result, seen = {}, set()
    remaining = MAX_REFERENCE_BYTES
    pending = deque(_reference_paths(source))
    while pending:
        alternatives = pending.popleft()
        if alternatives in seen:
            continue
        seen.add(alternatives)
        for ref in alternatives:
            key = ref.as_posix()
            if not _eligible(key, editable):
                continue
            target = root / ref
            try:
                if not target.is_file() or target.resolve() != target:
                    continue
                with target.open("rb") as file:
                    raw = file.read(remaining + 1)
                if len(raw) > remaining:
                    break
                content = raw.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            if _safe_text(content):
                result[key] = content
                remaining -= len(raw)
                if len(result) == MAX_REFERENCE_FILES or not remaining:
                    return result
                pending.extend(_reference_paths({key: content}))
            break
    return result


def valid_candidate_references(value: object, source: dict[str, str], editable: set[str]) -> bool:
    if not (
        isinstance(value, dict)
        and len(value) <= MAX_REFERENCE_FILES
        and all(
            _eligible(path, editable) and isinstance(text, str) and _safe_text(text)
            for path, text in value.items()
        )
        and sum(len(text.encode("utf-8")) for text in value.values()) <= MAX_REFERENCE_BYTES
    ):
        return False
    remaining = dict(value)
    pending = deque(_reference_paths(source))
    while remaining and pending:
        for ref in pending.popleft():
            key = ref.as_posix()
            if key in remaining:
                pending.extend(_reference_paths({key: remaining.pop(key)}))
                break
    return not remaining
