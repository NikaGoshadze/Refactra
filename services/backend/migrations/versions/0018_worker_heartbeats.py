"""Record the execution worker's heartbeat for supervision and stalled-queue checks."""

import sqlalchemy as sa
from alembic import op

revision = "0018_worker_heartbeats"
down_revision = "0017_snapshot_preparation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_heartbeats",
        sa.Column("slot", sa.String(32), primary_key=True),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("worker_heartbeats")
