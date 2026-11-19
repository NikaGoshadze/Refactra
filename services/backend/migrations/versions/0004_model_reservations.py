"""Record held model budgets before any provider transport."""

import sqlalchemy as sa
from alembic import op

revision = "0004_model_reservations"
down_revision = "0003_analysis_contexts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("run_analysis_contexts.run_id"), nullable=False
        ),
        sa.Column("budget_month", sa.Date(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("maximum_cost_usd", sa.Numeric(18, 12), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "input_tokens BETWEEN 1 AND 12000 AND output_tokens BETWEEN 1 AND 3000 "
            "AND maximum_cost_usd BETWEEN 0 AND 0.75",
            name="ck_model_reservation_bounds",
        ),
    )
    op.create_index("ix_model_reservations_run_id", "model_reservations", ["run_id"])
    op.create_index("ix_model_reservations_budget_month", "model_reservations", ["budget_month"])


def downgrade():
    op.drop_table("model_reservations")
