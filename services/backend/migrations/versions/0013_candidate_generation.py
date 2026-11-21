"""Queue candidate generation and persist successful model patches."""

import sqlalchemy as sa
from alembic import op

revision = "0013_candidate_generation"
down_revision = "0012_publication_targets"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind",
        "run_commands",
        "kind IN ('START', 'APPLY_PLAN_APPROVAL', 'GENERATE_CANDIDATE')",
    )
    op.create_table(
        "run_generated_candidates",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("run_plan_approvals.run_id"), primary_key=True
        ),
        sa.Column(
            "reservation_id",
            sa.Uuid(),
            sa.ForeignKey("model_reservations.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "octet_length(document) BETWEEN 1 AND 1048576", name="ck_generated_candidate_size"
        ),
    )


def downgrade():
    op.drop_table("run_generated_candidates")
    op.execute("DELETE FROM run_commands WHERE kind = 'GENERATE_CANDIDATE'")
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind", "run_commands", "kind IN ('START', 'APPLY_PLAN_APPROVAL')"
    )
