"""Persist one bounded structured analysis input context per run."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_analysis_contexts"
down_revision = "0002_auth_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_analysis_contexts",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' "
            "AND COALESCE(payload->>'schema_version', '') = 'analysis-context-v1'",
            name="ck_analysis_context_format",
        ),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 2097152", name="ck_analysis_context_size"
        ),
    )


def downgrade():
    op.drop_table("run_analysis_contexts")
