"""Explicit library membership and source-grounded update events; never infer a like."""

from sqlalchemy import select

from app.connectors.http import canonicalize_url
from app.models import ContentItem, Favorite, UpdateNotice


def material_signature(version):
    # Provider activity timestamps, crawl timestamps and tracking URLs are not material changes.
    return (
        version.title.strip(),
        " ".join(version.clean_text.split()),
        version.author,
        version.acquisition_level,
        sorted(version.metadata_json.get("topics", [])),
    )


def record_updates(session, item, version, previous):
    favorite = session.scalar(
        select(Favorite).where(
            Favorite.user_id == item.user_id, Favorite.item_id == item.id, Favorite.active.is_(True)
        )
    )
    if favorite and previous and material_signature(previous) != material_signature(version):
        _notice(
            session,
            favorite,
            item,
            version,
            previous,
            "content_changed",
            f"已获取材料内容发生变化：v{previous.version_number} → v{version.version_number}",
            f"version:{version.id}",
        )
    # A newly discovered release is an update to a saved repository, not a duplicate article.
    repository = version.metadata_json.get("repository")
    if item.content_type != "release" or not repository or previous:
        return
    repo = session.scalar(
        select(ContentItem).where(
            ContentItem.user_id == item.user_id,
            ContentItem.canonical_url == canonicalize_url(repository),
        )
    )
    if repo is None:
        return
    saved = session.scalar(
        select(Favorite).where(
            Favorite.user_id == item.user_id, Favorite.item_id == repo.id, Favorite.active.is_(True)
        )
    )
    # Do not notify historical releases when first starting to watch a repository.
    if saved and version.published_at and version.published_at > saved.updated_at:
        _notice(
            session,
            saved,
            item,
            version,
            None,
            "new_release",
            f"收藏的仓库发布了新 Release：{version.title}",
            f"release:{item.id}",
        )


def _notice(session, favorite, item, version, previous, kind, message, key):
    exists = session.scalar(
        select(UpdateNotice.id).where(
            UpdateNotice.favorite_id == favorite.id, UpdateNotice.event_key == key
        )
    )
    if not exists:
        session.add(
            UpdateNotice(
                user_id=item.user_id,
                favorite_id=favorite.id,
                item_id=item.id,
                version_id=version.id,
                previous_version_id=previous.id if previous else None,
                kind=kind,
                message=message,
                event_key=key,
            )
        )
