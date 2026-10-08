import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select, text

from app.models import ContentItem, ContentOccurrence, ContentVersion, OriginalMaterial
from app.services.ingestion import _store_one, content_external_id


def store_document(session, source, document):
    document.prepare()
    lock_key = int.from_bytes(
        hashlib.sha256(str(source.user_id).encode()).digest()[:8], "big", signed=True
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    stats = SimpleNamespace(new_count=0, updated_count=0, unchanged_count=0)
    _store_one(session, source, document.item, datetime.now(UTC), stats)
    session.flush()
    occurrence = session.scalar(
        select(ContentOccurrence).where(
            ContentOccurrence.user_id == source.user_id,
            ContentOccurrence.source_id == source.id,
            ContentOccurrence.external_id == content_external_id(source, document.item.external_id),
        )
    )
    version = session.scalar(
        select(ContentVersion)
        .where(
            ContentVersion.user_id == source.user_id,
            ContentVersion.item_id == occurrence.item_id,
            ContentVersion.source_id == source.id,
        )
        .order_by(ContentVersion.version_number.desc())
        .limit(1)
    )
    # An explicit new read renews candidate access even when the bytes are unchanged.
    item = session.get(ContentItem, version.item_id)
    item.candidate_expires_at = datetime.now(UTC) + timedelta(days=30)
    for material in document.materials:
        description = material.describe()
        exists = session.scalar(
            select(OriginalMaterial.id).where(
                OriginalMaterial.version_id == version.id,
                OriginalMaterial.filename == material.filename,
                OriginalMaterial.sha256 == description["sha256"],
            )
        )
        if not exists:
            session.add(
                OriginalMaterial(
                    user_id=source.user_id, version_id=version.id, data=material.data, **description
                )
            )
    session.flush()
    source.last_success_at, source.last_error = datetime.now(UTC), None
    return version


def document_response(session, version):
    item = session.get(ContentItem, version.item_id)
    materials = session.execute(
        select(
            OriginalMaterial.id,
            OriginalMaterial.filename,
            OriginalMaterial.media_type,
            OriginalMaterial.sha256,
            OriginalMaterial.size,
            OriginalMaterial.original_url,
        )
        .where(
            OriginalMaterial.version_id == version.id,
            OriginalMaterial.user_id == version.user_id,
        )
        .order_by(OriginalMaterial.filename)
    ).all()
    return {
        "item_id": str(item.id),
        "version_id": str(version.id),
        "title": version.title,
        "original_url": item.canonical_url,
        "body": version.clean_text,
        "author": version.author,
        "published_at": version.published_at,
        "fetched_at": version.fetched_at,
        "status": version.metadata_json.get("content_status", "partial"),
        "reason": version.metadata_json.get("content_reason", ""),
        "sections": version.metadata_json.get("sections", []),
        "attachments": version.metadata_json.get("attachments", []),
        "summary": None,
        "summary_status": "not_generated",
        "evidence_scope": version.metadata_json.get("evidence_scope", ""),
        "materials": [
            {
                "id": str(m.id),
                "filename": m.filename,
                "media_type": m.media_type,
                "sha256": m.sha256,
                "size": m.size,
                "original_url": m.original_url,
                "download_url": f"/api/platforms/materials/{m.id}",
            }
            for m in materials
        ],
    }
