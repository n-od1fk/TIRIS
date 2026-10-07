from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.connectors.contracts import SourceError
from app.connectors.registry import get_connector, public_source_types
from app.database import get_session
from app.models import (
    ContentItem,
    ContentOccurrence,
    ContentVersion,
    Favorite,
    IngestionRun,
    Job,
    Source,
    UpdateNotice,
)
from app.user_context import request_settings

router = APIRouter(prefix="/api")
DB = Annotated[Session, Depends(get_session)]
Config = Annotated[Settings, Depends(request_settings)]


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=20)
    url: str = Field(max_length=2000)

    @model_validator(mode="after")
    def normalize_source(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("来源名称不能为空")
        try:
            self.url = get_connector(self.kind, for_creation=True).validate_url(self.url)
        except SourceError as exc:
            raise ValueError(str(exc)) from exc
        return self


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    kind: str
    url: str
    enabled: bool
    last_success_at: datetime | None
    last_error: str | None


class SourcePatch(BaseModel):
    enabled: bool


class SyncRequest(BaseModel):
    source_id: UUID


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    source_id: UUID
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None


def source_or_404(session, source_id, user_id):
    source = session.scalar(
        select(Source).where(
            Source.id == source_id, Source.user_id == user_id, Source.archived.is_(False)
        )
    )
    if source is None:
        raise HTTPException(404, "来源不存在")
    return source


@router.get("/sources", response_model=list[SourceRead])
def list_sources(session: DB, config: Config):
    return session.scalars(
        select(Source)
        .where(Source.user_id == config.user_id, Source.archived.is_(False))
        .order_by(Source.created_at)
    ).all()


@router.get("/source-types")
def source_types():
    return public_source_types()


@router.post("/sources", response_model=SourceRead, status_code=201)
def create_source(body: SourceCreate, session: DB, config: Config):
    existing = session.scalar(
        select(Source).where(
            Source.user_id == config.user_id, Source.kind == body.kind, Source.url == body.url
        )
    )
    if existing and not existing.archived:
        raise HTTPException(409, "此来源已存在")
    if existing:
        # Explicit re-add recovers an archived automatic template with its UUID intact.
        existing.archived, existing.enabled, existing.name = False, True, body.name
        session.commit()
        return existing
    source = Source(user_id=config.user_id, **body.model_dump())
    session.add(source)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "此来源已存在") from None
    return source


@router.patch("/sources/{source_id}", response_model=SourceRead)
def patch_source(source_id: UUID, body: SourcePatch, session: DB, config: Config):
    source = source_or_404(session, source_id, config.user_id)
    source.enabled = body.enabled
    session.commit()
    return source


@router.post("/sources/sync", response_model=JobRead, status_code=202)
def enqueue_sync(body: SyncRequest, session: DB, config: Config):
    source = session.scalar(
        select(Source)
        .where(
            Source.id == body.source_id,
            Source.user_id == config.user_id,
            Source.archived.is_(False),
        )
        .with_for_update()
    )
    if source is None:
        raise HTTPException(404, "来源不存在")
    if not source.enabled:
        raise HTTPException(409, "来源已停用")
    active = session.scalar(
        select(Job).where(
            Job.source_id == source.id, Job.status.in_(["queued", "running", "retrying"])
        )
    )
    if active:
        return active
    job = Job(user_id=config.user_id, source_id=source.id, status="queued")
    session.add(job)
    session.commit()
    try:
        from app.workers.tasks import sync_source

        sync_source.delay(str(job.id))
    except Exception:
        job.status, job.error = "failed", "任务队列不可用，请检查 Redis 和 Worker 后重新同步"
        job.finished_at = datetime.now(UTC)
        session.commit()
        raise HTTPException(503, {"message": job.error, "job_id": str(job.id)}) from None
    return job


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: UUID, session: DB, config: Config):
    job = session.scalar(select(Job).where(Job.id == job_id, Job.user_id == config.user_id))
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job


@router.get("/sources/{source_id}/runs")
def list_runs(source_id: UUID, session: DB, config: Config):
    source_or_404(session, source_id, config.user_id)
    return session.scalars(
        select(IngestionRun)
        .where(
            IngestionRun.source_id == source_id,
            IngestionRun.user_id == config.user_id,
        )
        .order_by(IngestionRun.started_at.desc())
        .limit(20)
    ).all()


