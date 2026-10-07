"""A paginated persistent feed; bounded batches do not impose a daily ten-item cap."""

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Text, cast, func, select, tuple_

from app.api import DB, Config, serialize_item
from app.discovery.api import run_read
from app.discovery.service import enqueue
from app.models import (
    ContentItem,
    ContentVersion,
    DiscoveryRun,
    Favorite,
    LongTermProfile,
    Recommendation,
)
from app.profiles.api import lock_preferences

router = APIRouter(prefix="/api/feed", tags=["feed"])


def refill(session, user_id):
    lock_preferences(session, user_id)
    now = datetime.now(UTC)
    last = session.scalar(
        select(DiscoveryRun)
        .where(DiscoveryRun.user_id == user_id)
        .order_by(DiscoveryRun.created_at.desc(), DiscoveryRun.id)
        .limit(1)
    )
    if last and last.status in {"queued", "running"}:
        return {"state": "running", "run": run_read(last), "retry_after": 3}
    profile = session.scalar(select(LongTermProfile).where(LongTermProfile.user_id == user_id))
    profile_changed = last is not None and last.snapshot.get("profile_version") != (
        profile.version if profile else 0
    )
    # Manual source sync may supply fresh candidates during an empty-batch backoff.
    fresh_material = False
    if last and last.finished_at:
        delivered = select(Recommendation.version_id).where(Recommendation.user_id == user_id)
        fresh_material = (
            session.scalar(
                select(ContentVersion.id)
                .where(
                    ContentVersion.user_id == user_id,
                    ContentVersion.fetched_at > last.finished_at,
                    ContentVersion.id.not_in(delivered),
                )
                .limit(1)
            )
            is not None
        )
    # Let successful cached batches continue; empty/failed batches back off, not busy-loop.
    delay = (
        900
        if last and (last.status == "failed" or not last.report.get("recommendation_count"))
        else 2
    )
    if (
        not profile_changed
        and not fresh_material
        and last
        and last.finished_at
        and now < last.finished_at + timedelta(seconds=delay)
    ):
        return {
            "state": "waiting",
            "run": run_read(last),
            "retry_after": max(
                2, int((last.finished_at + timedelta(seconds=delay) - now).total_seconds())
            ),
        }
    # Inspect persisted snapshots, not transient browser timers. Legacy runs refreshed sources.
    network_rows = session.scalars(
        select(DiscoveryRun).where(
            DiscoveryRun.user_id == user_id, DiscoveryRun.created_at > now - timedelta(minutes=15)
        )
    ).all()
    refresh = not any(row.snapshot.get("refresh_sources", True) for row in network_rows)
    try:
        run = enqueue(session, user_id, refresh=refresh, feed=True)
    except ValueError:
        return {"state": "needs_profile", "run": None, "retry_after": 60}
    return {"state": "running", "run": run_read(run), "retry_after": 3}


@router.post("/refill", status_code=202)
def refill_feed(session: DB, config: Config):
    try:
        return refill(session, config.user_id)
    except RuntimeError:
        raise HTTPException(503, "补充任务暂不可用，请稍后重试") from None


@router.get("")
def feed(
    session: DB,
    config: Config,
    cursor: str | None = Query(None, max_length=1000),
    limit: int = Query(20, ge=1, le=50),
):
    now = datetime.now(UTC)
    as_of, before = now, None
    if cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if UUID(payload["user"]) != config.user_id:
                raise ValueError()
            as_of = datetime.fromisoformat(payload["as_of"])
            before = (datetime.fromisoformat(payload["before"]), UUID(payload["id"]))
            if as_of.tzinfo is None or before[0].tzinfo is None or as_of > now:
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise HTTPException(422, "推荐分页位置无效，请刷新") from None
    # Stable snapshot paging; newer inserts stay at the top until explicitly refreshed.
    unique = (
        select(
            Recommendation.id.label("id"),
            DiscoveryRun.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=Recommendation.item_id,
                order_by=(DiscoveryRun.created_at.desc(), Recommendation.id.desc()),
            )
            .label("rn"),
        )
        .join(DiscoveryRun, DiscoveryRun.id == Recommendation.run_id)
        .where(
            Recommendation.user_id == config.user_id,
            DiscoveryRun.user_id == config.user_id,
            DiscoveryRun.created_at <= as_of,
            DiscoveryRun.status.in_(["succeeded", "partial"]),
        )
        .subquery()
    )
    query = (
        select(Recommendation, ContentItem, ContentVersion, unique.c.created_at)
        .join(unique, unique.c.id == Recommendation.id)
        .join(ContentItem, ContentItem.id == Recommendation.item_id)
        .join(ContentVersion, ContentVersion.id == Recommendation.version_id)
        .where(
            unique.c.rn == 1,
            ContentItem.user_id == config.user_id,
            ContentVersion.user_id == config.user_id,
            ContentItem.candidate_expires_at > now,
        )
    )
    profile = session.scalar(
        select(LongTermProfile).where(LongTermProfile.user_id == config.user_id)
    )
    for term in profile.data.get("blocked_topics", []) if profile else []:
        material = (
            ContentVersion.title
            + " "
            + ContentVersion.clean_text
            + " "
            + func.coalesce(cast(ContentVersion.metadata_json["topics"], Text), "")
        )
        query = query.where(~material.icontains(term, autoescape=True))
    if before:
        query = query.where(tuple_(unique.c.created_at, Recommendation.id) < before)
    rows = session.execute(
        query.order_by(unique.c.created_at.desc(), Recommendation.id.desc()).limit(limit + 1)
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    saved = set(
        session.scalars(
            select(Favorite.item_id).where(
                Favorite.user_id == config.user_id, Favorite.active.is_(True)
            )
        )
    )
    next_cursor = None
    if more:
        last, _, _, created = rows[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "user": str(config.user_id),
                    "as_of": as_of.isoformat(),
                    "before": created.isoformat(),
                    "id": str(last.id),
                }
            ).encode()
        ).decode()
    return {
        "items": [
            {
                **serialize_item(item, version),
                "title": version.title,
                "favorite": item.id in saved,
                "reason": rec.reason,
                "sources": [rec.score.get("source", "")],
            }
            for rec, item, version, _ in rows
        ],
        "next_cursor": next_cursor,
    }
