from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from refactra.config import Settings
from refactra.db import make_engine, sessions
from refactra.models import Run, RunCommand, RunEvent
from refactra.schemas import EventPage, EventRead, MockRunCreate, MockRunRead


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
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
        description="Local mock queue only. Live execution and approvals are not implemented.",
        lifespan=lifespan,
    )

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

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_, exc: SQLAlchemyError):
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "database_unavailable",
                    "message": "The database or schema is unavailable. Check local service logs.",
                }
            },
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
            if version != "0001_foundation":
                raise HTTPException(503, {"code": "schema_not_ready", "message": "Run migrations."})
        return {"status": "ready", "database": "postgresql", "schema": version}

    @app.get("/api/v1/capabilities")
    def capabilities():
        return {
            "mock_enabled": settings.enable_mock,
            "live_execution_enabled": False,
            "approvals_enabled": False,
            "github_publication_enabled": False,
        }

    @app.post("/api/v1/runs", status_code=503)
    def live_disabled():
        raise HTTPException(
            503,
            {
                "code": "live_execution_disabled",
                "message": "Live execution is not implemented in this milestone.",
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
