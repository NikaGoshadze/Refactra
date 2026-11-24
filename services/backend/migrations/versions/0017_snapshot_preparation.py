"""Queue source snapshot preparation for the worker instead of the HTTP request."""

from alembic import op

revision = "0017_snapshot_preparation"
down_revision = "0016_initial_plan_queue"
branch_labels = None
depends_on = None

EXISTING_KINDS = (
    "'START', 'APPLY_PLAN_APPROVAL', 'GENERATE_CANDIDATE', "
    "'VALIDATE_CANDIDATE', 'PUBLISH_CANDIDATE', 'GENERATE_PLAN'"
)


def upgrade():
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind", "run_commands", f"kind IN ({EXISTING_KINDS}, 'PREPARE_SNAPSHOT')"
    )


def downgrade():
    op.execute("DELETE FROM run_commands WHERE kind = 'PREPARE_SNAPSHOT'")
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint("ck_commands_kind", "run_commands", f"kind IN ({EXISTING_KINDS})")
