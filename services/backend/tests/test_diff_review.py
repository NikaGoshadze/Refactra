from uuid import uuid4

from refactra.candidate_patch import _diff_lines
from refactra.diff_review import review_candidate_diff
from refactra.refactoring_plan import PlanContent

from .test_candidate_results import passing_patch


def test_diff_review_flags_extra_route_and_keeps_yaml_behavior_unverified():
    paths = ["books_app/web_app.py", "settings.yaml", "README.md"]
    plan = PlanContent(
        schema_version="1",
        run_id=uuid4(),
        repository={"repository_id": 42, "base_branch": "main", "base_sha": "a" * 40},
        summary="Add /search to the browser and enable a setting.",
        changes=[
            {"path": path, "action": "MODIFY", "intent": "Implement the approved request."}
            for path in paths
        ],
        acceptance_criteria=[{"id": "AC1", "description": "The feature flag is enabled."}],
        validation_checks=[
            {
                "check_id": "repository.patch",
                "paths": paths,
                "required": True,
                "criteria_ids": ["AC1"],
            }
        ],
        preserve_behavior=["Keep the CLI unchanged."],
        assumptions=[],
        blocking_questions=[],
    )
    diff = b"".join(
        [
            *_diff_lines(paths[0], b"", b'@app.post("/search")\n@app.get("/cli-search")\n'),
            *_diff_lines(paths[1], b"feature: false\n", b"feature: false\n# enabled\n"),
        ]
    ).decode()
    review = review_candidate_diff(plan, diff, [passing_patch(*paths)])
    assert [item.diff_line is not None for item in review.files] == [True, True, False]
    assert [item.route for item in review.routes_to_review] == ["/cli-search"]
    assert review.criteria[0].passing_checks == []
