"""Record the owner's approval of an exact plan revision."""

import sqlalchemy as sa
from alembic import op

revision = "0009_plan_approvals"
down_revision = "0008_run_plans"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_plan_approvals",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "approved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "revision"],
            ["run_plans.run_id", "run_plans.revision"],
            name="fk_run_plan_approvals_revision",
        ),
        sa.CheckConstraint("approved_by > 0", name="ck_run_plan_approvals_actor"),
    )


def downgrade():
    op.drop_table("run_plan_approvals")
