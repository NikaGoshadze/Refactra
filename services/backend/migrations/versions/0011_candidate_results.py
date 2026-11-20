"""Store immutable validated candidate artifacts."""

import sqlalchemy as sa
from alembic import op

revision = "0011_candidate_results"
down_revision = "0010_plan_approval_queue"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_candidate_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("run_plan_approvals.run_id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("run_id", "revision", name="uq_candidate_results_revision"),
        sa.UniqueConstraint("run_id", "candidate_sha256", name="uq_candidate_results_digest"),
        sa.CheckConstraint("revision > 0", name="ck_candidate_results_revision"),
        sa.CheckConstraint(
            "octet_length(document) BETWEEN 1 AND 2097152", name="ck_candidate_results_size"
        ),
        sa.CheckConstraint(
            "sha256 = encode(sha256(document), 'hex')", name="ck_candidate_results_hash"
        ),
    )


def downgrade():
    op.drop_table("run_candidate_results")
