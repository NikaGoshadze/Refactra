"""Allow explicit read-only analysis reservations up to 6,000 output tokens."""

from alembic import op

revision = "0006_analysis_output"
down_revision = "0005_model_usage"
branch_labels = None
depends_on = None


def _replace_bounds(output_limit: int):
    name = "ck_model_reservation_bounds"
    op.drop_constraint(name, "model_reservations", type_="check")
    op.create_check_constraint(
        name,
        "model_reservations",
        f"input_tokens BETWEEN 1 AND 12000 AND output_tokens BETWEEN 1 AND {output_limit} "
        "AND maximum_cost_usd BETWEEN 0 AND 0.75",
    )


def upgrade():
    _replace_bounds(6000)


def downgrade():
    # PostgreSQL rejects the downgrade if existing receipts exceed the old ceiling.
    _replace_bounds(3000)
