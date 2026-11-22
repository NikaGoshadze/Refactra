"""Apply bounded, structured candidate edits to a freshly staged workspace."""

import re
from base64 import b64decode
from collections.abc import Iterator
from difflib import diff_bytes, unified_diff
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from refactra.refactoring_plan import PlanPath, approved_file_operation, is_env_cleanup

if TYPE_CHECKING:
    from refactra.repository_workspace import ApprovedWorkspace


MAX_CANDIDATE_BYTES = 1024 * 1024
MAX_EDIT_FILE_BYTES = 64 * 1024
MAX_DIFF_BYTES = 200 * 1024


class CandidatePatchError(ValueError):
    """An operator-safe error without candidate contents."""


class CandidateEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    path: PlanPath
    action: Literal["CREATE", "MODIFY", "DELETE"]
    content: Annotated[str, Field(max_length=((MAX_EDIT_FILE_BYTES + 2) // 3) * 4)] | None


class CandidatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    edits: list[CandidateEdit] = Field(min_length=1, max_length=10)


def read_candidate_patch(path: Path) -> CandidatePatch:
    with path.open("rb") as candidate:
        raw = candidate.read(MAX_CANDIDATE_BYTES + 1)
    if len(raw) > MAX_CANDIDATE_BYTES:
        raise CandidatePatchError("The candidate JSON exceeds the 1 MiB limit.")
    try:
        return CandidatePatch.model_validate_json(raw)
    except ValidationError:
        raise CandidatePatchError("The candidate JSON does not match the patch format.") from None


def _diff_lines(path: str, before: bytes | None, after: bytes | None) -> Iterator[bytes]:
    old_name = b"/dev/null" if before is None else f"a/{path}".encode()
    new_name = b"/dev/null" if after is None else f"b/{path}".encode()
    if not before and not after:
        yield b"--- " + old_name + b"\n+++ " + new_name + b"\n"
        return
    for line in diff_bytes(
        unified_diff,
        (before or b"").splitlines(keepends=True),
        (after or b"").splitlines(keepends=True),
        fromfile=old_name,
        tofile=new_name,
    ):
        yield line if line.endswith(b"\n") else line + b"\n\\ No newline at end of file\n"


def apply_candidate_patch(workspace: "ApprovedWorkspace", candidate: CandidatePatch) -> bytes:
    """Preflight the complete patch, then write only inside the disposable workspace."""
    if (candidate.base_sha, candidate.plan_sha256) != (workspace.base_sha, workspace.sha256):
        raise CandidatePatchError(
            "The candidate must target the approved commit and plan checksum."
        )
    approved = {change.path: change.action for change in workspace.changes}
    proposed = {edit.path: edit.action for edit in candidate.edits}
    if len(proposed) != len(candidate.edits) or proposed != approved:
        raise CandidatePatchError(
            "The candidate must match the approved file paths and actions exactly."
        )

    root = workspace.root.resolve(strict=True)
    operation = approved_file_operation(workspace.changes, workspace.validation_checks)
    env_cleanup = is_env_cleanup(workspace.changes, workspace.validation_checks)
    writes: list[tuple[Path, str, bytes | None]] = []
    diff = bytearray()
    for edit in sorted(candidate.edits, key=lambda item: item.path):
        destination = root.joinpath(*PurePosixPath(edit.path).parts)
        if (
            not destination.is_relative_to(root)
            or destination.is_symlink()
            or destination.resolve() != destination
        ):
            raise CandidatePatchError("The workspace contains a redirected path.")
        if (edit.action == "DELETE") != (edit.content is None):
            raise CandidatePatchError(
                "Create and modify require text content. Delete requires null."
            )

        before = None
        if edit.action == "CREATE":
            if destination.exists():
                raise CandidatePatchError("An approved create targets an existing path.")
        else:
            if not destination.is_file() or destination.stat().st_nlink != 1:
                raise CandidatePatchError(
                    "An approved edit targets a missing, linked, or non-regular file."
                )
            if env_cleanup and edit.path == ".env":
                before = b""
            else:
                with destination.open("rb") as source:
                    before = source.read(MAX_EDIT_FILE_BYTES + 1)
        if operation == "asset_replace":
            try:
                after = b64decode(edit.content or "", validate=True)
            except ValueError:
                raise CandidatePatchError("The replacement asset is invalid.") from None
            if (
                not after
                or len(after) > MAX_EDIT_FILE_BYTES
                or len(before or b"") > MAX_EDIT_FILE_BYTES
            ):
                raise CandidatePatchError("Asset replacements must use bounded file contents.")
            if before == after:
                raise CandidatePatchError("The supplied asset must change the file.")
            diff.extend(
                f"Replace binary file {edit.path}\n"
                f"Before SHA-256 {sha256(before or b'').hexdigest()}\n"
                f"After SHA-256 {sha256(after).hexdigest()}\n".encode()
            )
            writes.append((destination, edit.action, after))
            continue
        try:
            after = edit.content.encode("utf-8") if edit.content is not None else None
            for content in (before, after):
                if content is not None:
                    if len(content) > MAX_EDIT_FILE_BYTES or b"\0" in content:
                        raise CandidatePatchError("Edited files must be text and at most 64 KiB.")
                    content.decode("utf-8")
        except UnicodeError:
            raise CandidatePatchError("Edited files must use UTF-8.") from None
        if env_cleanup and edit.path == ".env.example":
            entries = [line for line in (after or b"").splitlines() if line.strip()]
            if not entries or any(
                not re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_]*=", line) for line in entries
            ):
                raise CandidatePatchError(
                    "The .env.example template must contain names with empty values."
                )
        if before == after:
            raise CandidatePatchError("An approved modify must change the file contents.")
        for line in _diff_lines(edit.path, before, after):
            if len(diff) + len(line) > MAX_DIFF_BYTES:
                raise CandidatePatchError("The review diff exceeds the 200 KiB limit.")
            diff.extend(line)
        writes.append((destination, edit.action, after))

    for destination, action, data in writes:
        if data is None:
            destination.unlink()
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb" if action == "CREATE" else "wb") as output:
                output.write(data)
    return bytes(diff)
