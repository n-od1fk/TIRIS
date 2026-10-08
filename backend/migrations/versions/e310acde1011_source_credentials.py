"""Source-scoped encrypted credentials; no migration of existing environment secrets."""

import sqlalchemy as sa
from alembic import op

revision = "e310acde1011"
down_revision = "d209acde1010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sources",
        sa.Column("credential_mode", sa.String(16), nullable=False, server_default="legacy_env"),
    )
    op.add_column("sources", sa.Column("credentials_ciphertext", sa.Text(), nullable=True))
    op.add_column(
        "source_discoveries", sa.Column("credentials_ciphertext", sa.Text(), nullable=True)
    )


def downgrade():
    op.drop_column("source_discoveries", "credentials_ciphertext")
    op.drop_column("sources", "credentials_ciphertext")
    op.drop_column("sources", "credential_mode")
