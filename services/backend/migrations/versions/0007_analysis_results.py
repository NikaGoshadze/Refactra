"""Store one reviewable analysis per repository run."""

import sqlalchemy as sa
from alembic import op

revision = "0007_analysis_results"
down_revision = "0006_analysis_output"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_analysis_results",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("run_analysis_contexts.run_id"), primary_key=True
        ),
        sa.Column(
            "reservation_id",
            sa.Uuid(),
            sa.ForeignKey("model_reservations.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("response_id", sa.Text(), nullable=False),
        sa.Column("analysis_text", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("run_analysis_results")
