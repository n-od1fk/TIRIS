import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.connectors.contracts import FetchResult, RawItem, SourceError
from app.connectors.http import canonicalize_url
from app.models import ContentItem, ContentOccurrence, ContentVersion, IngestionRun, Source


def content_fingerprint(raw: RawItem) -> tuple[str, str]:
    body = " ".join(raw.clean_text.split())
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    snapshot = {
        "title": raw.title,
        "text": body,
        "author": raw.author,
        "published_at": raw.published_at.isoformat() if raw.published_at else None,
        "level": raw.acquisition_level,
        "metadata": raw.metadata,
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return body_hash, fingerprint


def store_items(session: Session, source: Source, result: FetchResult, run: IngestionRun):
    # Serialize this user's writes so concurrent sources cannot create duplicate identities.
    lock_key = int.from_bytes(
        hashlib.sha256(str(source.user_id).encode()).digest()[:8], "big", signed=True
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    now = datetime.now(UTC)
    run.fetched_count = len(result.items)
    run.rejected_count = result.rejected_count
    for raw in result.items:
        try:
            with session.begin_nested():
                _store_one(session, source, raw, now, run)
        except SourceError:
            run.rejected_count += 1
    source.cursor = result.cursor
    source.last_success_at = now
    source.last_error = None


def _store_one(session: Session, source: Source, raw: RawItem, now: datetime, run: IngestionRun):
    url = canonicalize_url(raw.url)
    if not raw.external_id or not raw.title.strip():
        raise SourceError("缺少内容标识或标题")
    body_hash, fingerprint = content_fingerprint(raw)
    occurrence = session.scalar(
        select(ContentOccurrence).where(
            ContentOccurrence.source_id == source.id,
            ContentOccurrence.external_id == raw.external_id,
            ContentOccurrence.user_id == source.user_id,
        )
    )
    item = (
        session.get(ContentItem, occurrence.item_id)
        if occurrence
        else session.scalar(
            select(ContentItem).where(
                ContentItem.user_id == source.user_id, ContentItem.canonical_url == url
            )
        )
    )
    # Only long, exact full-text copies with the same title and type may merge across URLs.
    # Abstracts, paper/code pairs and semantic similarity never trigger deletion.
    if item is None and raw.acquisition_level == "full_text" and len(raw.clean_text) >= 200:
        latest_numbers = (
            select(ContentVersion.item_id, func.max(ContentVersion.version_number).label("number"))
            .group_by(ContentVersion.item_id)
            .subquery()
        )
        item = session.scalar(
            select(ContentItem)
            .join(ContentVersion)
            .join(
                latest_numbers,
                (latest_numbers.c.item_id == ContentVersion.item_id)
                & (latest_numbers.c.number == ContentVersion.version_number),
            )
            .where(
                ContentItem.user_id == source.user_id,
                ContentItem.title == raw.title,
                ContentItem.content_type == raw.content_type,
                ContentVersion.acquisition_level == "full_text",
                ContentVersion.body_hash == body_hash,
            )
            .limit(1)
        )
    is_new = item is None
    if item is None:
        item = ContentItem(
            user_id=source.user_id,
            canonical_url=url,
            title=raw.title,
            content_type=raw.content_type,
            language=raw.language,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(item)
        session.flush()
    if occurrence is None:
        occurrence = ContentOccurrence(
            user_id=source.user_id,
            source_id=source.id,
            item_id=item.id,
            external_id=raw.external_id,
            original_url=raw.url,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(occurrence)
    else:
        occurrence.last_seen_at = now
        occurrence.original_url = raw.url
    item.last_seen_at = now
    latest = session.scalar(
        select(ContentVersion)
        .where(
            ContentVersion.item_id == item.id,
            ContentVersion.user_id == source.user_id,
        )
        .order_by(ContentVersion.version_number.desc())
        .limit(1)
    )
    latest_from_source = session.scalar(
        select(ContentVersion)
        .where(
            ContentVersion.item_id == item.id,
            ContentVersion.source_id == source.id,
            ContentVersion.user_id == source.user_id,
        )
        .order_by(ContentVersion.version_number.desc())
        .limit(1)
    )
    if latest_from_source and latest_from_source.fingerprint == fingerprint:
        run.unchanged_count += 1
        return
    # Changed source snapshots are immutable, even when a newer excerpt is shorter.
    # Previous full text remains available in version history.
    item.title = raw.title
    version = ContentVersion(
        user_id=source.user_id,
        item_id=item.id,
        source_id=source.id,
        version_number=latest.version_number + 1 if latest else 1,
        title=raw.title,
        author=raw.author,
        published_at=raw.published_at,
        fetched_at=now,
        acquisition_level=raw.acquisition_level,
        clean_text=raw.clean_text,
        body_hash=body_hash,
        fingerprint=fingerprint,
        metadata_json=raw.metadata,
    )
    session.add(version)
    item.candidate_expires_at = now + timedelta(days=30)
    if is_new:
        run.new_count += 1
    else:
        run.updated_count += 1
    session.flush()
    from app.services.favorites import record_updates

    record_updates(session, item, version, latest_from_source)
