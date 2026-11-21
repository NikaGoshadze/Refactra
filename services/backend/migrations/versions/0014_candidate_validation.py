"""Queue validation for generated candidates."""

from alembic import op

revision = "0014_candidate_validation"
down_revision = "0013_candidate_generation"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind",
        "run_commands",
        "kind IN ('START', 'APPLY_PLAN_APPROVAL', 'GENERATE_CANDIDATE', 'VALIDATE_CANDIDATE')",
    )
    op.execute("""
        INSERT INTO run_commands (id, run_id, kind, status, dedupe_key, attempts)
        SELECT gen_random_uuid(), a.run_id, 'VALIDATE_CANDIDATE', 'PENDING',
               'repository:candidate-validation:' || a.run_id::text || ':' || a.revision::text, 0
        FROM run_generated_candidates g JOIN run_plan_approvals a ON a.run_id = g.run_id
        ON CONFLICT (dedupe_key) DO NOTHING
    """)


def downgrade():
    op.execute("DELETE FROM run_commands WHERE kind = 'VALIDATE_CANDIDATE'")
    op.drop_constraint("ck_commands_kind", "run_commands", type_="check")
    op.create_check_constraint(
        "ck_commands_kind",
        "run_commands",
        "kind IN ('START', 'APPLY_PLAN_APPROVAL', 'GENERATE_CANDIDATE')",
    )
