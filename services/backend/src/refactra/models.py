from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
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
    # These are controlled mock identities, never authenticated GitHub identities.
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


class RunCommand(Base):
    __tablename__ = "run_commands"
    __table_args__ = (
        CheckConstraint("kind = 'START'", name="ck_commands_foundation_kind"),
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
