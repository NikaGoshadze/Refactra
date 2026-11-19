"""Bounded source data for analysis, never trusted instructions or executable commands."""

import re
from dataclasses import dataclass, field

from refactra.repository_inventory import inventory_snapshot
from refactra.repository_selection import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    FileSelection,
    eligible_file_role,
)
from refactra.repository_snapshot import SourceSnapshot

# Conservative markers, not a general secret scanner. Withhold the entire file on a match.
SECRET_MARKERS = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
    r"|https?://[^/\s:@]+:[^/\s@]+@"
    r"|\b(?:[a-z0-9]+_)*(?:password|passwd|api[_-]?key|client[_-]?secret|access[_-]?token|private[_-]?key|token|secret)"
    r"\b[\"']?\s*(?::\s*(?:str|string)\s*)?[:=]\s*[\"'][^\"'\r\n]+[\"']",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RetrievedFile:
    path: str
    role: str
    start_line: int
    end_line: int
    size_bytes: int
    text: str = field(repr=False)


@dataclass(frozen=True)
class SkippedFile:
    path: str
    reason: str


@dataclass(frozen=True)
class RetrievedContent:
    repository_id: int
    commit_sha: str
    files: tuple[RetrievedFile, ...]
    skipped: tuple[SkippedFile, ...]
    total_bytes: int


def retrieve_selected_content(
    snapshot: SourceSnapshot, selection: FileSelection
) -> RetrievedContent:
    """Read only pinned in-memory bytes, rechecking eligibility, sizes and text each time.

    No disk/network access, repository execution or model calls occur. The caller must
    apply the model's token budget before using this untrusted data in a prompt.
    """
    if (snapshot.repository_id, snapshot.commit_sha) != (
        selection.repository_id,
        selection.commit_sha,
    ):
        raise ValueError("Snapshot and selection must describe the same revision.")
    if len(selection.files) > MAX_FILES or len({item.path for item in selection.files}) != len(
        selection.files
    ):
        raise ValueError("Selection must contain at most eight unique file references.")
    inventory = inventory_snapshot(snapshot)
    sources = {source.path: source for source in snapshot.files}
    files, skipped, total = [], [], 0
    for reference in selection.files:
        source = sources.get(reference.path)
        role = eligible_file_role(reference.path, inventory)
        reason = None
        if source is None:
            reason = "missing_file"
        elif role is None:
            reason = "ineligible_path"
        elif reference.role != role:
            reason = "role_mismatch"
        elif reference.size_bytes != len(source.content):
            reason = "size_mismatch"
        elif len(source.content) > MAX_FILE_BYTES:
            reason = "file_byte_limit"
        else:
            try:
                text = source.content.decode("utf-8-sig")
                if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
                    raise ValueError("Unsupported text")
                if not text:
                    reason = "empty_file"
                elif SECRET_MARKERS.search(text):
                    reason = "possible_secret"
                elif total + len(source.content) > MAX_TOTAL_BYTES:
                    reason = "total_byte_limit"
            except (UnicodeError, ValueError):
                reason = "unsupported_text"
        if reason:
            skipped.append(SkippedFile(reference.path, reason))
            continue
        line_count = len(re.split(r"\r\n|\r|\n", text)) - int(text.endswith(("\r", "\n")))
        files.append(RetrievedFile(reference.path, role, 1, line_count, len(source.content), text))
        total += len(source.content)
    return RetrievedContent(
        snapshot.repository_id, snapshot.commit_sha, tuple(files), tuple(skipped), total
    )
