"""AI-written branch name, commit message and pull request text for a candidate.

The model writes this text in the same call that produces the patch. It is untrusted: the
server caps lengths, strips control characters, reduces the branch name to safe characters and
neutralises @mentions so a draft PR cannot notify people. The cleaned text is saved with the
candidate, so commit rebuilds reproduce the same commit, and it is copied into the publication
target, so the owner approves exactly what will be published. Candidates saved before this
feature have no text and keep the original names.
"""

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_SLUG = 40
MAX_SUBJECT = 72
MAX_COMMIT_BODY = 2000
MAX_TITLE = 100
MAX_SUMMARY = 4000

INSTRUCTIONS = (
    "Also fill publication: a short lowercase branch_slug of a few words joined by hyphens, a "
    "commit_subject in the imperative mood under 72 characters, a commit_body of a few plain "
    "lines explaining what changed and why, a pr_title without a Draft or WIP prefix (GitHub "
    "already marks the pull request as a draft), and a "
    "pr_summary in Markdown describing the change, the files touched and how it was checked. "
    "Describe only this patch; do not mention people, credentials or tools."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "branch_slug": {"type": "string"},
        "commit_subject": {"type": "string"},
        "commit_body": {"type": "string"},
        "pr_title": {"type": "string"},
        "pr_summary": {"type": "string"},
    },
    "required": ["branch_slug", "commit_subject", "commit_body", "pr_title", "pr_summary"],
    "additionalProperties": False,
}


class PublicationText(BaseModel):
    """Cleaned text saved with a candidate."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    branch_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=MAX_SLUG)
    commit_subject: str = Field(min_length=1, max_length=MAX_SUBJECT, pattern=r"^[^\n]*$")
    commit_body: str = Field(max_length=MAX_COMMIT_BODY)
    pr_title: str = Field(min_length=1, max_length=MAX_TITLE, pattern=r"^[^\n]*$")
    pr_summary: str = Field(max_length=MAX_SUMMARY)


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")


def _text(value, limit: int, *, single_line: bool = False) -> str:
    text = _CONTROL.sub("", str(value).replace("\r\n", "\n").replace("\t", " "))
    if single_line:
        text = " ".join(text.split())
    text = _MENTION.sub("@⁠", text).strip()  # A word joiner stops GitHub mention links.
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def clean_publication_text(raw: object) -> PublicationText | None:
    """Clean the model's publication object; None when it is missing or unusable."""
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(key), str) for key in SCHEMA["required"]
    ):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", raw["branch_slug"].lower()).strip("-")[:MAX_SLUG].strip("-")
    subject = _text(raw["commit_subject"], MAX_SUBJECT, single_line=True)
    title = _text(raw["pr_title"], MAX_TITLE, single_line=True)
    if not slug or not subject or not title:
        return None
    return PublicationText(
        branch_slug=slug,
        commit_subject=subject,
        commit_body=_text(raw["commit_body"], MAX_COMMIT_BODY),
        pr_title=title,
        pr_summary=_text(raw["pr_summary"], MAX_SUMMARY),
    )


def publication_from_candidate(candidate: dict) -> PublicationText | None:
    saved = candidate.get("publication")
    return PublicationText.model_validate(saved) if saved is not None else None


def head_branch(run_id: UUID, revision: int, text: PublicationText | None) -> str:
    if text is None:
        return f"refactra/{run_id}/r{revision}"
    # The run ID prefix keeps names unique; later result revisions get their own branch.
    suffix = str(run_id)[:8] + (f"-r{revision}" if revision > 1 else "")
    return f"refactra/{text.branch_slug}-{suffix}"


def commit_message(run_id: UUID, revision: int, text: PublicationText | None) -> str:
    if text is None:
        return f"Refactra candidate {run_id} r{revision}"
    body = text.commit_body
    return f"{text.commit_subject}\n\n{body}\n" if body else f"{text.commit_subject}\n"


def validation_warnings(validation: list[dict], diagnostic_id) -> list[str]:
    """Markdown lines for required checks that could not run or failed."""
    return [
        f"- `{check_id}` · {check['status']}"
        for check in validation
        if check["status"] in {"UNAVAILABLE", "FAILED"}
        and (check_id := diagnostic_id(check.get("check_id")))
    ]


def pull_request_text(
    text: PublicationText | None, *, commit: str, base_sha: str, warnings: list[str]
) -> dict[str, str]:
    """The draft PR's title and body; the fixed sections are written by Refactra, not the model."""
    sections = [text.pr_summary] if text and text.pr_summary else []
    if warnings:
        sections.append("### Validation warnings\n\n" + "\n".join(warnings))
    sections.append(
        f"---\nPrepared by Refactra from approved commit `{commit}` on pinned base `{base_sha}`."
    )
    title = text.pr_title if text else f"Refactra candidate {commit[:12]}"
    return {"title": title, "body": "\n\n".join(sections)}
