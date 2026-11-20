"""Store bounded refactoring plan revisions and their exact content hashes."""

import sqlalchemy as sa
from alembic import op

revision = "0008_run_plans"
down_revision = "0007_analysis_results"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("run_analysis_results.run_id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("run_id", "revision", name="uq_run_plans_revision"),
        sa.CheckConstraint("revision > 0", name="ck_run_plans_revision"),
        sa.CheckConstraint("octet_length(document) BETWEEN 1 AND 65536", name="ck_run_plans_size"),
        sa.CheckConstraint("sha256 = encode(sha256(document), 'hex')", name="ck_run_plans_hash"),
    )


def downgrade():
    op.drop_table("run_plans")