def serialize_item(item, version):
    return {
        "id": item.id,
        "title": item.title,
        "canonical_url": item.canonical_url,
        "content_type": item.content_type,
        "language": item.language,
        "first_seen_at": item.first_seen_at,
        "last_seen_at": item.last_seen_at,
        "version": version.version_number if version else None,
        "published_at": version.published_at if version else None,
        "fetched_at": version.fetched_at if version else None,
        "acquisition_level": version.acquisition_level if version else "metadata_only",
        "excerpt": version.clean_text[:500] if version else "",
        "evidence_scope": version.metadata_json.get("evidence_scope", "") if version else "",
    }


@router.get("/items")
def list_items(
    session: DB,
    config: Config,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    q: str = Query("", max_length=200),
    source_id: UUID | None = None,
    scope: Literal["candidates", "library"] = "candidates",
):
    criteria = [ContentItem.user_id == config.user_id]
    saved_ids = select(Favorite.item_id).where(
        Favorite.user_id == config.user_id, Favorite.active.is_(True)
    )
    if scope == "library":
        criteria.append(ContentItem.id.in_(saved_ids))
    else:
        criteria.extend(
            [~ContentItem.id.in_(saved_ids), ContentItem.candidate_expires_at > datetime.now(UTC)]
        )
    if q.strip():
        criteria.append(ContentItem.title.icontains(q.strip(), autoescape=True))
    if source_id:
        criteria.append(
            ContentItem.id.in_(
                select(ContentOccurrence.item_id).where(
                    ContentOccurrence.source_id == source_id,
                    ContentOccurrence.user_id == config.user_id,
                )
            )
        )
    total = session.scalar(select(func.count()).select_from(ContentItem).where(*criteria))
    items = session.scalars(
        select(ContentItem)
        .where(*criteria)
        .order_by(ContentItem.first_seen_at.desc(), ContentItem.id)
        .offset(offset)
        .limit(limit)
    ).all()
    latest, sources = {}, {}
    if items:
        versions = session.scalars(
            select(ContentVersion)
            .where(
                ContentVersion.user_id == config.user_id,
                ContentVersion.item_id.in_([item.id for item in items]),
            )
            .distinct(ContentVersion.item_id)
            .order_by(ContentVersion.item_id, ContentVersion.version_number.desc())
        ).all()
        for version in versions:
            latest.setdefault(version.item_id, version)
        for item_id, name in session.execute(
            select(ContentOccurrence.item_id, Source.name)
            .join(Source)
            .where(
                ContentOccurrence.user_id == config.user_id,
                ContentOccurrence.item_id.in_([item.id for item in items]),
            )
        ):
            sources.setdefault(item_id, set()).add(name)
    return {
        "total": total,
        "scope": scope,
        "items": [
            {
                **serialize_item(item, latest.get(item.id)),
                "favorite": scope == "library",
                "sources": sorted(sources.get(item.id, [])),
            }
            for item in items
        ],
    }


@router.get("/items/{item_id}")
def item_detail(item_id: UUID, session: DB, config: Config):
    item = session.scalar(
        select(ContentItem).where(ContentItem.id == item_id, ContentItem.user_id == config.user_id)
    )
    if item is None:
        raise HTTPException(404, "内容不存在")
    versions = session.scalars(
        select(ContentVersion)
        .where(
            ContentVersion.item_id == item_id,
            ContentVersion.user_id == config.user_id,
        )
        .order_by(ContentVersion.version_number.desc())
    ).all()
    occurrences = session.execute(
        select(ContentOccurrence, Source.name)
        .join(Source)
        .where(
            ContentOccurrence.item_id == item_id,
            ContentOccurrence.user_id == config.user_id,
        )
    ).all()
    return {
        **serialize_item(item, versions[0] if versions else None),
        "favorite": session.scalar(
            select(Favorite.id).where(
                Favorite.user_id == config.user_id,
                Favorite.item_id == item.id,
                Favorite.active.is_(True),
            )
        )
        is not None,
        "text": versions[0].clean_text if versions else "",
        "versions": [
            {
                "number": v.version_number,
                "fetched_at": v.fetched_at,
                "acquisition_level": v.acquisition_level,
                "title": v.title,
                "text": v.clean_text,
                "evidence_scope": v.metadata_json.get("evidence_scope", ""),
            }
            for v in versions
        ],
        "sources": [
            {
                "name": name,
                "original_url": occurrence.original_url,
                "external_id": occurrence.external_id,
            }
            for occurrence, name in occurrences
        ],
    }


class FavoriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)


@router.put("/items/{item_id}/favorite")
def set_favorite(item_id: UUID, body: FavoriteRequest, session: DB, config: Config):
    from app.profiles.api import lock_preferences

    lock_preferences(session, config.user_id)
    item = session.scalar(
        select(ContentItem).where(ContentItem.id == item_id, ContentItem.user_id == config.user_id)
    )
    if item is None:
        raise HTTPException(404, "内容不存在")
    latest = session.scalar(
        select(ContentVersion)
        .where(ContentVersion.item_id == item.id, ContentVersion.user_id == config.user_id)
        .order_by(ContentVersion.version_number.desc())
        .limit(1)
    )
    if latest is None:
        raise HTTPException(409, "内容还没有可保存的版本")
    favorite = session.scalar(
        select(Favorite).where(Favorite.user_id == config.user_id, Favorite.item_id == item.id)
    )
    now = datetime.now(UTC)
    if favorite is None and body.enabled:
        favorite = Favorite(
            user_id=config.user_id, item_id=item.id, saved_version_id=latest.id, updated_at=now
        )
        session.add(favorite)
    elif favorite and favorite.active != body.enabled:
        favorite.active, favorite.updated_at = body.enabled, now
        if body.enabled:
            favorite.saved_version_id = latest.id
        else:
            item.candidate_expires_at = now + timedelta(days=30)
    # Search-discovered repositories need a repository connector for release monitoring.
    if (
        body.enabled
        and item.content_type == "repository"
        and item.canonical_url.startswith("https://github.com/")
    ):
        owned = session.scalar(
            select(Source).where(
                Source.user_id == config.user_id,
                Source.kind == "github",
                Source.url == item.canonical_url,
            )
        )
        if owned is None:
            session.add(
                Source(
                    user_id=config.user_id,
                    name=f"收藏跟踪：{item.title}"[:200],
                    kind="github",
                    url=item.canonical_url,
                )
            )
        elif owned.archived:
            owned.archived, owned.enabled = False, True
    session.commit()
    return {
        "item_id": item.id,
        "favorite": body.enabled,
        "monitoring_scope": (
            "GitHub 仓库元数据/Release；文章只检查订阅源中仍可获取的内容，不保证全文"
        ),
    }


@router.get("/updates")
def list_updates(
    session: DB, config: Config, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)
):
    criteria = [
        UpdateNotice.user_id == config.user_id,
        Favorite.user_id == config.user_id,
        Favorite.active.is_(True),
    ]
    total = session.scalar(
        select(func.count()).select_from(UpdateNotice).join(Favorite).where(*criteria)
    )
    unread = session.scalar(
        select(func.count())
        .select_from(UpdateNotice)
        .join(Favorite)
        .where(*criteria, UpdateNotice.read.is_(False))
    )
    rows = session.execute(
        select(UpdateNotice, ContentItem, ContentVersion)
        .select_from(UpdateNotice)
        .join(Favorite, Favorite.id == UpdateNotice.favorite_id)
        .join(ContentItem, ContentItem.id == UpdateNotice.item_id)
        .join(ContentVersion, ContentVersion.id == UpdateNotice.version_id)
        .where(*criteria)
        .order_by(UpdateNotice.created_at.desc(), UpdateNotice.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "total": total,
        "unread": unread,
        "items": [
            {
                "id": n.id,
                "item_id": item.id,
                "title": v.title,
                "url": item.canonical_url,
                "kind": n.kind,
                "message": n.message,
                "read": n.read,
                "version": v.version_number,
                "previous_version_id": n.previous_version_id,
                "evidence_scope": v.metadata_json.get("evidence_scope", ""),
                "created_at": n.created_at,
            }
            for n, item, v in rows
        ],
    }


@router.patch("/updates/{notice_id}/read")
def read_update(notice_id: UUID, session: DB, config: Config):
    notice = session.scalar(
        select(UpdateNotice).where(
            UpdateNotice.id == notice_id, UpdateNotice.user_id == config.user_id
        )
    )
    if notice is None:
        raise HTTPException(404, "提醒不存在")
    notice.read = True
    session.commit()
    return {"id": notice.id, "read": True}


@router.get("/jobs", response_model=list[JobRead])
def list_jobs(session: DB, config: Config):
    return session.scalars(
        select(Job).where(Job.user_id == config.user_id).order_by(Job.created_at.desc()).limit(50)
    ).all()
