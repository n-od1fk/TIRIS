from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api import DB, Config, serialize_item
from app.models import (
    ContentItem,
    ContentOccurrence,
    ContentVersion,
    EmbeddingChunk,
    EmbeddingJob,
    EmbeddingSpace,
    Source,
)
from app.profiles.api import lock_preferences
from app.retrieval import providers
from app.retrieval.service import coverage, latest_versions
from app.workers.tasks import index_embedding

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])
Provider = Literal["local", "bailian"]
EmbedderFactory = Annotated[object, Depends(lambda: providers.get_embedder)]


class IndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    max_items: int = Field(default=3, strict=True, ge=1, le=20)
    consent_external: bool = Field(default=False, strict=True)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=5, strict=True, ge=1, le=20)
    source_id: UUID | None = None
    consent_external: bool = Field(default=False, strict=True)


def describe(provider, config):
    try:
        return providers.descriptor(provider, config)
    except (providers.EmbeddingError, ValueError):
        raise HTTPException(503, "百炼 Base URL 尚未正确配置") from None


def require_ready(provider, config, consent):
    if provider == "bailian" and not consent:
        raise HTTPException(422, "使用百炼需明确同意发送文本到外部服务并可能计费")
    reason = providers.readiness(provider, config)
    if reason:
        raise HTTPException(503, reason)


def job_read(job):
    return {
        key: getattr(job, key)
        for key in (
            "id",
            "status",
            "max_items",
            "indexed_items",
            "indexed_chunks",
            "reported_tokens",
            "error",
            "created_at",
            "started_at",
            "finished_at",
        )
    }


@router.get("/backends")
def list_backends(session: DB, config: Config):
    result = []
    for provider in ("local", "bailian"):
        reason = providers.readiness(provider, config)
        try:
            data = providers.descriptor(provider, config)
            key = providers.space_key(data)
        except (providers.EmbeddingError, ValueError):
            key = None
        space = (
            session.scalar(
                select(EmbeddingSpace).where(
                    EmbeddingSpace.user_id == config.user_id, EmbeddingSpace.key == key
                )
            )
            if key
            else None
        )
        job = (
            session.scalar(
                select(EmbeddingJob)
                .where(EmbeddingJob.user_id == config.user_id, EmbeddingJob.space_id == space.id)
                .order_by(EmbeddingJob.created_at.desc())
                .limit(1)
            )
            if space
            else None
        )
        result.append(
            {
                "provider": provider,
                "model": providers.LOCAL_MODEL if provider == "local" else "text-embedding-v4",
                "dimensions": providers.DIMENSIONS,
                "key": key,
                "ready": reason is None,
                "reason": reason,
                **coverage(session, config.user_id, space.id if space else None),
                "latest_job": job_read(job) if job else None,
            }
        )
    return result


@router.post("/index", status_code=202)
def create_index(body: IndexRequest, session: DB, config: Config):
    require_ready(body.provider, config, body.consent_external)
    data = describe(body.provider, config)
    key = providers.space_key(data)
    lock_preferences(session, config.user_id)
    space = session.scalar(
        select(EmbeddingSpace).where(
            EmbeddingSpace.user_id == config.user_id, EmbeddingSpace.key == key
        )
    )
    if space is None:
        space = EmbeddingSpace(
            user_id=config.user_id, key=key, provider=body.provider, descriptor=data
        )
        session.add(space)
        session.flush()
    active = session.scalar(
        select(EmbeddingJob).where(
            EmbeddingJob.space_id == space.id,
            EmbeddingJob.user_id == config.user_id,
            EmbeddingJob.status.in_(["queued", "running"]),
        )
    )
    if active:
        return job_read(active)
    job = EmbeddingJob(user_id=config.user_id, space_id=space.id, max_items=body.max_items)
    session.add(job)
    session.commit()
    try:
        index_embedding.delay(str(job.id))
    except Exception:
        from datetime import UTC, datetime

        job.status, job.error, job.finished_at = "failed", "索引队列不可用", datetime.now(UTC)
        session.commit()
        raise HTTPException(503, {"message": job.error, "job_id": str(job.id)}) from None
    return job_read(job)


