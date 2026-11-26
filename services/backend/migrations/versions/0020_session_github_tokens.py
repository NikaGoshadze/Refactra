"""Keep the signed-in user's encrypted GitHub token with the session."""

import sqlalchemy as sa
from alembic import op

revision = "0020_session_github_tokens"
down_revision = "0019_command_history_index"
branch_labels = None
depends_on = None

COLUMNS = (
    ("github_token_ciphertext", sa.LargeBinary()),
    ("github_token_expires_at", sa.DateTime(timezone=True)),
    ("github_refresh_ciphertext", sa.LargeBinary()),
    ("github_refresh_expires_at", sa.DateTime(timezone=True)),
)


def upgrade():
    for name, column_type in COLUMNS:
        op.add_column("auth_sessions", sa.Column(name, column_type, nullable=True))


def downgrade():
    for name, _ in reversed(COLUMNS):
        op.drop_column("auth_sessions", name)
