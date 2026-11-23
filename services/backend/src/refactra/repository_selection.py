"""Bounded initial file references, not AI analysis or permission to edit/read content."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from posixpath import normpath

from refactra.repository_inventory import RepositoryInventory
from refactra.repository_snapshot import SKIP_DIRECTORIES, SourceSnapshot

MAX_FILES = 8
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 128 * 1024
TABULAR_SUFFIXES = {".csv", ".tsv", ".jsonl", ".ndjson"}
DATA_TERMS = {"csv", "tsv", "jsonl", "ndjson", "dataset", "datasets"}
BROWSER_REQUEST = re.compile(r"\b(?:browser|frontend|html|page|web|form)\b", re.IGNORECASE)
BROWSER_PATH_TERMS = {"template", "templates", "web"}
STOP_WORDS = frozenset(
    (
        "the and for from with into this that refactor extract change "
        "file files code test tests src py js ts add implement modify frontend backend"
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
    return "test" if path in inventory.test_candidates else "source"


def _named_in_task(path: str, task: str) -> bool:
    name = PurePosixPath(path).name
    return any(
        re.search(r"(?<![\w/.-])" + re.escape(value) + r"(?![\w/.-])", task)
        for value in {path, name}
    )


def _relative_imports(path: PurePosixPath, text: str) -> set[str]:
    """Recognize the relative links used by Python and JavaScript module imports."""
    targets = {
        normpath(str(path.parent / relative))
        for relative in re.findall(r"""['"](\.{1,2}/[^'"/][^'"\s]*)['"]""", text)
    }
    for dots, module in re.findall(r"\bfrom\s+(\.+)([\w.]+)\s+import\b", text):
        parent = path.parent
        for _ in dots[1:]:
            parent = parent.parent
        targets.add(str(parent / module.replace(".", "/")))
    return targets


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
    browser_request = bool(BROWSER_REQUEST.search(task_text))
    text_paths = set(inventory.text_paths)
    entry_paths = set(inventory.entry_point_candidates)
    candidates, oversized = [], 0
    imports: dict[str, set[str]] = {}
    named = set()
    for source in sorted(snapshot.files, key=lambda file: file.path):
        role = eligible_file_role(source.path, inventory)
        if source.path not in text_paths or role is None:
            continue
        is_named = _named_in_task(source.path, task_text)
        suffix = PurePosixPath(source.path).suffix.casefold()
        if suffix in TABULAR_SUFFIXES and not is_named and not terms.intersection(DATA_TERMS):
            continue
        if len(source.content) > MAX_FILE_BYTES:
            oversized += 1
            continue
        path_terms = _words(source.path)
        path_hits = len(terms.intersection(path_terms))
        # Inspect only eligible bounded bytes; never export content or matched task terms.
        text = source.content.decode("utf-8-sig").casefold()
        content_hits = sum(term in text for term in terms) if role != "metadata" else 0
        score = 10 * path_hits + content_hits
        browser_surface = (
            browser_request
            and role == "source"
            and (path_hits or content_hits)
            and (
                bool(BROWSER_PATH_TERMS.intersection(path_terms))
                or suffix in {".html", ".jinja", ".jinja2"}
                or (suffix == ".py" and "render_template(" in text)
            )
        )
        if browser_surface:
            score += 16
        if role == "source":
            imports[source.path] = _relative_imports(
                PurePosixPath(source.path), source.content.decode("utf-8-sig")
            )
        reasons = (
            ("task_path_match",) if path_hits else ("task_content_match",) if content_hits else ()
        )
        if browser_surface:
            reasons += ("browser_surface",)
        if is_named:
            named.add(source.path)
            reasons = ("explicit_path",) + reasons
            score += 1000
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
    focus = sources[:1]
    related_modules = set().union(*(imports[item.path] for item in focus))
    focused_paths = {item.path for item in focus}
    for _, item in ranked:
        if len(focus) == 3:
            break
        if item.role != "source" or item.path in focused_paths:
            continue
        path = PurePosixPath(item.path)
        names = {item.path, str(path.with_suffix(""))}
        if path.stem in {"__init__", "index"}:
            names.add(str(path.parent))
        if names & related_modules:
            focus.append(SelectedFile(item.path, item.role, ("relative_import",), item.size_bytes))
    focused_paths = {item.path for item in focus}
    focus.extend([item for item in sources if item.path not in focused_paths][: 3 - len(focus)])
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
    explicit = [item for _, item in ranked if item.path in named]
    ordered = explicit + [
        item
        for item in focus + [item for _, item in tests[:3]] + metadata[:2]
        if item.path not in named
    ]
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
