import json
from uuid import UUID

import pytest

from refactra.candidate_request import CANDIDATE_INSTRUCTIONS, candidate_text_config
from refactra.publication_text import (
    INSTRUCTIONS,
    MAX_SUBJECT,
    MAX_SUMMARY,
    PublicationText,
    clean_publication_text,
    commit_message,
    head_branch,
    pull_request_text,
)

RUN = UUID("1234abcd-0000-4000-8000-000000000000")
RAW = {
    "branch_slug": "Extract Summary_Lines!!",
    "commit_subject": "Extract   summary\nlines",
    "commit_body": "Move the display logic.\r\nKeep messages.",
    "pr_title": "Extract summary lines",
    "pr_summary": "## Summary\n\nMoves logic. Thanks @octocat and see email a@b.c.",
}


def test_model_text_is_cleaned_into_safe_values():
    text = clean_publication_text(RAW)
    assert text.branch_slug == "extract-summary-lines"
    assert text.commit_subject == "Extract summary lines"  # One line, spaces collapsed.
    assert text.commit_body == "Move the display logic.\nKeep messages."
    # A word joiner after @ stops GitHub from notifying people; e-mail addresses stay.
    assert "@⁠octocat" in text.pr_summary and "a@b.c" in text.pr_summary


def test_control_characters_and_overlong_text_are_trimmed():
    text = clean_publication_text(
        {
            **RAW,
            "commit_subject": "x" * 200,
            "pr_summary": "y" * 5000,
            "pr_title": "Bidi‮abc\u0007",
        }
    )
    assert len(text.commit_subject) == MAX_SUBJECT and text.commit_subject.endswith("…")
    assert len(text.pr_summary) == MAX_SUMMARY
    assert text.pr_title == "Bidiabc"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {**RAW, "pr_title": 7},
        {**RAW, "branch_slug": "!!!"},
        {**RAW, "commit_subject": "  "},
    ],
)
def test_missing_or_unusable_text_falls_back(raw):
    assert clean_publication_text(raw) is None


def test_names_and_messages_with_and_without_ai_text():
    text = clean_publication_text(RAW)
    assert head_branch(RUN, 1, text) == "refactra/extract-summary-lines-1234abcd"
    assert head_branch(RUN, 2, text) == "refactra/extract-summary-lines-1234abcd-r2"
    assert head_branch(RUN, 1, None) == f"refactra/{RUN}/r1"
    assert commit_message(RUN, 1, text) == (
        "Extract summary lines\n\nMove the display logic.\nKeep messages.\n"
    )
    assert commit_message(RUN, 1, None) == f"Refactra candidate {RUN} r1"


def test_pull_request_text_adds_fixed_sections():
    text = clean_publication_text(RAW)
    pr = pull_request_text(text, commit="c" * 40, base_sha="b" * 40, warnings=["- `x` · FAILED"])
    assert pr["title"] == "Extract summary lines"
    assert pr["body"].startswith("## Summary")
    assert "### Validation warnings\n\n- `x` · FAILED" in pr["body"]
    assert pr["body"].endswith(f"approved commit `{'c' * 40}` on pinned base `{'b' * 40}`.")
    fallback = pull_request_text(None, commit="c" * 40, base_sha="b" * 40, warnings=[])
    assert fallback["title"] == f"Refactra candidate {'c' * 12}"
    assert "Validation warnings" not in fallback["body"]


def test_saved_text_is_strictly_validated():
    with pytest.raises(ValueError):
        PublicationText(
            branch_slug="Bad Slug",
            commit_subject="ok",
            commit_body="",
            pr_title="ok",
            pr_summary="",
        )


def test_the_model_must_return_publication_text():
    context = json.dumps({"approved_plan": {"changes": [{"path": "src/a.py", "action": "MODIFY"}]}})
    schema = candidate_text_config(context)["format"]["schema"]
    assert schema["required"] == ["edits", "publication"]
    assert set(schema["properties"]["publication"]["required"]) == {
        "branch_slug",
        "commit_subject",
        "commit_body",
        "pr_title",
        "pr_summary",
    }


def test_the_prompt_asks_for_publication_text_without_a_draft_prefix():
    assert INSTRUCTIONS in CANDIDATE_INSTRUCTIONS
    assert "without a Draft or WIP prefix" in INSTRUCTIONS
