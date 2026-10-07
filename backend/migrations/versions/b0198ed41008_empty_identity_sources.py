"""Archive unused auto-seeded identity sources without deleting configuration.

Revision ID: b0198ed41008
Revises: a61269c01007
"""

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "b0198ed41008"
down_revision = "a61269c01007"
branch_labels = None
depends_on = None

# Freeze the old templates here; future templates must not change migration behavior.
TEMPLATES = [
    ("Cloudflare 博客", "rss", "https://blog.cloudflare.com/rss/"),
    ("pgvector 仓库与版本", "github", "https://github.com/pgvector/pgvector"),
    ("Black Hat 档案索引", "blackhat", "https://www.blackhat.com/html/archives.html"),
]


def upgrade():
    op.add_column(
        "sources", sa.Column("archived", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.drop_constraint("ck_source_kind", "sources", type_="check")
    op.create_check_constraint("ck_source_kind", "sources", "length(kind) > 0")
    archive_unused_templates(op.get_bind(), get_settings().user_id)


def archive_unused_templates(connection, default_id):
    # Old create_identity inserted templates in the exact same PostgreSQL transaction.
    # Skip manual/renamed/used sources and the original default identity. Preserve rows,
    # enabled flags, UUIDs, cursor and all content so recovery remains possible.
    statement = sa.text("""
        UPDATE sources AS s SET archived = true
        FROM local_identities AS i
        WHERE s.user_id = i.id AND i.id != :default_id
          AND s.created_at = i.created_at
          AND s.name = :name AND s.kind = :kind AND s.url = :url
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.source_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM ingestion_runs r WHERE r.source_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM content_occurrences o WHERE o.source_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM content_versions v WHERE v.source_id = s.id)
          AND NOT EXISTS (SELECT 1 FROM fixed_columns c
                          WHERE CAST(c.source_ids AS jsonb)
                                @> jsonb_build_array(CAST(s.id AS text)))
    """)
    for name, kind, url in TEMPLATES:
        connection.execute(
            statement, {"default_id": default_id, "name": name, "kind": kind, "url": url}
        )


def downgrade():
    # Restores visibility of archived template rows; no rows were deleted.
    op.drop_column("sources", "archived")
    op.drop_constraint("ck_source_kind", "sources", type_="check")
    op.create_check_constraint(
        "ck_source_kind", "sources", "kind IN ('rss', 'github', 'blackhat', 'github_search')"
    )
