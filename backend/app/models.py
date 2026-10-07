"""Source identity, provenance and immutable content snapshots for the first milestone."""

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Identity:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)


class LocalIdentity(Base):
    """Local workspace label, not an authenticated account."""

    __tablename__ = "local_identities"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Source(Identity, Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "url", name="uq_source_identity"),
        CheckConstraint("length(kind) > 0", name="ck_source_kind"),
    )
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False)
    cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Identity, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','retrying','succeeded','failed')", name="ck_job_status"
        ),
        Index(
            "uq_active_source_job",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','retrying')"),
        ),
    )
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionRun(Identity, Base):
    __tablename__ = "ingestion_runs"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class ContentItem(Identity, Base):
    __tablename__ = "content_items"
    __table_args__ = (UniqueConstraint("user_id", "canonical_url", name="uq_item_url"),)
    canonical_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(30))
    language: Mapped[str] = mapped_column(String(20), default="unknown")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    candidate_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now() + interval '30 days'")
    )


class ContentOccurrence(Identity, Base):
    __tablename__ = "content_occurrences"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_occurrence_identity"),)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("content_items.id"), index=True)
    external_id: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContentVersion(Identity, Base):
    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint("item_id", "version_number", name="uq_item_version"),
        CheckConstraint(
            "acquisition_level IN ('metadata_only','abstract_only','full_text')",
            name="ck_acquisition_level",
        ),
    )
    item_id: Mapped[UUID] = mapped_column(ForeignKey("content_items.id"), index=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acquisition_level: Mapped[str] = mapped_column(String(20))
    clean_text: Mapped[str] = mapped_column(Text, default="")
    body_hash: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    parser_version: Mapped[str] = mapped_column(String(30), default="v1")


class LongTermProfile(Identity, Base):
    __tablename__ = "long_term_profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_long_term_profile_user"),)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    field_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProfileRevision(Identity, Base):
    __tablename__ = "profile_revisions"
    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_profile_revision"),)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("long_term_profiles.id"))
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    field_metadata: Mapped[dict] = mapped_column(JSON)
    changed_fields: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FixedColumn(Identity, Base):
    __tablename__ = "fixed_columns"
    __table_args__ = (
        UniqueConstraint("user_id", "name_key", name="uq_fixed_column_name"),
        CheckConstraint("items_per_day BETWEEN 1 AND 10", name="ck_column_items"),
    )
    name: Mapped[str] = mapped_column(String(100))
    name_key: Mapped[str] = mapped_column(String(100))
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    content_types: Mapped[list] = mapped_column(JSON, default=list)
    items_per_day: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmbeddingSpace(Identity, Base):
    __tablename__ = "embedding_spaces"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_embedding_space"),)
    key: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(20))
    descriptor: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmbeddingChunk(Identity, Base):
    __tablename__ = "embedding_chunks"
    __table_args__ = (
        UniqueConstraint("space_id", "version_id", "ordinal", name="uq_embedding_chunk"),
    )
    space_id: Mapped[UUID] = mapped_column(ForeignKey("embedding_spaces.id"), index=True)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("content_items.id"), index=True)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("content_versions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    passage: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSON)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmbeddingJob(Identity, Base):
    __tablename__ = "embedding_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed')", name="ck_embed_job"),
        Index(
            "uq_active_embedding_job",
            "space_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
    )
    space_id: Mapped[UUID] = mapped_column(ForeignKey("embedding_spaces.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    max_items: Mapped[int] = mapped_column(Integer)
    indexed_items: Mapped[int] = mapped_column(Integer, default=0)
    indexed_chunks: Mapped[int] = mapped_column(Integer, default=0)
    reported_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Favorite(Identity, Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_favorite_item"),)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("content_items.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    saved_version_id: Mapped[UUID] = mapped_column(ForeignKey("content_versions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UpdateNotice(Identity, Base):
    __tablename__ = "update_notices"
    __table_args__ = (UniqueConstraint("favorite_id", "event_key", name="uq_update_notice"),)
    favorite_id: Mapped[UUID] = mapped_column(ForeignKey("favorites.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(100))
    item_id: Mapped[UUID] = mapped_column(ForeignKey("content_items.id"))
    version_id: Mapped[UUID] = mapped_column(ForeignKey("content_versions.id"))
    previous_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("content_versions.id"))
    kind: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiscoverySettings(Identity, Base):
    __tablename__ = "discovery_settings"
    __table_args__ = (UniqueConstraint("user_id", name="uq_discovery_settings"),)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DiscoveryRun(Identity, Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "schedule_key", name="uq_discovery_day"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','partial','failed')",
            name="ck_discovery_status",
        ),
        Index(
            "uq_active_discovery",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
    )
    schedule_key: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Recommendation(Identity, Base):
    __tablename__ = "recommendations"
    __table_args__ = (UniqueConstraint("run_id", "item_id", name="uq_recommendation_item"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("discovery_runs.id"), index=True)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("content_items.id"))
    version_id: Mapped[UUID] = mapped_column(ForeignKey("content_versions.id"))
    position: Mapped[int] = mapped_column(Integer)
    score: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    column_name: Mapped[str | None] = mapped_column(String(100))
