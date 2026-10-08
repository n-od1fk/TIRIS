"""Dynamic input/fetch configuration and short-lived discovery drafts."""

import sqlalchemy as sa
from alembic import op

revision = "d209acde1010"
down_revision = "c102acde1009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sources", sa.Column("input_url", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "sources", sa.Column("access_type", sa.String(20), nullable=False, server_default="legacy")
    )
    op.add_column("sources", sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "sources", sa.Column("connection_version", sa.Integer(), nullable=False, server_default="1")
    )
    op.execute("UPDATE sources SET input_url = url, access_type = kind")
    op.create_table(
        "source_discoveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_source_discoveries_user_id", "source_discoveries", ["user_id"])


def downgrade():
    op.drop_table("source_discoveries")
    for column in ("connection_version", "validated_at", "access_type", "input_url"):
        op.drop_column("sources", column)
