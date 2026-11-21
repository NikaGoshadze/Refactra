"""Allow completed repository analyses to enqueue their initial plan."""

from alembic import op

revision = "0016_initial_plan_queue"
down_revision = "0015_candidate_publication"
branch_labels = None
depends_on = None

EXISTING_KINDS = (
    "'START', 'APPLY_PLAN_APPROVAL', 'GENERATE_CANDIDATE', "
    "'VALIDATE_CANDIDATE', 'PUBLISH_CANDIDATE'"
)


def upgrade():
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind", "run_commands", f"kind IN ({EXISTING_KINDS}, 'GENERATE_PLAN')"
    )


def downgrade():
    op.execute("DELETE FROM run_commands WHERE kind = 'GENERATE_PLAN'")
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint("ck_commands_kind", "run_commands", f"kind IN ({EXISTING_KINDS})")
