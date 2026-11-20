"""Queue approved plans for the worker, including previously recorded approvals."""

from alembic import op

revision = "0010_plan_approval_queue"
down_revision = "0009_plan_approvals"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_commands_foundation_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind", "run_commands", "kind IN ('START', 'APPLY_PLAN_APPROVAL')"
    )
    op.execute(
        """
        INSERT INTO run_commands (id, run_id, kind, status, dedupe_key, attempts)
        SELECT gen_random_uuid(), run_id, 'APPLY_PLAN_APPROVAL', 'PENDING',
               'repository:plan-approval:' || run_id::text || ':' || revision::text, 0
        FROM run_plan_approvals
        ON CONFLICT (dedupe_key) DO NOTHING
        """
    )


def downgrade():
    op.execute("DELETE FROM run_commands WHERE kind = 'APPLY_PLAN_APPROVAL'")
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint("ck_commands_foundation_kind", "run_commands", "kind = 'START'")
