"""Store the exact commit and destination awaiting publication review."""

import sqlalchemy as sa
from alembic import op

revision = "0012_publication_targets"
down_revision = "0011_candidate_results"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_publication_targets",
        sa.Column(
            "result_id", sa.Uuid(), sa.ForeignKey("run_candidate_results.id"), primary_key=True
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "octet_length(document) BETWEEN 1 AND 8192", name="ck_publication_targets_size"
        ),
        sa.CheckConstraint(
            "sha256 = encode(sha256(document), 'hex')", name="ck_publication_targets_hash"
        ),
    )


def downgrade():
    op.drop_table("run_publication_targets")
