"""Invites and the waitlist: one access row per GitHub user."""

import sqlalchemy as sa
from alembic import op

revision = "0022_user_access"
down_revision = "0021_run_history"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_access",
        sa.Column("github_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("github_login", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("github_user_id > 0", name="ck_user_access_user"),
        sa.CheckConstraint(
            "status IN ('invited', 'waitlisted', 'removed')", name="ck_user_access_status"
        ),
    )
    op.create_index("ix_user_access_status_requested", "user_access", ["status", "requested_at"])


def downgrade():
    op.drop_index("ix_user_access_status_requested", table_name="user_access")
    op.drop_table("user_access")
