"""User-facing explanations for stopped runs, shared by the run routes and the history."""

from refactra.publication_target import PUBLISH_CANDIDATE
from refactra.refactoring_plan import APPLY_PLAN_APPROVAL
from refactra.repository_analysis_worker import GENERATE_CANDIDATE
from refactra.repository_candidate import VALIDATE_CANDIDATE

ANALYSIS_FAILURE_MESSAGES = {
    "analysis_access_revoked": "The run owner no longer has access to this repository.",
    "analysis_access_unavailable": "GitHub access could not be verified. Submit the run again.",
    "analysis_access_unconfigured": "The worker's repository access settings are unavailable.",
    "snapshot_unavailable": "The pinned source could not be retrieved. Submit the run again.",
    "snapshot_unsupported": "The pinned source exceeds the supported analysis limits.",
    "worker_command_failed": "The worker stopped this run after an unexpected error.",
    "command_attempts_exhausted": "The worker stopped this run after repeated interruptions.",
    "analysis_budget_exceeded": "The run reached an analysis budget limit.",
    "analysis_input_invalid": "The saved input could not be analyzed.",
    "analysis_outcome_unknown": "The model request outcome could not be confirmed.",
    "analysis_provider_failed": "The model did not return a complete analysis.",
    "analysis_result_missing": "The model request was recorded, but the analysis was not saved.",
}

# Candidate command kind -> (progress state, failure message).
CANDIDATE_STAGES = {
    APPLY_PLAN_APPROVAL: ("queued", "The worker could not verify the approved plan."),
    GENERATE_CANDIDATE: ("generating", "The worker could not generate the candidate patch."),
    VALIDATE_CANDIDATE: ("validating", "Candidate validation failed."),
    PUBLISH_CANDIDATE: ("publishing", "The worker could not finish publishing the draft PR."),
}
