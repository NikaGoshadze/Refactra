"""foundation runs commands events"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=False),
        sa.Column("github_repository_id", sa.BigInteger(), nullable=False),
        sa.Column("base_branch", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.String(length=40), nullable=False),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'ANALYZING', 'NEEDS_HUMAN_REVIEW', 'FAILED')",
            name="ck_runs_foundation_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_runs_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "run_commands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind = 'START'", name="ck_commands_foundation_kind"),
        sa.CheckConstraint(
            "status <> 'CLAIMED' OR (claimed_by IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_commands_claim",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'DONE', 'FAILED')", name="ck_commands_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_commands_attempts"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_commands_claimable",
        "run_commands",
        ["available_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('PENDING', 'CLAIMED')"),
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_run_id_id", "run_events", ["run_id", "id"], unique=False)


def downgrade():
    op.drop_index("ix_events_run_id_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index(
        "ix_commands_claimable",
        table_name="run_commands",
        postgresql_where=sa.text("status IN ('PENDING', 'CLAIMED')"),
    )
    op.drop_table("run_commands")
    op.drop_table("runs")
