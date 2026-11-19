# Read-only analysis benchmark v1

Compare GPT-5.4 mini and DeepSeek V4.1 Flash on five synthetic, owner-authored cases.
This suite measures analysis, not generated-code correctness or completed refactoring.
The operator command runs the execution core and produces metadata-only summaries.
Reading these files makes no API calls; paid execution requires `--run`.

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

## Operator command

From the repository root, start local PostgreSQL and install the locked backend
dependencies. The ignored `.env` must contain the host `REFACTRA_DATABASE_URL` and
`REFACTRA_OPENAI_API_KEY` / `REFACTRA_DEEPSEEK_API_KEY`. Download the pinned tokenizer
as described in the root README; `REFACTRA_DEEPSEEK_TOKENIZER_PATH` defaults to
`.runtime/deepseek-v41-tokenizer.json`. Literal plain/quoted `.env` values are supported,
without shell expansion; process environment values take precedence. Never pass keys
as command arguments. Paths are relative to the current working directory.

```powershell
# Paid execution: up to 30 initial requests; reuses receipts and the $1 ledger cap.
uv run --project services/backend python -m refactra.benchmark_operator --run

# No provider calls or credentials required; substitute the printed hash directory.
uv run --project services/backend python -m refactra.benchmark_operator --summary .runtime/benchmarks/HASH
```

`--env-file PATH` selects another local configuration file. The command only accepts
loopback PostgreSQL hosts and creates/migrates the persistent `refactra_benchmark`
schema with a search path excluding `public`. Application tables are not migrated.
Do not drop this schema to retry: it preserves budget holds across interrupted runs.
Outputs and `summary.json` stay under ignored `.runtime/benchmarks/HASH` by default.

For an explicitly requested clean comparison after an adapter fix, use
`--run --trial response-id-fix-1`. A new label creates separate attempts/artifacts
while preserving all earlier results and costs in the same capped ledger. Reusing
a label resumes it; omitting the flag retains the original trial identity. Never
delete prior failure records or reservations to retry. Response IDs are opaque
nonblank strings: OpenAI's `resp_` prefix and DeepSeek's UUID IDs are both accepted.
Tests cover DeepSeek's documented UUID shape; the original OpenAI-shaped mock missed it.

Summaries show completion against 15 planned requests per provider, missing/error
statuses, confirmed token totals, available cache metadata, conservative settled
estimates and unknown-cost holds separately. Latency includes recorded successful
and failed attempts from counting through settlement; recovered unknown results have
no invented timings. Artifact totals may omit interrupted attempts without files;
PostgreSQL admission remains authoritative. No response text or secrets are printed;
quality remains ungraded and no winner is assigned. `--run` exits nonzero for incomplete runs; `--summary` can inspect
them without resending. Anonymization and rubric grading remain separate work.

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
