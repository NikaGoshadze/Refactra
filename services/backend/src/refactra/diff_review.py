"""Report observable evidence from the approved plan and its saved candidate diff."""

import re

from pydantic import BaseModel

from refactra.refactoring_plan import ASSET_CHECK, PATCH_CHECK, PlanContent
from refactra.repository_validation import ValidationResult

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_ROUTE = re.compile(
    r"^([+-])\s*@\w+[.](?:route|get|post|put|patch|delete)\(\s*['\"](/[^'\"]{1,160})['\"]"
)


class FileEvidence(BaseModel):
    path: str
    action: str
    diff_line: int | None


class CriterionEvidence(BaseModel):
    id: str
    description: str
    passing_checks: list[str]


class RouteFinding(BaseModel):
    path: str
    route: str
    diff_line: int


class DiffReview(BaseModel):
    files: list[FileEvidence]
    criteria: list[CriterionEvidence]
    routes_to_review: list[RouteFinding]


def review_candidate_diff(
    plan: PlanContent, diff: str, checks: list[ValidationResult]
) -> DiffReview:
    lines = diff.split("\n")
    headers = {
        (
            "--- /dev/null" if edit.action == "CREATE" else f"--- a/{edit.path}",
            "+++ /dev/null" if edit.action == "DELETE" else f"+++ b/{edit.path}",
        ): edit.path
        for edit in plan.changes
    }
    intentions = {edit.path: edit.intent for edit in plan.changes}
    goals = " ".join(
        [plan.summary, *(criterion.description for criterion in plan.acceptance_criteria)]
    )
    first_lines, routes, removed_routes = {}, [], set()
    current, remaining = None, 0
    for index, line in enumerate(lines):
        line = line.rstrip("\r")
        if remaining:
            if line[:1] in {"+", "-", " "}:
                remaining -= 2 if line.startswith(" ") else 1
            if current and current.endswith(".py") and (route := _ROUTE.match(line)):
                if route[1] == "-":
                    removed_routes.add((current, route[2]))
                elif (
                    len(routes) < 10
                    and route[2] not in goals
                    and route[2] not in intentions[current]
                ):
                    routes.append(RouteFinding(path=current, route=route[2], diff_line=index + 1))
            continue
        if hunk := _HUNK.match(line):
            remaining = sum(int(count) if count is not None else 1 for count in hunk.groups())
        if index + 1 < len(lines) and (path := headers.get((line, lines[index + 1].rstrip("\r")))):
            current = path
            first_lines.setdefault(path, index + 1)
        if line.startswith("Replace binary file ") and line[20:] in intentions:
            first_lines.setdefault(line[20:], index + 1)
    passed = {(check.check_id, tuple(check.paths)) for check in checks if check.status == "PASSED"}
    return DiffReview(
        files=[
            FileEvidence(path=edit.path, action=edit.action, diff_line=first_lines.get(edit.path))
            for edit in plan.changes
        ],
        criteria=[
            CriterionEvidence(
                id=criterion.id,
                description=criterion.description,
                passing_checks=sorted(
                    {
                        check.check_id
                        for check in plan.validation_checks
                        if (check.check_id, tuple(check.paths)) in passed
                        and check.check_id not in {PATCH_CHECK, ASSET_CHECK}
                        and criterion.id in check.criteria_ids
                    }
                ),
            )
            for criterion in plan.acceptance_criteria
        ],
        routes_to_review=[item for item in routes if (item.path, item.route) not in removed_routes],
    )
