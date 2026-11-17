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
