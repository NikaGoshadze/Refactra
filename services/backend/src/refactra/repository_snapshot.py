"""Read an authorized run's pinned source into bounded memory; never write or execute it.

Callers must recheck session/tester/repository and current GitHub permission before
passing a freshly scoped read token and a run loaded from PostgreSQL.
"""

import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

import httpx

from refactra.github_app import GitHubAppError, InstallationToken
from refactra.models import Run

MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 1000
SKIP_DIRECTORIES = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".next", "dist", "build", "coverage"}
)


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class SourceSnapshot:
    repository_id: int
    commit_sha: str
    files: tuple[SourceFile, ...] = field(repr=False)


def _read_archive(data: bytes) -> tuple[SourceFile, ...]:
    files, seen, root, total, count = [], set(), None, 0, 0
    with ZipFile(BytesIO(data)) as archive:
        for entry in archive.infolist():
            name = entry.orig_filename.rstrip("/") if entry.is_dir() else entry.orig_filename
            parts = name.split("/")
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (
                any(part in {"", ".", ".."} for part in parts)
                or re.search(r"[\x00-\x1f\x7f\\:]", name)
                or name in seen
                or mode not in {0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG}
                or entry.flag_bits & 1
                or entry.compress_type not in {0, 8}
                or (root is not None and parts[0] != root)
            ):
                raise ValueError("Unsafe or unsupported archive entry")
            root = parts[0]
            seen.add(name)
            if entry.is_dir():
                continue
            count += 1
            total += entry.file_size
            if (
                len(parts) < 2
                or count > MAX_FILES
                or total > MAX_SOURCE_BYTES
                or entry.file_size > MAX_FILE_BYTES
            ):
                raise ValueError("Source archive exceeds supported bounds")
            if SKIP_DIRECTORIES.intersection(parts[1:-1]):
                continue
            with archive.open(entry) as source:
                content = source.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES or len(content) != entry.file_size:
                raise ValueError("Source file exceeds supported bounds")
            files.append(SourceFile("/".join(parts[1:]), content))
    return tuple(sorted(files, key=lambda source: source.path))


def fetch_source_snapshot(
    run: Run, token: InstallationToken, *, transport: httpx.BaseTransport | None = None
) -> SourceSnapshot:
    """Use the persisted SHA, never the branch head. Return no partial snapshot on failure."""
    try:
        if (
            run.workflow_version != "repository-v0.1"
            or type(run.github_repository_id) is not int
            or run.github_repository_id <= 0
            or token.repository_id != run.github_repository_id
            or not isinstance(run.base_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", run.base_sha)
            or not token.value.strip()
            or token.expires_at.tzinfo is None
            or token.expires_at <= datetime.now(UTC)
        ):
            raise ValueError("A pinned repository run and matching live token are required")
        with httpx.Client(
            timeout=10.0, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            with client.stream(
                "GET",
                f"https://api.github.com/repositories/{run.github_repository_id}/zipball/{run.base_sha}",
                headers={
                    "Authorization": f"Bearer {token.value}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            ) as response:
                if response.status_code != 302:
                    raise ValueError("Expected an archive redirect")
                location = response.headers["Location"]
                target = urlsplit(location)
                if target.scheme != "https" or target.netloc != "codeload.github.com":
                    raise ValueError("Unexpected archive host")
            # The temporary archive URL carries its own authorization; do not forward the token.
            with client.stream(
                "GET", location, headers={"Accept-Encoding": "identity"}
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("Content-Encoding", "identity") != "identity"
                    or int(response.headers.get("Content-Length", "0")) > MAX_ARCHIVE_BYTES
                ):
                    raise ValueError("Archive download unavailable or oversized")
                data = bytearray()
                for chunk in response.iter_raw(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > MAX_ARCHIVE_BYTES:
                        raise ValueError("Archive download exceeds supported bounds")
                    data.extend(chunk)
        files = _read_archive(bytes(data))
        return SourceSnapshot(run.github_repository_id, run.base_sha, files)
    except (
        httpx.HTTPError,
        ValueError,
        KeyError,
        TypeError,
        BadZipFile,
        EOFError,
        RuntimeError,
        ZlibError,
    ):
        raise GitHubAppError("Repository source snapshot could not be retrieved safely.") from None
