"""Add server-side browser sessions; no OAuth credentials or raw session tokens."""

import sqlalchemy as sa
from alembic import op

revision = "0002_auth_sessions"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("github_user_id > 0", name="ck_auth_sessions_user"),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiry"),
    )


def downgrade():
    op.drop_table("auth_sessions")
