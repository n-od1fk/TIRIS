"""Local identity labels, no login credentials.

Revision ID: a61269c01007
Revises: c21824acb819
"""

import sqlalchemy as sa
from alembic import op

revision = "a61269c01007"
down_revision = "c21824acb819"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "local_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("local_identities")