@router.post("/search")
def semantic_search(body: SearchRequest, session: DB, config: Config, factory: EmbedderFactory):
    query = body.query.strip()
    if not query:
        raise HTTPException(422, "查询不能全为空白")
    data = describe(body.provider, config)
    space = session.scalar(
        select(EmbeddingSpace).where(
            EmbeddingSpace.user_id == config.user_id,
            EmbeddingSpace.key == providers.space_key(data),
        )
    )
    stats = coverage(session, config.user_id, space.id if space else None)
    response = {
        "provider": body.provider,
        "model": data["model"],
        "space_key": providers.space_key(data),
        **stats,
        "items": [],
        "note": "相似度用于相对排序，不是相关概率；尚未进行人工相关性判定",
    }
    if stats["indexed_items"] == 0:
        response["note"] = "该模型尚无当前版本的索引，请先建立；没有调用向量接口"
        return response
    require_ready(body.provider, config, body.consent_external)
    latest = latest_versions(config.user_id)
    criteria = [
        EmbeddingChunk.space_id == space.id,
        EmbeddingChunk.user_id == config.user_id,
        EmbeddingChunk.version_id.in_(select(latest.c.id)),
    ]
    if body.source_id:
        criteria.append(
            EmbeddingChunk.item_id.in_(
                select(ContentOccurrence.item_id).where(
                    ContentOccurrence.user_id == config.user_id,
                    ContentOccurrence.source_id == body.source_id,
                )
            )
        )
    # Avoid a billable query for an empty source filter.
    if not session.scalar(select(EmbeddingChunk.id).where(*criteria).limit(1)):
        response["note"] = "所选来源没有当前版本索引；没有调用向量接口"
        return response
    try:
        vector = factory(body.provider, config).encode([query], query=True).vectors[0]
    except providers.EmbeddingError as exc:
        raise HTTPException(503, str(exc)) from None
    distance = EmbeddingChunk.embedding.cosine_distance(vector)
    ranked = (
        select(
            EmbeddingChunk.id.label("chunk_id"),
            distance.label("distance"),
            func.row_number()
            .over(partition_by=EmbeddingChunk.item_id, order_by=(distance, EmbeddingChunk.ordinal))
            .label("rank"),
        )
        .where(*criteria)
        .subquery()
    )
    rows = session.execute(
        select(EmbeddingChunk, ContentItem, ContentVersion, ranked.c.distance)
        .join(ranked, ranked.c.chunk_id == EmbeddingChunk.id)
        .join(ContentItem, ContentItem.id == EmbeddingChunk.item_id)
        .join(ContentVersion, ContentVersion.id == EmbeddingChunk.version_id)
        .where(
            ranked.c.rank == 1,
            ContentItem.user_id == config.user_id,
            ContentVersion.user_id == config.user_id,
        )
        .order_by(ranked.c.distance, ContentItem.id)
        .limit(body.limit)
    ).all()
    names = {}
    if rows:
        for item_id, name in session.execute(
            select(ContentOccurrence.item_id, Source.name)
            .join(Source)
            .where(
                ContentOccurrence.user_id == config.user_id,
                Source.user_id == config.user_id,
                ContentOccurrence.item_id.in_([row[1].id for row in rows]),
            )
        ):
            names.setdefault(item_id, set()).add(name)
    response["items"] = [
        {
            **serialize_item(item, version),
            "sources": sorted(names.get(item.id, [])),
            "similarity": max(-1.0, min(1.0, 1.0 - float(dist))),
            "matched_passage": chunk.passage,
            "chunk_ordinal": chunk.ordinal,
            "index_evidence": chunk.evidence,
        }
        for chunk, item, version, dist in rows
    ]
    return response
