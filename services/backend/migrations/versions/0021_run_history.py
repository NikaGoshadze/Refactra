"""Keep each run's repository name and index runs by owner for the history page."""

import sqlalchemy as sa
from alembic import op

revision = "0021_run_history"
down_revision = "0020_session_github_tokens"
branch_labels = None
depends_on = None


def upgrade():
    # Older runs keep NULL; the history shows their repository ID instead.
    op.add_column("runs", sa.Column("repository_full_name", sa.Text(), nullable=True))
    op.create_index("ix_runs_owner_created", "runs", ["requested_by", "created_at", "id"])


def downgrade():
    op.drop_index("ix_runs_owner_created", table_name="runs")
    op.drop_column("runs", "repository_full_name")
