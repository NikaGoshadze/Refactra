"""Bounded initial file references, not AI analysis or permission to edit/read content."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from refactra.repository_inventory import LANGUAGES, RepositoryInventory
from refactra.repository_snapshot import SKIP_DIRECTORIES, SourceSnapshot

MAX_FILES = 8
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 128 * 1024
STOP_WORDS = frozenset(
    (
        "the and for from with into this that refactor extract change "
        "file files code test tests src py js ts"
    ).split()
)
BLOCKED_PARTS = SKIP_DIRECTORIES | {
    ".github",
    ".runtime",
    ".aws",
    ".ssh",
    "secrets",
    "credentials",
    "vendor",
}


@dataclass(frozen=True)
class SelectedFile:
    path: str
    role: str
    reasons: tuple[str, ...]
    size_bytes: int


@dataclass(frozen=True)
class FileSelection:
    repository_id: int
    commit_sha: str
    files: tuple[SelectedFile, ...]
    total_bytes: int
    budget_omitted: int
    used_fallback: bool


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.casefold())) - STOP_WORDS


def is_excluded_source_path(path: PurePosixPath) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    name = parts[-1]
    return bool(
        BLOCKED_PARTS.intersection(parts)
        or name.startswith((".env", "secret", "credential", "id_rsa", "id_ed25519"))
        or name in {".npmrc", ".pypirc"}
        or path.suffix.casefold() in {".pem", ".key", ".p12", ".pfx", ".lock", ".map"}
        or ".min." in name
    )


def eligible_file_role(path: str, inventory: RepositoryInventory) -> str | None:
    """Shared path/type policy only; callers must separately validate content and budgets."""
    if (
        path not in inventory.file_paths
        or is_excluded_source_path(PurePosixPath(path))
        or path in inventory.lockfile_paths
    ):
        return None
    if path in inventory.manifest_paths or path in inventory.configuration_paths:
        return "metadata"
    if PurePosixPath(path).suffix not in LANGUAGES:
        return None
    return "test" if path in inventory.test_candidates else "source"


def select_task_files(
    snapshot: SourceSnapshot, inventory: RepositoryInventory, task_text: str
) -> FileSelection:
    """Shortlist lexical matches, related test names and ancestor metadata deterministically.

    Accept only the matching validated snapshot/inventory. No file contents are returned.
    Later retrieval must reapply its content policy and model-token budget; references
    do not establish edit approval, runnable checks or absence of embedded secrets.
    """
    if (snapshot.repository_id, snapshot.commit_sha) != (
        inventory.repository_id,
        inventory.commit_sha,
    ):
        raise ValueError("Snapshot and inventory must describe the same revision.")
    if not isinstance(task_text, str) or not 1 <= len(task_text) <= 4000 or not task_text.strip():
        raise ValueError("A nonblank task of at most 4000 characters is required.")
    terms = set(sorted(_words(task_text))[:32])
    text_paths = set(inventory.text_paths)
    entry_paths = set(inventory.entry_point_candidates)
    candidates, oversized = [], 0
    for source in sorted(snapshot.files, key=lambda file: file.path):
        role = eligible_file_role(source.path, inventory)
        if source.path not in text_paths or role is None:
            continue
        if len(source.content) > MAX_FILE_BYTES:
            oversized += 1
            continue
        path_hits = len(terms.intersection(_words(source.path)))
        # Inspect only eligible bounded bytes; never export content or matched task terms.
        text = source.content.decode("utf-8-sig").casefold()
        content_hits = sum(term in text for term in terms) if role != "metadata" else 0
        score = 10 * path_hits + content_hits
        reasons = (
            ("task_path_match",) if path_hits else ("task_content_match",) if content_hits else ()
        )
        candidates.append((score, SelectedFile(source.path, role, reasons, len(source.content))))
    ranked = sorted(candidates, key=lambda item: (-item[0], item[1].path))
    sources = [item for score, item in ranked if item.role == "source" and score > 0]
    used_fallback = not sources
    if used_fallback:
        sources = [item for _, item in ranked if item.role == "source" and item.path in entry_paths]
        sources = sources or [item for _, item in ranked if item.role == "source"]
        sources = [
            SelectedFile(item.path, item.role, ("structural_fallback",), item.size_bytes)
            for item in sources
        ]
    focus = sources[:3]
    focus_names = set().union(*(_words(PurePosixPath(item.path).stem) for item in focus))
    tests, metadata = [], []
    for score, item in ranked:
        path = PurePosixPath(item.path)
        if item.role == "test":
            related = bool(focus_names.intersection(_words(path.stem)))
            if score or related:
                reasons = item.reasons + (("related_test_name",) if related else ())
                tests.append(
                    (
                        score + 5 * related,
                        SelectedFile(item.path, item.role, reasons, item.size_bytes),
                    )
                )
        if item.role == "metadata" and any(
            path.parent in PurePosixPath(source.path).parents for source in focus
        ):
            metadata.append(
                SelectedFile(item.path, item.role, ("ancestor_metadata",), item.size_bytes)
            )
    tests.sort(key=lambda item: (-item[0], item[1].path))
    metadata.sort(key=lambda item: (-len(PurePosixPath(item.path).parts), item.path))
    ordered = focus + [item for _, item in tests[:3]] + metadata[:2]
    selected, total, omitted = [], 0, oversized
    for item in ordered:
        if len(selected) >= MAX_FILES or total + item.size_bytes > MAX_TOTAL_BYTES:
            omitted += 1
            continue
        selected.append(item)
        total += item.size_bytes
    return FileSelection(
        snapshot.repository_id, snapshot.commit_sha, tuple(selected), total, omitted, used_fallback
    )
