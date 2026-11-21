"""Allow new publication approvals to enqueue a worker command."""

from alembic import op

revision = "0015_candidate_publication"
down_revision = "0014_candidate_validation"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind",
        "run_commands",
        "kind IN ('START', 'APPLY_PLAN_APPROVAL', 'GENERATE_CANDIDATE', "
        "'VALIDATE_CANDIDATE', 'PUBLISH_CANDIDATE')",
    )


def downgrade():
    op.execute("DELETE FROM run_commands WHERE kind = 'PUBLISH_CANDIDATE'")
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind",
        "run_commands",
        "kind IN ('START', 'APPLY_PLAN_APPROVAL', 'GENERATE_CANDIDATE', 'VALIDATE_CANDIDATE')",
    )
