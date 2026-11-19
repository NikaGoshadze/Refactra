# Read-only analysis benchmark v1

Compare GPT-5.4 mini and DeepSeek V4.1 Flash on five synthetic, owner-authored cases.
This suite measures analysis, not generated-code correctness or completed refactoring.
The execution core is available; CLI/schema setup and reporting are a separate batch.
Reading these files makes no API calls.

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
actual calls require an explicit operator invocation. Do not silently retry.

## Execution core

`refactra.analysis_benchmark.run_benchmark(engine, providers, output_root)` accepts
the two adapters keyed `openai`/`deepseek` and an already-migrated dedicated PostgreSQL
schema. It rejects the default `public` schema; the caller must provision isolation.
Use ignored `.runtime/benchmarks` for output_root. Synthetic run/repository identities
are local benchmark metadata, not authorized GitHub repositories. No GitHub call occurs.
One persisted context is shared by each provider pair, with 15 contexts/30 attempts.

The core fingerprints inputs, evaluator-key bytes and pricing/instruction configuration;
only the key's hash is recorded, never its contents in prompts. Stable attempt IDs and
a session execution lock prevent duplicate/overlapping runs. PostgreSQL admission caps
the schema's lifetime settled estimates plus unknown holds at $1 before sending.
Outputs are atomically replaced JSON artifacts containing status, response text/ID,
prompt hash, tokens/cache metadata, reservation/settled estimates and elapsed time from
input counting through usage settlement. Estimates use configured uncached/peak rates,
not provider invoices or cache/off-peak discounts. Provider failures remain recorded.

Resume skips saved results. A ledger receipt with a missing result becomes unknown_result
without resending; confirmed usage survives but missing response text is not recovered.
Unknown usage retains its full hold. Budget rejection stops the pass. JSON artifacts
are untrusted data, never executable; anonymization, grading and reports are later work.

## Grade collected responses

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
