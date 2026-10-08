"""Original platform files.

Revision ID: c102acde1009
Revises: b0198ed41008
"""

import sqlalchemy as sa
from alembic import op

revision = "c102acde1009"
down_revision = "b0198ed41008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "original_materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["version_id"], ["content_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "filename", "sha256", name="uq_original_material"),
    )
    op.create_index("ix_original_materials_user_id", "original_materials", ["user_id"])
    op.create_index("ix_original_materials_version_id", "original_materials", ["version_id"])


def downgrade():
    op.drop_table("original_materials")
