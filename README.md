# Refactra

Autonomous repository refactoring and draft pull-request automation with five sequential agent roles and two human approvals.

Current stage: Phase 1 — Architecture & Foundation. A local mock skeleton connects a Next.js UI, FastAPI, PostgreSQL and a separate command worker. The real agent workflow is not implemented or deployed.

## Run locally

Install Docker Desktop with Linux containers (WSL2 on Windows), then run from the repository root:

```powershell
docker compose up --build -d
```

Open [the local workspace](http://localhost:3000). The [API docs](http://localhost:8000/docs) and [readiness endpoint](http://localhost:8000/health/ready) are available separately.

Compose waits for PostgreSQL, applies Alembic migrations, then starts the API and worker. Default development credentials and ports are in [.env.example](.env.example); copying it to `.env` is optional for Compose. These credentials and this unauthenticated mock stack are for local development only. Published ports bind to loopback; do not expose this build as the public demo.

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
docker compose up --build -d
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
docker compose -f compose.yaml -f compose.repository-api.yaml up --build -d --wait api
```

This adds the repository settings and a read-only PEM mount to the API only, while preserving its database and OAuth settings. Missing required environment values stop Compose configuration; a missing key stops container creation without creating a replacement directory. Other services receive no new settings or key mounts. Use both `-f` files on later `up` commands to keep protected repository access enabled. The base `docker compose up` command still needs no PEM and recreates the API without this opt-in configuration. Compose merges the override with the base file; see [Docker's merge rules](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/).

Sign in at `http://localhost:3000`, then use the Repository connection panel, or open `http://localhost:3000/api/v1/repositories/1001132861` in that same browser for AI-Hub through the web proxy. The direct API remains at `http://localhost:8000/api/v1/repositories/1001132861`. Use `localhost` consistently so the browser sends the local session cookie. A successful request returns only repository metadata with `access: "read-only"`; an anonymous request returns 401, and a disallowed user/repository returns 403. No tokens or PEM contents appear in the response. Use the configured web/API ports if they differ from 3000/8000, and rebuild the web service to apply UI/proxy changes.

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

Repository records use `repository-v0.1`, separate from mock runs, and enqueue no worker command. The live-run endpoint remains disabled. Later source retrieval must load `base_sha` from PostgreSQL rather than resolve the branch again. API/UI wiring and bounded source retrieval remain later batches.

## What this milestone does

1. The UI submits a clearly labeled mock task with an idempotency key.
2. The API atomically inserts a run, START command and creation event.
3. A separate worker claims work using a short PostgreSQL `FOR UPDATE SKIP LOCKED` transaction and a 60-second lease.
4. The deterministic stub records its activity and stops at `NEEDS_HUMAN_REVIEW` with reason `mock_boundary`. The UI polls the persisted status/events and restores the last run after a page reload.

There is no repository checkout, model call, plan, human approval, code edit, test execution against a target repository, or PR publication. Validation and PR fields remain null. A mock run reaching its boundary is not a completed refactoring. The worker has no tools that can perform those actions.

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
