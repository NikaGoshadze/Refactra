"""Index per-run command history for status polling."""

from alembic import op

revision = "0019_command_history_index"
down_revision = "0018_worker_heartbeats"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_commands_run_created", "run_commands", ["run_id", "created_at", "id"])


def downgrade():
    op.drop_index("ix_commands_run_created", table_name="run_commands")
