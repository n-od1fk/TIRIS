from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api import DB, Config, serialize_item
from app.discovery.schemas import DiscoveryData, DiscoveryPatch
from app.models import (
    ContentItem,
    ContentVersion,
    DiscoveryRun,
    DiscoverySettings,
    Favorite,
    Recommendation,
)
from app.profiles.api import lock_preferences, require_version

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("/settings")
def settings(session: DB, config: Config):
    row = session.scalar(
        select(DiscoverySettings).where(DiscoverySettings.user_id == config.user_id)
    )
    return {
        "version": row.version if row else 0,
        "data": DiscoveryData.model_validate(row.data if row else {}).model_dump(),
        "timezone": "Asia/Shanghai",
    }


@router.patch("/settings")
def patch_settings(body: DiscoveryPatch, session: DB, config: Config):
    lock_preferences(session, config.user_id)
    row = session.scalar(
        select(DiscoverySettings).where(DiscoverySettings.user_id == config.user_id)
    )
    require_version(body.expected_version, row.version if row else 0)
    before = DiscoveryData.model_validate(row.data if row else {}).model_dump()
    patch = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    data = DiscoveryData.model_validate({**before, **patch}).model_dump()
    if data["daily_enabled"]:
        from app.models import FixedColumn, LongTermProfile

        profile = session.scalar(
            select(LongTermProfile).where(LongTermProfile.user_id == config.user_id)
        )
        long_terms = (
            (profile.data.get("long_term_directions", []) + profile.data.get("learning_goals", []))
            if profile
            else []
        )
        column = session.scalar(
            select(FixedColumn.id)
            .where(FixedColumn.user_id == config.user_id, FixedColumn.enabled.is_(True))
            .limit(1)
        )
        saved = session.scalar(
            select(Favorite.id)
            .where(Favorite.user_id == config.user_id, Favorite.active.is_(True))
            .limit(1)
        )
        if not (data["recent_topics"] or long_terms or column or saved):
            raise HTTPException(
                422, "开启每日运行前，请填写兴趣/方向、启用栏目或收藏需要跟踪的资料"
            )
    if before != data:
        if row is None:
            session.add(
                DiscoverySettings(user_id=config.user_id, data=data, updated_at=datetime.now(UTC))
            )
        else:
            row.data, row.version, row.updated_at = data, row.version + 1, datetime.now(UTC)
        session.commit()
    return settings(session, config)


def run_read(row):
    return {
        field: getattr(row, field)
        for field in (
            "id",
            "status",
            "report",
            "error",
            "created_at",
            "started_at",
            "finished_at",
            "schedule_key",
        )
    }


@router.post("/runs", status_code=202)
def start_run(session: DB, config: Config):
    from app.discovery.service import enqueue

    try:
        return run_read(enqueue(session, config.user_id))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except RuntimeError:
        raise HTTPException(503, "发现任务队列不可用，请检查 Redis/Worker") from None


@router.get("/runs")
def list_runs(session: DB, config: Config):
    return [
        run_read(row)
        for row in session.scalars(
            select(DiscoveryRun)
            .where(DiscoveryRun.user_id == config.user_id)
            .order_by(DiscoveryRun.created_at.desc(), DiscoveryRun.id)
            .limit(20)
        )
    ]


@router.post("/check-updates", status_code=202)
def check_updates(session: DB, config: Config):
    from app.discovery.service import enqueue

    try:
        return run_read(enqueue(session, config.user_id, mode="updates"))
    except RuntimeError:
        raise HTTPException(503, "更新检查队列不可用") from None


@router.get("/runs/{run_id}/recommendations")
def recommendations(run_id: UUID, session: DB, config: Config, limit: int = Query(10, ge=1, le=10)):
    run = session.scalar(
        select(DiscoveryRun).where(
            DiscoveryRun.id == run_id, DiscoveryRun.user_id == config.user_id
        )
    )
    if run is None:
        raise HTTPException(404, "发现任务不存在")
    rows = session.execute(
        select(Recommendation, ContentItem, ContentVersion)
        .join(ContentItem, ContentItem.id == Recommendation.item_id)
        .join(ContentVersion, ContentVersion.id == Recommendation.version_id)
        .where(
            Recommendation.run_id == run.id,
            Recommendation.user_id == config.user_id,
            ContentItem.user_id == config.user_id,
            ContentVersion.user_id == config.user_id,
            ContentItem.candidate_expires_at > datetime.now(UTC),
        )
        .order_by(Recommendation.position)
        .limit(limit)
    ).all()
    saved = set(
        session.scalars(
            select(Favorite.item_id).where(
                Favorite.user_id == config.user_id, Favorite.active.is_(True)
            )
        )
    )
    return {
        "run": run_read(run),
        "items": [
            {
                **serialize_item(item, v),
                "title": v.title,
                "favorite": item.id in saved,
                "reason": rec.reason,
                "score": rec.score,
                "column_name": rec.column_name,
                "summary_label": "来源节选（非 AI 总结）",
                "difficulty": "尚未评估",
                "sources": [rec.score.get("source", "")],
            }
            for rec, item, v in rows
        ],
    }
