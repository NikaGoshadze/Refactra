import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from refactra.access_logs import install_auth_query_redaction
from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.diagnostics import database_error_details, workflow_log
from refactra.github_auth import create_github_auth_router
from refactra.models import Run, RunCommand, RunEvent
from refactra.production import require_production_ready
from refactra.repository_routes import create_repository_router
from refactra.request_limits import BodyLimitMiddleware
from refactra.run_access import RepositoryAuthorizer
from refactra.run_history import create_run_history_router
from refactra.schemas import EventPage, EventRead, MockRunCreate, MockRunRead
from refactra.session_routes import create_session_router
from refactra.user_repository_routes import create_user_repository_router
from refactra.worker_health import (
    ANALYSIS_WORKER_MODES,
    PUBLICATION_WORKER_MODES,
    worker_status,
)

REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    *,
    repository_authorizer: RepositoryAuthorizer | None = None,
) -> FastAPI:
    install_auth_query_redaction()
    # In production, unsafe settings stop the API here instead of on a user's first request.
    environment = require_production_ready("api")
    settings = settings or Settings.from_env()
    owns_engine = engine is None
    engine = engine or make_engine(settings.database_url)
    session_factory = sessions(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owns_engine:
            engine.dispose()

    app = FastAPI(
        title="Refactra Foundation API",
        version="0.1.0",
        description="Local mock queue and optional protected repository analysis.",
        lifespan=lifespan,
    )
    app.include_router(create_github_auth_router(session_factory))
    app.include_router(create_session_router(session_factory))
    app.include_router(
        create_repository_router(
            session_factory,
            enable_analysis=settings.enable_repository_analysis,
            authorizer=repository_authorizer,
        )
    )
    app.include_router(create_user_repository_router(session_factory))
    app.include_router(create_run_history_router(session_factory))

    @app.exception_handler(HTTPException)
    async def http_error(_, exc: HTTPException):
        detail = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {"code": "request_error", "message": str(exc.detail)}
        )
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Check the request fields, path and Idempotency-Key header.",
                }
            },
        )

    # Registered before request_id so request_id stays outermost and tags rejections too.
    app.add_middleware(BodyLimitMiddleware)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        # Correlates a response with its log events; supplied IDs must be log-safe.
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = supplied if REQUEST_ID.fullmatch(supplied) else uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        details = database_error_details(exc)
        request_id = getattr(request.state, "request_id", None)
        route = request.scope.get("route")
        workflow_log(
            "database_error",
            warning=True,
            request_id=request_id,
            method=request.method,
            route=getattr(route, "path", None),
            **details,
        )
        if details["category"] in {"integrity", "data", "programming"}:
            # A bug or invalid data; retrying will not help.
            status, code = 500, "database_error"
            message = "The request failed on a database error. Report the request ID."
        else:
            status, code = 503, "database_unavailable"
            message = "The database is unavailable or busy. Retry shortly."
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "request_id": request_id}},
        )

    def require_mock():
        if not settings.enable_mock:
            raise HTTPException(503, {"code": "mock_disabled", "message": "Mock mode is disabled."})

    def get_run(session, run_id: UUID) -> Run:
        run = session.get(Run, run_id)
        if run is None or run.workflow_version != "mock-v0.1":
            raise HTTPException(404, {"code": "not_found", "message": "Mock run not found."})
        return run

    def read_run(session, run: Run) -> MockRunRead:
        terminal = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.event_type == "RUN_STOPPED")
            .order_by(RunEvent.id.desc())
            .limit(1)
        )
        return MockRunRead(
            run_id=run.id,
            status=run.status,
            task_text=run.task_text,
            version=run.version,
            workflow_version=run.workflow_version,
            created_at=run.created_at,
            updated_at=run.updated_at,
            stop_reason=terminal.payload if terminal else None,
            status_url=f"/api/v1/mock/runs/{run.id}",
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            if version != "0021_run_history":
                raise HTTPException(503, {"code": "schema_not_ready", "message": "Run migrations."})
        return {"status": "ready", "database": "postgresql", "schema": version}

    @app.get("/health/worker")
    def worker_health(response: Response):
        """Liveness of the execution worker and the claimable backlog; 503 needs attention."""
        with session_factory() as session:
            status = worker_status(session)
        response.headers["Cache-Control"] = "no-store"
        if not status.healthy:
            response.status_code = 503
        return asdict(status)

    @app.get("/api/v1/capabilities")
    def capabilities():
        """What this deployment accepts, and whether a worker is available to act on it."""
        try:
            with session_factory() as session:
                worker = worker_status(session)
            status, mode = worker.status, worker.mode
        except SQLAlchemyError:
            status, mode = "unknown", None
        running = status == "running"
        return {
            "environment": environment,
            "mock_enabled": settings.enable_mock,
            "repository_analysis_enabled": settings.enable_repository_analysis,
            # Plan, waiver and publication approval routes are always served for existing runs.
            "approvals_enabled": True,
            "worker": {"status": status, "mode": mode},
            "analysis_worker_available": running and mode in ANALYSIS_WORKER_MODES,
            "publication_worker_available": running and mode in PUBLICATION_WORKER_MODES,
        }

    if not settings.enable_repository_analysis:

        @app.post("/api/v1/runs", status_code=503)
        def live_disabled():
            raise HTTPException(
                503,
                {
                    "code": "live_execution_disabled",
                    "message": "Repository analysis submission is disabled.",
                },
            )

    @app.post("/api/v1/mock/runs", response_model=MockRunRead, status_code=202)
    def submit_mock(
        body: MockRunCreate,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ):
        require_mock()
        dedupe_key = f"mock:local:{idempotency_key}"

        def existing(session):
            command = session.scalar(select(RunCommand).where(RunCommand.dedupe_key == dedupe_key))
            if command is None:
                return None
            run = get_run(session, command.run_id)
            if run.task_text != body.task_text:
                raise HTTPException(
                    409,
                    {
                        "code": "idempotency_conflict",
                        "message": "This key was used for a different task.",
                    },
                )
            return run

        with session_factory() as session:
            try:
                with session.begin():
                    run = existing(session)
                    if run is None:
                        run = Run(task_text=body.task_text)
                        session.add(run)
                        session.flush()
                        session.add(RunCommand(run_id=run.id, dedupe_key=dedupe_key))
                        session.add(
                            RunEvent(
                                run_id=run.id,
                                event_type="RUN_CREATED",
                                payload={
                                    "mode": "mock",
                                    "message": "Mock task saved with a START command.",
                                },
                            )
                        )
                    else:
                        response.status_code = 200
            except IntegrityError:
                # A concurrent retry may have won the unique dedupe key; its transaction is durable.
                session.rollback()
                run = existing(session)
                if run is None:
                    raise
                response.status_code = 200
            response.headers["Location"] = f"/api/v1/mock/runs/{run.id}"
            return read_run(session, run)

    @app.get("/api/v1/mock/runs/{run_id}", response_model=MockRunRead)
    def mock_status(run_id: UUID):
        require_mock()
        with session_factory() as session:
            return read_run(session, get_run(session, run_id))

    @app.get("/api/v1/mock/runs/{run_id}/events", response_model=EventPage)
    def mock_events(
        run_id: UUID,
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        require_mock()
        with session_factory() as session:
            get_run(session, run_id)
            rows = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.id > after_id)
                .order_by(RunEvent.id)
                .limit(limit + 1)
            ).all()
            page = rows[:limit]
            return EventPage(
                items=[EventRead.model_validate(row) for row in page],
                next_after_id=page[-1].id if page else after_id,
                has_more=len(rows) > limit,
            )

    return app


app = create_app()
