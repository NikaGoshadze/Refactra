# Read-only analysis benchmark v1

Compare GPT-5.4 mini and DeepSeek V4.1 Flash on five synthetic, owner-authored cases.
This suite measures analysis, not generated-code correctness or completed refactoring.
There is no provider runner in this batch and reading these files makes no API calls.

## Inputs and grading

- `cases.toml` contains only model-visible case IDs, language, task and source files.
- `grading.toml` contains evaluator-only expected findings and critical failures. Never
  send it, this README or evaluator test code to either model.
- Present every source file in sorted path order with 1-based line numbers derived
  from its exact decoded TOML string. Preserve whitespace and the same prompt for both.
- No repository retrieval/selection occurs in this benchmark: both see the same files.
  This isolates the model from retrieval quality; full-pipeline evaluation comes later.

## Scoring and decision

Before collecting answers, freeze both TOML files at a recorded Git commit/hash.
Plan three fresh-context attempts per case per model (30 initial requests), alternate provider
order, disable thinking explicitly for both and allow at most 3,000 output tokens.
Inputs must fit both providers' token limits without truncation. Cap total spend at $1;
actual calls still require the later budget-controlled runner. Do not silently retry.

Grade anonymized answers against each case's ten checks. Each check earns 0 (missing
or wrong), 0.5 (partly correct) or 1 (fully correct); record evidence for deductions.
Category points = category weight × mean of its two check ratings. Sum categories for
a score out of 100. Accurate references require the right path and relevant line range.
Equivalent sound solutions are accepted; do not require exact wording. A critical
failure makes the response unacceptable regardless of its raw score.

Report each case's three scores, then their mean; overall quality is the mean of the
five case means. Report completion rate separately; API failures have no quality grade
and cannot silently disappear. Fewer than three graded answers for any case makes the
comparison incomplete. Retrying a failed attempt is separately recorded and budgeted.
Acceptable responses score at least 80 with no critical failure. Cost per acceptable
response = total attempt cost / acceptable responses; report N/A if the divisor is zero.
Include token counts, estimated USD at the applicable rates, cached tokens, median/worst
end-to-end latency and failures. Invoice reconciliation is separate from token estimates.

A candidate needs an overall score ≥80 and no unresolved critical failure. If both
qualify within five points, prefer lower cost per acceptable response, using latency
as a further tie-breaker. Inspect larger quality gaps before choosing. Results are
provisional for this analysis role; test actual patches separately when editing exists.
