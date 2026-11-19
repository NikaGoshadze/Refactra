"""Preserve rate snapshots and reconcile confirmed usage; legacy holds remain unknown."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_model_usage"
down_revision = "0004_model_reservations"
branch_labels = None
depends_on = None


def upgrade():
    for name, column_type in (
        ("pricing", JSONB(none_as_null=True)),
        ("actual_input_tokens", sa.Integer()),
        ("actual_output_tokens", sa.Integer()),
        ("actual_cost_usd", sa.Numeric(18, 12)),
        ("settled_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("model_reservations", sa.Column(name, column_type, nullable=True))
    op.create_check_constraint(
        "ck_model_settlement_complete",
        "model_reservations",
        "num_nonnulls(actual_input_tokens, actual_output_tokens, actual_cost_usd, settled_at) "
        "IN (0, 4) "
        "AND (actual_input_tokens IS NULL OR (actual_input_tokens >= 0 "
        "AND actual_output_tokens >= 0 AND actual_cost_usd >= 0 AND pricing IS NOT NULL))",
    )


def downgrade():
    op.drop_constraint("ck_model_settlement_complete", "model_reservations", type_="check")
    for name in (
        "settled_at",
        "actual_cost_usd",
        "actual_output_tokens",
        "actual_input_tokens",
        "pricing",
    ):
        op.drop_column("model_reservations", name)
