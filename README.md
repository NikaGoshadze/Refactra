# Refactra

Autonomous repository refactoring and draft pull-request automation with five sequential agent roles and two human approvals.

Current stage: Phase 1 — Architecture & Foundation. A local mock skeleton connects a Next.js UI, FastAPI, PostgreSQL and a separate command worker. The real agent workflow is not implemented or deployed.

## Run locally

Install Docker Desktop with Linux containers (WSL2 on Windows), then run from the repository root:

```powershell
.\scripts\start-local.ps1
```

Open [the local workspace](http://localhost:3000). The [API docs](http://localhost:8000/docs) and [readiness endpoint](http://localhost:8000/health/ready) are available separately.

Compose waits for PostgreSQL, applies Alembic migrations, then starts the API and worker. Default development credentials and ports are in [.env.example](.env.example); copying it to `.env` is optional for Compose. These credentials and this unauthenticated mock stack are for local development only. Published ports bind to loopback; do not expose this build as the public demo.

Run the same script after changing code or switching branches. It builds the web image, replaces the web container, and checks that the running image matches the build and can reach the API. A Docker Desktop restart reuses the existing container. A build on its own updates the image. Both steps are needed to serve changed web code. [Docker Compose startup](https://docs.docker.com/reference/cli/docker/compose/up/), [Docker Compose restart](https://docs.docker.com/reference/cli/docker/compose/restart/)

```powershell
docker compose ps
docker compose logs --tail 50 api worker
docker compose down
```

The named PostgreSQL volume survives `down`. Removing that volume deletes local runs. No GitHub credentials, model keys or paid services are needed.

## Optional local GitHub sign-in

The mock queue works without GitHub. Sign-in establishes identity; protected repository metadata is available when the API override is enabled. Live refactoring remains unimplemented.

Create your local configuration from the repository root with Python 3.12:

```powershell
python scripts/prepare-local-env.py
```

This creates an ignored `.env` with a random OAuth-cookie signing key. It never prints that key and leaves an existing `.env` untouched. Keep real secrets in `.env`; `.env.example` contains blank placeholders only. If `.env` already exists, add missing keys from the example and retain your existing signing key.

Register a development **GitHub App** under **Settings → Developer settings → GitHub Apps → New GitHub App**. Use a unique name, such as `Refactra-dev-<short-suffix>`, and these settings:

| Setting | Local value |
|---|---|
| Homepage URL | Your Refactra repository URL |
| Callback URL | `http://localhost:3000/api/v1/auth/github/callback` |
| Expire user authorization tokens | Enabled |
| Request user authorization during installation | Disabled; use our sign-in link to initiate it |
| Device Flow / Setup URL | Disabled / empty |
| Webhook Active | Disabled; this sign-in flow uses no webhooks |
| Permissions | Keep at minimum/default access for sign-in; repository read/write grants come with later integrations |
| Installation availability | Only on this account for the owner's initial development App |

These settings follow [GitHub's registration instructions](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app). [User authorization does not require installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-with-a-github-app-on-behalf-of-a-user). This identity-only flow uses the Client ID and client secret; no App private key is needed.

Copy the App's **Client ID**, generate a **client secret**, and edit `.env` locally:

```dotenv
REFACTRA_GITHUB_CLIENT_ID=your-app-client-id
REFACTRA_GITHUB_CLIENT_SECRET=your-client-secret
REFACTRA_GITHUB_CALLBACK_URL=http://localhost:3000/api/v1/auth/github/callback
```

Retain the generated `REFACTRA_OAUTH_STATE_SECRET`. The Client ID differs from the App ID; the client secret is required for the server's authorization-code exchange. See [GitHub's user access-token flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app). Compose supplies the OAuth client secret and signing key only to the API service; other services do not receive these secrets. The optional repository check also uses the public Client ID.

Start or recreate the stack after editing `.env`:

```powershell
.\scripts\start-local.ps1
```

Use **http://localhost:3000** consistently. Opening `127.0.0.1` instead changes the browser origin and cookie host. If you change the web port, update both the local callback setting and the GitHub App callback to match. HTTPS callbacks automatically use Secure cookies; HTTP is supported only for loopback development.

Manual verification after credentials are configured:

1. Click **Sign in with GitHub**, authorize your development App, and confirm the page displays your GitHub login.
2. Reload and confirm the signed-in state persists. `/api/v1/auth/session` should return your identity through the web origin.
3. Click **Sign out**, reload, and confirm anonymous status remains. Session cookies are HttpOnly and are not available through JavaScript.
4. Try declining authorization; no new session should be created. Backend auth access logs must show redacted query values rather than authorization codes/state.

Automated tests use simulated GitHub responses. A real GitHub round trip is a separate manual check; do not claim it passed until observed. Avoid posting `.env`, client secrets, cookie values or unredacted configuration output in issues/logs. Authentication does not enable live runs or authorize repository actions.

## Optional read-only GitHub repository check

This operator-only command verifies the App installation against one repository. It reads repository metadata; it does not retrieve source, authorize browser users or enable live runs.

1. In your GitHub App's **Permissions & events**, set **Repository permissions → Contents → Read-only** and save. Keep other permissions at their minimum/default values.
2. Under **Install App**, install it on your account with **Only select repositories**, choosing the one test repository. If already installed, update its selected repositories and accept the changed permission when GitHub prompts. See [GitHub's installation instructions](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app).
3. In the App's **Credentials → Key pairs → New key** (or **General → Private keys** in the older layout), download the PEM. Save it locally as `.runtime/secrets/github-app.pem`, creating that directory if needed. Use the downloaded file, not its SHA fingerprint or the OAuth client secret. Keep the file out of chat and Git. See [GitHub's private-key instructions](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps).
4. Add these settings to your ignored `.env`, keeping your existing OAuth settings:

```dotenv
REFACTRA_GITHUB_INSTALLATION_ID=your-installation-id
REFACTRA_GITHUB_REPOSITORY_ID=your-repository-id
```

The installation ID is the number in `https://github.com/settings/installations/NUMBER` when configuring the installed App; it is different from the App ID. The repository ID is the repository's numeric `id` in GitHub's API. For the owner's first check, `NikaGoshadze/AI-Hub` has repository ID `1001132861`.

From the repository root, run:

```powershell
docker compose --profile github-check run --build --rm --no-deps github-check
```

The optional profile mounts the PEM read-only into that one-off container. The default stack starts without a PEM; a missing PEM causes the check to fail without creating a directory in its place. The file stays outside Docker build contexts. The web app and worker do not receive it; the API receives it only with the separate override below. On Linux hosts, allow container user UID 10001 to read the key without making it publicly readable; Docker Desktop uses the Windows file-sharing permissions.

Success prints only the repository name/ID, `read-only` access and token expiry. The client requests Contents/Metadata read access for exactly the configured repository ID and checks the returned scope/expiry before use. No JWT, installation token or private key is printed or persisted. The token expires automatically; see [GitHub's scoped-token flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

This diagnostic uses operator-supplied IDs. An API for browser users must independently check the user's session, invitation and repository permissions before using the App client. Automated checks simulate GitHub; record a live check separately after completing installation/key setup.

## Repository access policy foundation

The backend has a local eligibility policy for future repository endpoints. It resolves an active, unexpired, non-revoked Refactra session, checks the user's numeric GitHub ID against `REFACTRA_ALLOWED_GITHUB_USER_IDS`, and requires the requested repository ID to match `REFACTRA_GITHUB_REPOSITORY_ID`. Missing allowlists/repository settings deny access; malformed settings are rejected. Usernames are display data, not allowlist entries.

`REFACTRA_ALLOWED_GITHUB_USER_IDS` accepts comma-separated numeric IDs, for example `12345,67890`. Keep real local choices in ignored `.env`; an empty value allows nobody. This is an operator-managed tester list, with no invitation UI or extra database table.

The separate GitHub permission checker mints a repository-scoped read token, confirms repository identity, resolves the user's current login by numeric ID, and checks that same user's current base permission. It requires `write` or `admin`; GitHub maps maintain roles to the `write` base permission. A role name alone does not grant access. Every call rechecks GitHub; missing access is denied and unverifiable responses fail closed. See [GitHub's permission endpoint](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user).

`GET /api/v1/repositories/{repository_id}` now applies the local policy before loading App credentials or calling GitHub, checks current write permission, and returns only `repository_id`, `full_name`, `default_branch` and `access: "read-only"`. Metadata and handled access-error responses use `Cache-Control: no-store`. It returns 401 for missing/inactive sessions, 403 for local/GitHub access denial, 503 for unavailable configuration and 502 when GitHub results cannot be verified. Credentials and upstream error bodies are not returned.

Runtime settings are `REFACTRA_ALLOWED_GITHUB_USER_IDS`, `REFACTRA_GITHUB_REPOSITORY_ID`, `REFACTRA_GITHUB_INSTALLATION_ID`, `REFACTRA_GITHUB_CLIENT_ID` and a readable `REFACTRA_GITHUB_PRIVATE_KEY_PATH`. The default Compose API does not receive the repository settings or key mount; enable the optional override below for local use. The web proxy exposes this endpoint as GET-only for positive decimal repository IDs. Endpoint tests use PostgreSQL sessions and simulated GitHub responses.

The workspace's Repository connection panel targets the selected AI-Hub development fixture (`1001132861`); repository selection is not implemented yet. Confirm your account, then click Check repository access to retrieve metadata. Loading/errors are explicit, failed checks can be retried, and sign-out clears details and cancels pending reads. Results stay in component memory and are rechecked on demand; the browser permits 65 seconds for the proxy's 60-second request limit. This panel does not enable real refactoring or change the mock task queue.

Repository proxy calls forward only Refactra session cookies (local and HTTPS names), preserve backend status/JSON, and force `no-store` on responses. OAuth/unrelated cookies and browser Authorization headers are not forwarded to this route. The proxy permits up to 60 seconds for the backend's sequential GitHub checks; other route timeouts are unchanged. Proxy tests use an isolated production Next.js server and a simulated API, including a response delayed beyond five seconds.

The returned App token remains inside the backend and read-only even when the user has write access. The operator-only `github-check` command remains separate and uses no browser session. Live refactoring and publication remain disabled.

## Optional protected repository API runtime

Complete GitHub App sign-in and repository installation/key setup above. In ignored `.env`, set `REFACTRA_ALLOWED_GITHUB_USER_IDS` to the allowed testers' numeric GitHub IDs (comma-separated), and fill the existing Client ID, installation ID and repository ID. Keep the PEM at `.runtime/secrets/github-app.pem`.

Opt in explicitly from the repository root:

```powershell
.\scripts\start-local.ps1 -RepositoryApi
```

This builds the backend and web images before starting Compose, then verifies that the running API and web containers use those images. The protected repository override adds repository settings and a read-only PEM mount to the API while preserving its database and OAuth settings. Missing required environment values stop Compose configuration, and a missing PEM stops the API from starting. Keep using `-RepositoryApi` for protected access on later starts. Compose merges the override with the base file. [Docker's merge rules](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/)

Sign in at `http://localhost:3000`, then use the Repository connection panel, or open `http://localhost:3000/api/v1/repositories/1001132861` in that same browser for AI-Hub through the web proxy. The direct API remains at `http://localhost:8000/api/v1/repositories/1001132861`. Use `localhost` consistently so the browser sends the local session cookie. A successful request returns only repository metadata with `access: "read-only"`; an anonymous request returns 401, and a disallowed user/repository returns 403. No tokens or PEM contents appear in the response. Use the configured web/API ports if they differ from 3000/8000.

To remove the API key mount and repository settings again:

```powershell
docker compose -f compose.yaml up -d --no-deps --force-recreate --wait api
```

That recreates only the API with the base configuration; it preserves the PostgreSQL volume and existing sessions. No real refactoring or publication is enabled by this override.

## Repository revision foundation

The backend revision resolver accepts an already-authorized, repository-scoped read token, validates repository identity, and reads the exact branch head through [GitHub's reference endpoint](https://docs.github.com/en/rest/git/refs#get-a-reference). Use `resolve_revision(token, base_branch="develop")` for an explicit selection, or omit `base_branch` to use the repository default. The existing `resolve_default_revision(token)` helper also selects the default.

The immutable result keeps `default_branch` (repository metadata) and `base_branch` (the actual selection) separate, alongside repository ID/name and the full 40-character commit SHA. Later source reads must use that returned SHA, so a moving branch does not change an existing resolved revision. Branch selections are literal names, including names with `/`; invalid names raise a sanitized `ValueError` before network access. Empty selections are rejected, and missing branches never silently fall back to the default or to a tag. Validation follows [Git's branch/ref naming rules](https://git-scm.com/docs/git-check-ref-format).

Missing/invalid branch references and GitHub failures stop resolution without falling back to another branch or tag. The resolver is not exposed by the metadata API or UI yet.

The internal `create_repository_run(session, identity=..., installation_id=..., revision=..., task_text=...)` service persists the requesting user, installation/repository IDs, selected base branch, exact SHA, task and initial `CREATED` state, plus a `RUN_CREATED` event in the caller's transaction. Callers must first verify session/tester/repository eligibility and current GitHub permission, then pass the revision resolved with that installation's scoped token. Only numeric identities and revision/task data are stored; credentials are not accepted by this service.

Repository records use `repository-v0.1`, separate from mock runs. Source retrieval uses the saved `base_sha`. A repository run can be queued after its analysis context is ready.

The internal `fetch_source_snapshot(run, token)` reader accepts a run loaded from PostgreSQL and a fresh scoped token after current access checks. It requests [GitHub's ZIP archive](https://docs.github.com/en/rest/repos/contents#download-a-repository-archive-zip) at the saved SHA, accepts one HTTPS redirect to `codeload.github.com`, and keeps the installation token on the API request only. Downloads stop above 10 MiB; archive-wide limits are 20 MiB uncompressed, 1,000 regular files and 2 MiB per file, including entries in skipped directories. Common dependency/build directories (`node_modules`, `.venv`, `venv`, `__pycache__`, `.next`, `dist`, `build`, `coverage`, `.git`) are omitted from the returned files.

The reader rejects unsafe paths, duplicate/mixed archive roots, links, special/encrypted entries, unsupported compression and corrupt ZIPs. Files are returned as an immutable, sorted in-memory snapshot with repository ID and commit SHA; failures return no partial snapshot. No files are written or executed, and source bytes are excluded from object representations. Full-snapshot persistence and workflow/API/UI wiring remain later work; binary contents must be filtered before any model input.

## Repository inventory foundation

`inventory_snapshot(snapshot)` inspects a bounded, validated `SourceSnapshot` without network, imports of repository code, installs or subprocesses. Its immutable report retains the repository ID/SHA and sorted file paths, top-level directories, UTF-8 text candidates, language counts, likely tests/entry points, manifests, lockfiles and familiar test/lint/build configuration paths. Language detection currently covers Python, JavaScript, TypeScript, HTML and CSS; test/entry-point names are heuristics. Nested manifests keep their paths, without implying monorepo execution support.

For manifests up to 64 KiB, JSON/TOML parsing reports npm dependency/script names and Python `project.dependencies`, `project.scripts` names and `[tool]` keys. Script bodies, dependency versions and source text are omitted. Requirements/lock/config files are listed without executing or resolving them; Python optional/development groups and dynamic metadata are not resolved in this first inventory. Invalid, oversized or non-text manifests produce sanitized warnings while preserving the rest of the inventory. Other files failing UTF-8 decoding or containing unsupported control bytes stay in the file list but are excluded from text/language/test candidates.

These observations establish context, not installed tools, passing tests, an execution profile or approved commands. AI analysis and workflow/API/UI integration remain later batches. Text candidacy alone does not authorize sending a file to a model.

## Task file selection foundation

`select_task_files(snapshot, inventory, task_text)` requires matching repository IDs/SHAs and a nonblank task of at most 4,000 characters. It returns immutable file references with roles, reasons and byte sizes, preserving the revision. This initial heuristic uses up to 32 task terms to rank path/content matches, takes up to three source files, adds up to three test-name/task matches and two metadata files from ancestor directories. Without source matches it falls back to conventional entry points or sorted source files. The AI can reassess this initial shortlist; it is not semantic analysis or a complete dependency graph.

Default limits are eight files, 64 KiB per file and 128 KiB in total. Oversized files are skipped rather than truncated; `budget_omitted` counts eligible files excluded for size and shortlisted files that fail the final budget pass. Lockfiles, common generated/dependency paths, minified files, known sensitive paths/names and unsupported file types are excluded; only inventory text candidates are eligible. Manifests and test/tool configuration are metadata references, not edit targets. No file contents, commands or task terms are returned or logged by the helper.

This is a conservative filename policy, not a general secret scanner. References do not grant read/edit approval or authorize model calls. Content retrieval rechecks its policy; model callers must enforce token budgets and must not treat repository instructions as authorization. The selector performs no network, execution or imports of repository code, installs or model calls; workflow/API/UI wiring remains separate work.

## Bounded content retrieval foundation

`retrieve_selected_content(snapshot, selection)` reads the selected files from the validated in-memory snapshot only. It checks repository ID/SHA, rejects duplicate or excessive references, rebuilds the inventory to recheck path/type/role eligibility, and verifies actual file sizes independently of selection totals. It enforces the same eight-file, 64 KiB-per-file and 128 KiB-total byte limits. Missing, blocked, mismatched, oversized, empty, non-UTF-8 or control-byte files receive explicit skip reasons; oversized files are not truncated.

Returned immutable records carry repository ID/SHA, path, role, original byte size, full text and a one-based start/end line range. UTF-8 BOMs are removed; original newline characters are retained, with physical LF/CRLF/CR line counting. Text is excluded from object representations. Common private-key headers, literal credential assignments and HTTP basic-auth URLs cause the entire file to be withheld as `possible_secret`; this conservative check can have false positives and is not a general secret scanner.

The result is untrusted repository data, not instructions or edit/command approval. The helper performs no filesystem/network reads, repository execution or model calls. Byte bounds do not establish a model-token budget; model-specific token accounting and workflow/API/UI integration remain later work.

## Python structural inspection foundation

`inspect_python_symbols(content)` consumes policy-checked `RetrievedContent` and uses [Python 3.12's AST parser](https://docs.python.org/3.12/library/ast.html) on `.py`/`.pyi` files. Its immutable report preserves the repository ID/SHA, file paths/roles and upstream skip reasons. Definitions include classes, synchronous/async functions, lexical dotted names and inclusive line spans that include decorators. Import declarations retain modules, imported names, aliases, relative levels (as leading dots), lexical scope and line spans. Source bodies, defaults, literals and docstrings are not returned.

Each file is limited to 10,000 visited AST nodes and 200 combined definition/import records. Exceeding a limit or encountering parser recursion discards that file's partial index with `inspection_limit`; invalid syntax records `syntax_error` and its line without echoing source/errors. Parser syntax warnings are suppressed to avoid source-line logging. Other files remain available, and non-Python files receive `unsupported_language` skips.

This is structural evidence, not semantic analysis, a resolved dependency/call graph, compiler validation or passing tests. Imports are not resolved, decorators/defaults are not evaluated and target code is never executed. Dynamic imports and lambda/variable bindings are not indexed. Model calls, token accounting and workflow/API/UI wiring remain later work.

## Durable run analysis context

`prepare_analysis_context(run, snapshot)` packages the inventory, shortlist, policy-checked retrieved text and Python inspection into versioned JSON bound to the saved run ID, repository ID, commit SHA and task. Prepare outside the write transaction after authorizing repository access and fetching the pinned snapshot. Its immutable bytes are excluded from normal object representations. The package is capped at 2 MiB; it retains only the source text admitted by retrieval, not the entire snapshot.

`save_analysis_context(session, prepared)` locks the run for the short save, rechecks its identity, and writes one `run_analysis_contexts` row plus an `ANALYSIS_CONTEXT_SAVED` event in the caller's transaction. Identical retries reuse the row without adding an event; different payloads cannot replace it through this helper. `load_analysis_context(session, run_id)` reloads the saved package after validating its run/revision/task, without GitHub access or recomputing the earlier steps. Event payloads contain only version/size metadata. This is a structured input checkpoint; reviewable analysis/plan/diff artifacts remain separate later work.

Migration `0003_analysis_contexts` adds the table and database format/size constraints; apply migrations before using the updated backend (readiness requires the latest migration head). No API or worker command is added, run status remains unchanged and real execution stays disabled. Source text is now retained in the local PostgreSQL context row, so existing authorization/content policies still apply; provider integration and actual agent orchestration remain subsequent work.

## Offline analysis request budgets

`prepare_analysis_request(context, pricing, usage, monthly_committed_usd=..., count_input_tokens=...)` prepares the read-only analysis instructions and complete saved context. It requires explicitly configured model/context-window limits, finite nonnegative Decimal input/output rates and a trusted provider-aware token counter. The counter receives the full model input and must account for the actual provider envelope as well as instructions/context. There is no character-count fallback, selected provider, bundled live pricing or provider SDK in this step.

Preflight caps are 12,000 input / 6,000 requested output tokens per call (default output allowance 3,000); 40,000 input / 12,000 output tokens and eight attempts per run; estimated spend of $0.75 per run and $12 per month. The requested output allowance must also fit the configured model context window. Cost estimates use all input tokens plus the full requested output allowance at configured rates, without assuming cache discounts. All limits are inclusive; blocked requests raise a source-free `BudgetExceeded.code` without truncating context.

The immutable result contains model input (text excluded from repr), the maximum estimated request cost and candidate run/monthly reservations. Usage must include actual usage plus outstanding reservations, including calls with unknown outcomes. This preflight helper alone neither persists counters nor enforces concurrency; durable admission below rechecks actual ledger balances. Verified live model pricing/tokenization and provider execution remain later batches; real execution stays disabled and this helper makes no paid calls.

## Durable model budget reservations

`reserve_analysis_request(session, attempt_id, prepared_request)` requires trusted preflight output and a stable UUID for one physical attempt. Inside a short READ COMMITTED transaction, it takes a transaction-scoped PostgreSQL advisory lock, verifies the saved context/trusted instructions and sums the reservation ledger to recheck run attempts, tokens and estimated cost plus the UTC calendar month's estimated cost across runs. It disregards stale preflight usage snapshots. Autocommit and other isolation levels are rejected. A single ledger/lock suits the current one-worker scale without a separate monthly-counter service.

The helper atomically inserts a `model_reservations` row and metadata-only activity event. It records a request fingerprint and rounded-up Decimal cost (12 fractional digits), with no prompt/source body in the ledger. Matching attempt IDs return `created=False` without another allocation; conflicting requests cannot reuse the ID. **Commit before transport.** Only a newly committed receipt represents a new attempt; an existing receipt never authorizes resending. A real retry needs a new attempt UUID and another allocation.

Migration `0004_model_reservations` adds the ledger, bounds and run/month indexes; readiness requires the latest migration head. No provider transport, API route, worker scheduling or status transition is introduced. Unsettled allocations remain held, including unknown outcomes. The running stack must apply migrations when upgraded; tests use isolated schemas and no paid calls.

## Confirmed model usage settlement

Preflight requests carry their verified pricing configuration. New reservations capture the input/output rates and check that the requested estimate matches them. `settle_model_usage(session, attempt_id, input_tokens=..., output_tokens=...)` accepts only confirmed nonnegative integer counts from trusted provider metadata, never model-generated claims. It records observed token counts and an estimated cost at the reservation's original uncached rates; it does not claim to reproduce a provider invoice.

Settlement uses the same transaction lock as admission and commits its usage record/event in the caller's transaction. Identical repeated reports add no event; conflicting reports cannot overwrite confirmed usage. Later admission sums confirmed usage where available and otherwise retains the full hold. Unused token/cost allowances are released, but the attempt count never decreases. Missing/unknown usage is not zero usage. Overruns are recorded without clamping, flagged in the event and included in future budget checks.

Migration `0005_model_usage` adds nullable pricing and usage fields and an all-or-none settlement constraint. Migration `0006_analysis_output` raises the per-call reservation ceiling to 6,000 output tokens. Existing holds remain held and require their original rate snapshot for settlement.

## Read-only repository worker

After authorizing model use for the repository, an internal caller can queue a prepared run with `enqueue_repository_analysis(session, run_id)`. The worker uses its saved, pinned context and GPT-5.4 mini with the default 3,000-token output allowance. Set `REFACTRA_OPENAI_API_KEY` in the worker environment and start `uv run python -m refactra.worker --repository-analysis` after applying migrations. Choose either mock or repository analysis mode for the single worker slot.

Migration `0007_analysis_results` adds `run_analysis_results`. The worker saves the result as untrusted text and transitions the run to `NEEDS_HUMAN_REVIEW` in the same transaction. Budget receipts are committed before generation. If a restarted worker finds a receipt without a saved result, it records an unknown outcome or missing result and stops that run.

## Protected analysis submission

Set `REFACTRA_ENABLE_REPOSITORY_ANALYSIS=true` for the configured repository API to enable `POST /api/v1/runs`. Submit JSON with `repository_id`, a literal `base_branch`, and `task_text`. Send the session cookie, the configured app `Origin`, `X-Refactra-CSRF: 1`, and a UUID `Idempotency-Key`. The API requires an invited user with current GitHub write access and reads source using the App's scoped read token.

A `202` receipt contains the run ID, repository ID, branch, pinned SHA, and creation timestamp. Its `Location` header points to the run status. A retry using the same account, key and input keeps the same commit, even after a failed source download or a branch update. Use a new key to submit different input. The worker can claim the command once the bounded context and command commit together.

`GET /api/v1/runs/{run_id}` returns the pinned revision and run status. It includes `analysis_text` once the saved result is ready for human review, or a sanitized `stop_reason` if analysis failed. Treat the result as untrusted model text. Each read requires ownership, an active session, and current repository access under the configured installation. Existing runs remain readable when new submissions are disabled.

Both routes are available through the same-origin Next.js proxy. It forwards only Refactra session cookies, preserves the original submission origin and CSRF and idempotency headers, and keeps responses uncached. The proxy allows up to 90 seconds to prepare a submission and 60 seconds to check a run.

After verifying repository access, use **Repository analysis** to choose the base branch and enter a task. The panel shows the pinned commit and polls until the worker saves the analysis or records a failure. A submission retry with the same input reuses its key while the panel is open. The panel clears on sign-out and tracks the current run in memory.

## Refactoring plan checkpoint

Migration `0008_run_plans` adds plan revisions. After a run has saved analysis, an internal caller can pass a structured proposal to `save_refactoring_plan`. The server binds it to that run and commit. PostgreSQL stores the exact JSON bytes, bounded to 64 KiB, and checks their SHA-256 hash. An identical retry returns the current revision. A changed proposal appends a revision only when the caller's expected revision still matches. A saved proposal is untrusted input for human review.

To generate the first plan as a local operator, run `uv run python -m refactra.repository_plan_worker <run-id>` from `services/backend` after migrating. Set the database URL, OpenAI key, and the protected repository configuration in the process environment, including the PEM path. The command rechecks the submitting user's current GitHub write access, uses the saved analysis and pinned source, and returns plan metadata. Planning requests use OpenAI Structured Outputs and the existing run budget. Local validation still checks rules such as whether the proposed checks reference criteria in the same plan.

Rerunning the command returns a saved plan or stops if a structured attempt was already recorded. A run with a settled receipt from the earlier prompt-only implementation and no saved plan can make one structured attempt. An earlier receipt with unknown usage blocks the attempt.

`GET /api/v1/runs/{run_id}/plan` on the backend returns the latest saved plan. Add `?revision=1` to read a specific revision. The API verifies run ownership and current GitHub access before returning the plan or a `plan_not_found` error. The response includes the proposal and artifact metadata. The SHA-256 identifies the exact saved document bytes.

The planner and plan read API also report the number of blocking questions, unregistered validation IDs, and whether a registered check is required. The initial Python profile recognizes `python.pytest` during plan review. Earlier drafts with IDs like `VC1` stay reviewable and need revision. These checks inspect plan metadata only. Running tests and approving a plan are later steps.

In the browser, click **Load saved plan** when an analysis is ready for review. To return to an earlier run, verify repository access, choose **Open a saved run**, and enter its run ID. The plan view shows the latest saved revision. Loading it uses the saved analysis and plan without making a model request.

Use **Revise plan draft** to edit the proposal JSON. Record answers in the relevant fields, remove resolved questions, and update any unrecognized validation IDs. The protected `POST /api/v1/runs/{run_id}/plan` takes `{expected_revision, proposal}` and returns the saved draft with updated readiness. A changed draft appends an immutable revision. A stale edit returns 409, and retrying the same saved content returns its existing revision.

The protected `POST /api/v1/runs/{run_id}/plan/approve` accepts `{plan_id, revision, sha256}` with the active owner's session, trusted Origin, and `X-Refactra-CSRF: 1`. The latest revision must match all three fields, have zero blocking questions, and use registered validation IDs with a required check. PostgreSQL records one decision and event per run. Plan reads include the receipt for that revision, and new draft edits are locked once approved.

In the browser, review the saved draft and click **Approve plan revision N**. The plan shows the recorded decision, including when reopened.

Migration `0010_plan_approval_queue` is required for API readiness. Approval and its queue command commit together. The migration also queues previously approved plans. From `services/backend`, run `uv run python -m refactra.worker --repository-approval --once` with `REFACTRA_DATABASE_URL` set. The worker rechecks the saved revision, checksum, owner, and readiness, then records `PLAN_APPROVAL_VERIFIED`. A claim can be recovered after its lease expires. This checkpoint makes no model calls. Repository editing is a later step.

After worker verification, run `uv run python -m refactra.repository_workspace <run-id>` with the same database and protected GitHub settings as the planner. It rechecks GitHub access, fetches the exact approved commit, and checks that the plan's Python file actions match the source. Protected paths and conflicting filesystem names are excluded or rejected before staging. The command removes the temporary directory and reports the pinned commit, plan revision, and allowed changes. This prepares source for a later isolated editing and validation step.

Add `--candidate <patch.json>` to apply a local candidate inside the fresh workspace and include a review diff and its SHA-256 in the JSON output. The candidate contains `base_sha`, `plan_sha256`, and `edits`. Each edit has `path`, `action`, and `content` with the full UTF-8 text for `CREATE` or `MODIFY`, or `null` for `DELETE`. The path and action list must match the approved plan exactly. The limits are 1 MiB per input JSON, 64 KiB per edited file, and 200 KiB for the diff. The workspace is removed after the preview.

To generate that JSON from a verified plan, run `uv run python -m refactra.repository_candidate <run-id> --output <new-patch.json>` with the same GitHub, database, and OpenAI settings as the planner. The model receives the approved plan and pinned contents of its existing edit targets. One budgeted attempt per approved plan produces a local patch that passes the existing scope and diff checks. Use the workspace command above to review its diff and the validation command below to run tests.

The `--repository-candidate` worker verifies queued approvals and saves generated patches in PostgreSQL with model settlement. After migration `0014_candidate_validation`, run it on the Docker host with `--candidate-source <local-clone> --candidate-artifacts <existing-artifact-directory>` to process validation jobs too. It runs isolated pytest, saves passing evidence, and prepares the commit and publication target. A reclaimed job reuses saved validation evidence. Each artifact directory is named `<run-id>-r<result-revision>`. Use the approved repository's clone and a directory under an ignored location such as `.runtime`.

Add `--validate` with `--candidate` to run the approved pytest files and report exit codes, bounded output, durations, and the runner image ID alongside the diff. Run this command on the Docker host with the locally built `refactra-backend:local` image. The runner uses a fixed pytest command with repository configuration and plugin autoload disabled. It inspects the container's offline, non-root, read-only setup and resource limits before starting. A required check that fails or exceeds a limit produces a nonzero command exit. This first runner covers the candidate tests for the curated Python fixture.

Add `--save-result` to the same command to store a successful candidate, its diff, and its validation evidence in PostgreSQL. This requires migration `0011_candidate_results`. The command returns a result ID, revision, and checksum. Saving the same candidate and diff again returns the original result. A different passing candidate gets a new revision.

Open the saved run and load its approved plan, then select **Load saved result** to review the latest candidate diff and validation output. The protected `GET /api/v1/runs/{run_id}/result` endpoint also accepts `?revision=1` to retrieve a specific saved result.

To prepare the reviewed revision for publication, run this from `services/backend` with `REFACTRA_DATABASE_URL` set and Git installed.

```powershell
uv run python -m refactra.candidate_commit <run-id> <result-revision> --source <local-clone> --output <new-artifact-directory>
```

Use a trusted local clone that contains the pinned commit and a new output directory under an ignored location such as `.runtime`. Refactra reads the pinned Git objects, reproduces the saved diff byte for byte, then creates a commit in `repository.git` alongside `receipt.json`. The receipt binds the commit to the result checksum, approved plan, and proposed PR base and head. Publication requires a separate approval.

Add `--save-target` to save the verified commit and PR destination in PostgreSQL for publication review. This option requires migration `0012_publication_targets` and accepts an existing output directory when its receipt and Git ref match the recomputed commit. Repeating the command returns the same `publication_target_sha256`.

Refresh the saved result in the browser to review the commit and branches. **Approve publication** records the run owner's decision for that exact target. A later publication step uses that decision to open the draft PR.

After approval, an operator can run `uv run python -m refactra.publication_branch <run-id> --artifact <prepared-directory>` from `services/backend`. Use the protected repository and database settings and an App installation with Contents read and write access for the selected repository. The command rechecks owner access and the pinned base, pushes the approved commit to its designated branch, and records the verified head in PostgreSQL. Repeating it with the same remote head returns the same receipt.

Run `uv run python -m refactra.publication_pr <run-id>` after the branch push. The App also needs Pull requests read and write access on the selected repository. Refactra checks the remote base and head, opens or recovers the matching draft PR, and records its URL in PostgreSQL.

Select **Refresh saved result** in the browser to see whether the branch was pushed and open the draft PR once its link is recorded.

## Read-only OpenAI analysis adapter

`execute_read_only_analysis(session_factory, run_id, attempt_id, provider)` reloads the saved context, counts the complete input through OpenAI's `/v1/responses/input_tokens` endpoint, commits durable admission, and then makes one Responses API call outside database transactions. A trusted operator must authorize repository access first. Construct `OpenAIAnalysisProvider` with the server-only `REFACTRA_OPENAI_API_KEY` from your environment; host Python commands do not load `.env` automatically. No new dependency is required.

The initial fixed profile is `gpt-5.4-mini-2026-03-17`, with reasoning disabled, a 400,000-token context window and standard uncached estimates of $0.75/$4.50 per million input/output tokens. Rates and snapshot were checked on 2026-11-19 against [model documentation](https://developers.openai.com/api/docs/models/gpt-5.4-mini) and [pricing](https://developers.openai.com/api/docs/pricing). At the existing 12k/3k request limits, the maximum estimated reservation is $0.0225. [Input counting](https://developers.openai.com/api/docs/guides/token-counting) includes instructions and the same input envelope used for generation; excessive input is rejected, not truncated.

Calls use the default service tier, `store=false`, no tools and no automatic retries or redirects. Confirmed usage settles the original reservation even for incomplete/refused responses; missing/invalid usage, unexpected model/tier, transport errors and timeouts retain the full hold. Existing receipts never authorize resending. Errors and object representations omit credentials, source and response text. Estimates do not reproduce the provider invoice, and `store=false` is not a zero-retention guarantee.

The adapter returns untrusted text. The worker saves it for review when invoked through the repository queue. Automated tests use mocked HTTP and isolated PostgreSQL schemas. Local operator checks retain their context and usage ledger in a separate `refactra_operator` schema.

## DeepSeek analysis option

`DeepSeekAnalysisProvider(api_key, tokenizer_path=Path(...))` uses the same `execute_read_only_analysis` entry point and PostgreSQL ledger with its own trusted pricing profile. Supply the server-only `REFACTRA_DEEPSEEK_API_KEY`; host Python commands do not load `.env` automatically. The hosted `deepseek-flash` alias currently identifies V4.1 Flash-1017; unlike the OpenAI snapshot, this alias is not an immutable version pin. Record the response model and evaluation date when benchmarking.

Requests go only to `https://api.deepseek.com/responses`, with reasoning disabled, no tools and no automatic retries. Unsupported OpenAI options (`store`, `service_tier`, `truncation`) are omitted; DeepSeek documents its Responses API as stateless. Confirmed input/output usage settles the reservation, including incomplete/refused responses; unknown usage or a different response model retains the hold. Rates checked on 2026-11-19 use conservative **peak, uncached** estimates of $0.30/$1.20 per million input/output tokens, at most $0.0072 for a 12k/3k request. Off-peak/cache discounts are not applied to this ledger estimate. See [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) and [Responses compatibility](https://api-docs.deepseek.com/guides/responses_api/).

DeepSeek's documented token counting uses an offline tokenizer. The adapter requires a local file matching SHA-256 `81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99`; it does not download at runtime or use character-count estimates. Download the pinned publisher asset into ignored `.runtime`:

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/deepseek-ai/deepseek-recipe/935817c66f5d6e64838f419692545f06787e485f/static/tokenizers/v41/tokenizer.json" -OutFile .runtime/deepseek-v41-tokenizer.json
```

Pass that path (the `.env.example` setting is `REFACTRA_DEEPSEEK_TOKENIZER_PATH`) to the constructor. Counting follows the publisher's [V4.1 encoding](https://github.com/deepseek-ai/deepseek-recipe/blob/935817c66f5d6e64838f419692545f06787e485f/deepseek-recipe-encoding/src/v4/dsv41.rs) for exactly one system instruction and one user text message, including five control tokens. Literal control-token spellings inside source are encoded as ordinary text. This counter does not support images, tools, thinking or conversation history. Offline checks matched the publisher's encoder on all five benchmark inputs and four additional text cases; that does not establish agreement with a future hosted model revision.

The portable `tokenizers` dependency and its lockfile are required; the 6.4 MB publisher asset stays local rather than being redistributed. Automated transport/ledger tests use a tiny synthetic tokenizer and make no paid calls. No benchmark runner, new execution endpoint or worker command is enabled by this option.

## What the interactive mock does

1. The UI submits a clearly labeled mock task with an idempotency key.
2. The API atomically inserts a run, START command and creation event.
3. A separate worker claims work using a short PostgreSQL `FOR UPDATE SKIP LOCKED` transaction and a 60-second lease.
4. The deterministic stub records its activity and stops at `NEEDS_HUMAN_REVIEW` with reason `mock_boundary`. The UI polls the persisted status/events and restores the last run after a page reload.

The interactive mock has no repository checkout, model call, plan, human approval, code edit, test execution against a target repository, or PR publication. Validation and PR fields remain null. A mock run reaching its boundary is not a completed refactoring. The worker has no tools that can perform those actions.

The initial migration implements three foundation tables: `runs`, `run_commands` and `run_events`. Artifact/approval tables and the full lifecycle will arrive with the actual workflow. LangGraph checkpoints are deliberately deferred to that milestone; activity events are not checkpoints.

The mock worker's outcome and command completion commit together, so an interrupted transaction can be retried without external side effects. The executable enforces one worker using a PostgreSQL advisory lock; expired claims can be recovered after restart. This is evidence for the mock queue, not a claim of exactly-once GitHub operations or full workflow recovery.

To see persistence directly, stop the worker with `docker compose stop worker`, submit a task, and observe its queued state. Start it with `docker compose start worker` and observe the recorded outcome.

## Checks

Backend integration tests use real PostgreSQL and migrate a unique disposable schema per test. They never truncate the application tables:

```powershell
docker compose --profile test run --rm test
docker compose run --rm api uv run --no-sync ruff check .
docker compose run --rm api uv run --no-sync ruff format --check .
```

For frontend checks and the browser smoke test, install Node 24 (see [.node-version](.node-version)), then:

```powershell
npm ci
npm run lint
npm run build
npm run typecheck
npx playwright install chromium
npm run test:e2e
```

The browser smoke tests expect the Compose stack at `http://127.0.0.1:3000`. Override `REFACTRA_WEB_URL` if needed. They create mock records in the local development database. The auth-proxy integration tests start an isolated Next.js server against a simulated API; they need the local `npm run build` output and never contact GitHub. Type checking follows the build because Next.js generates route types during the build.

For backend development outside Docker, use Python 3.12 and uv, run `uv sync --frozen` in `services/backend`, set `REFACTRA_DATABASE_URL` and `REFACTRA_ENABLE_MOCK=true`, then run `uv run alembic upgrade head`. Start `uv run uvicorn refactra.api:app --reload` and `uv run python -m refactra.worker` in separate terminals. Set `REFACTRA_TEST_DATABASE_URL` before `uv run pytest`; database tests skip explicitly when it is absent. Set environment variables in the shell; these Python commands do not automatically load the root `.env` file.

## Repository layout

- `apps/web`: Next.js App Router UI and a server-side API proxy.
- `services/backend`: shared FastAPI/worker package, Alembic migrations and PostgreSQL integration tests.
- `tests/e2e`: Playwright browser and proxy integration tests; shared helpers live in `tests/fixtures`.
- `docs/diagrams`: versioned diagrams and editable source links.

Frontend dependencies are locked in `package-lock.json`; backend dependencies are locked in `services/backend/uv.lock`. Both containers run as non-root users. These service containers are not the restricted code-execution sandbox planned for later.

## Diagrams

- [Diagrams and editable source links](docs/diagrams/README.md)

Working architecture, contracts and decision records remain local until finalized.

The project targets small, controlled Python and React/TypeScript repositories. The eventual public demo will use clearly labeled read-only replay; real execution is intended for a protected/local environment. This local interactive mock is not that public replay. Working behavior, tests, costs and deployment will be reported only when verified.
