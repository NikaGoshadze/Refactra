from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("github_user_id > 0", name="ck_auth_sessions_user"),
        CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiry"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    github_user_id: Mapped[int] = mapped_column(BigInteger)
    # Display data only; repository permissions must be checked separately.
    github_login: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CREATED', 'ANALYZING', 'NEEDS_HUMAN_REVIEW', 'FAILED')",
            name="ck_runs_foundation_status",
        ),
        CheckConstraint("version >= 1", name="ck_runs_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Zero identities and fixture revision defaults are for mock runs only.
    requested_by: Mapped[int] = mapped_column(BigInteger, default=0)
    github_installation_id: Mapped[int] = mapped_column(BigInteger, default=0)
    github_repository_id: Mapped[int] = mapped_column(BigInteger, default=0)
    base_branch: Mapped[str] = mapped_column(Text, default="mock-fixture")
    base_sha: Mapped[str] = mapped_column(String(40), default="0" * 40)
    task_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    workflow_version: Mapped[str] = mapped_column(String(32), default="mock-v0.1")
    version: Mapped[int] = mapped_column(Integer, default=1)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunAnalysisContext(Base):
    __tablename__ = "run_analysis_contexts"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' "
            "AND COALESCE(payload->>'schema_version', '') = 'analysis-context-v1'",
            name="ck_analysis_context_format",
        ),
        CheckConstraint("octet_length(payload::text) <= 2097152", name="ck_analysis_context_size"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelReservation(Base):
    __tablename__ = "model_reservations"
    __table_args__ = (
        CheckConstraint(
            "input_tokens BETWEEN 1 AND 12000 AND output_tokens BETWEEN 1 AND 6000 "
            "AND maximum_cost_usd BETWEEN 0 AND 0.75",
            name="ck_model_reservation_bounds",
        ),
        CheckConstraint(
            "num_nonnulls(actual_input_tokens, actual_output_tokens, actual_cost_usd, settled_at) "
            "IN (0, 4) "
            "AND (actual_input_tokens IS NULL OR (actual_input_tokens >= 0 "
            "AND actual_output_tokens >= 0 AND actual_cost_usd >= 0 AND pricing IS NOT NULL))",
            name="ck_model_settlement_complete",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run_analysis_contexts.run_id"), index=True)
    budget_month: Mapped[date] = mapped_column(Date, index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    maximum_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 12))
    pricing: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    actual_input_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_output_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 12))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunAnalysisResult(Base):
    __tablename__ = "run_analysis_results"

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_analysis_contexts.run_id"), primary_key=True
    )
    reservation_id: Mapped[UUID] = mapped_column(ForeignKey("model_reservations.id"), unique=True)
    response_id: Mapped[str] = mapped_column(Text)
    analysis_text: Mapped[str] = mapped_column(Text, deferred=True)


class RunPlan(Base):
    __tablename__ = "run_plans"
    __table_args__ = (
        UniqueConstraint("run_id", "revision", name="uq_run_plans_revision"),
        CheckConstraint("revision > 0", name="ck_run_plans_revision"),
        CheckConstraint("octet_length(document) BETWEEN 1 AND 65536", name="ck_run_plans_size"),
        CheckConstraint("sha256 = encode(sha256(document), 'hex')", name="ck_run_plans_hash"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run_analysis_results.run_id"))
    revision: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunPlanApproval(Base):
    __tablename__ = "run_plan_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "revision"],
            ["run_plans.run_id", "run_plans.revision"],
            name="fk_run_plan_approvals_revision",
        ),
        CheckConstraint("approved_by > 0", name="ck_run_plan_approvals_actor"),
    )

    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[int] = mapped_column(BigInteger)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RunGeneratedCandidate(Base):
    __tablename__ = "run_generated_candidates"
    __table_args__ = (
        CheckConstraint(
            "octet_length(document) BETWEEN 1 AND 1048576", name="ck_generated_candidate_size"
        ),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("run_plan_approvals.run_id"), primary_key=True)
    reservation_id: Mapped[UUID] = mapped_column(ForeignKey("model_reservations.id"), unique=True)
    document: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)


class RunCandidateResult(Base):
    __tablename__ = "run_candidate_results"
    __table_args__ = (
        UniqueConstraint("run_id", "revision", name="uq_candidate_results_revision"),
        UniqueConstraint("run_id", "candidate_sha256", name="uq_candidate_results_digest"),
        CheckConstraint("revision > 0", name="ck_candidate_results_revision"),
        CheckConstraint(
            "octet_length(document) BETWEEN 1 AND 2097152", name="ck_candidate_results_size"
        ),
        CheckConstraint(
            "sha256 = encode(sha256(document), 'hex')", name="ck_candidate_results_hash"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run_plan_approvals.run_id"))
    revision: Mapped[int] = mapped_column(Integer)
    candidate_sha256: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunPublicationTarget(Base):
    __tablename__ = "run_publication_targets"
    __table_args__ = (
        CheckConstraint(
            "octet_length(document) BETWEEN 1 AND 8192", name="ck_publication_targets_size"
        ),
        CheckConstraint(
            "sha256 = encode(sha256(document), 'hex')", name="ck_publication_targets_hash"
        ),
    )

    result_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_candidate_results.id"), primary_key=True
    )
    sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunCommand(Base):
    __tablename__ = "run_commands"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('START', 'APPLY_PLAN_APPROVAL', 'GENERATE_CANDIDATE', "
            "'VALIDATE_CANDIDATE', 'PUBLISH_CANDIDATE')",
            name="ck_commands_kind",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'DONE', 'FAILED')", name="ck_commands_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_commands_attempts"),
        CheckConstraint(
            "status <> 'CLAIMED' OR (claimed_by IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_commands_claim",
        ),
        Index(
            "ix_commands_claimable",
            "available_at",
            "created_at",
            postgresql_where=text("status IN ('PENDING', 'CLAIMED')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"))
    kind: Mapped[str] = mapped_column(String(32), default="START")
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_events_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
