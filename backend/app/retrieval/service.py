from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import ContentVersion, EmbeddingChunk, EmbeddingJob, EmbeddingSpace
from app.retrieval.chunks import passages
from app.retrieval.providers import EmbeddingError, descriptor, get_embedder, space_key


def latest_versions(user_id: UUID):
    return (
        select(ContentVersion)
        .where(ContentVersion.user_id == user_id)
        .distinct(ContentVersion.item_id)
        .order_by(ContentVersion.item_id, ContentVersion.version_number.desc())
        .subquery()
    )


def coverage(session, user_id: UUID, space_id: UUID | None):
    latest = latest_versions(user_id)
    total = session.scalar(select(func.count()).select_from(latest))
    indexed = 0
    if space_id:
        indexed = session.scalar(
            select(func.count(func.distinct(EmbeddingChunk.version_id))).where(
                EmbeddingChunk.user_id == user_id,
                EmbeddingChunk.space_id == space_id,
                EmbeddingChunk.version_id.in_(select(latest.c.id)),
            )
        )
    return {"total_items": total, "indexed_items": indexed, "pending_items": total - indexed}


def execute_index(job_id: UUID):
    """No provider retry. Documents commit atomically; completed documents survive later failure."""
    with SessionLocal.begin() as session:
        job = session.scalar(
            select(EmbeddingJob).where(EmbeddingJob.id == job_id).with_for_update()
        )
        if job is None or job.status != "queued":
            return
        space = session.get(EmbeddingSpace, job.space_id)
        job.status, job.started_at = "running", datetime.now(UTC)
        user_id, space_id, provider = job.user_id, space.id, space.provider
        saved_key, max_items = space.key, job.max_items
    try:
        config = get_settings()
        if space_key(descriptor(provider, config)) != saved_key:
            raise EmbeddingError("模型配置已变化，请重新选择后端并创建索引任务")
        embedder = get_embedder(provider, config)
        with SessionLocal() as session:
            latest = latest_versions(user_id)
            ids = session.scalars(
                select(latest.c.id)
                .where(
                    ~latest.c.id.in_(
                        select(EmbeddingChunk.version_id).where(
                            EmbeddingChunk.user_id == user_id, EmbeddingChunk.space_id == space_id
                        )
                    )
                )
                .order_by(latest.c.fetched_at.desc(), latest.c.id)
                .limit(max_items)
            ).all()
        for version_id in ids:
            with SessionLocal() as session:
                version = session.get(ContentVersion, version_id)
                chunks = passages(version.title, version.clean_text)
                item_id = version.item_id
                scope = {
                    "acquisition_level": version.acquisition_level,
                    "evidence_scope": version.metadata_json.get("evidence_scope", ""),
                }
            result = embedder.encode([chunk["passage"] for chunk in chunks])
            with SessionLocal.begin() as session:
                job = session.get(EmbeddingJob, job_id)
                for chunk, vector in zip(chunks, result.vectors, strict=True):
                    chunk["evidence"].update(scope)
                    session.add(
                        EmbeddingChunk(
                            user_id=user_id,
                            space_id=space_id,
                            item_id=item_id,
                            version_id=version_id,
                            **chunk,
                            embedding=vector,
                        )
                    )
                job.indexed_items += 1
                job.indexed_chunks += len(chunks)
                job.reported_tokens += result.reported_tokens
        with SessionLocal.begin() as session:
            job = session.get(EmbeddingJob, job_id)
            job.status, job.finished_at = "succeeded", datetime.now(UTC)
    except Exception as exc:
        # Neither traceback text nor provider body is stored, logged or returned.
        error = str(exc) if isinstance(exc, EmbeddingError) else "索引处理失败，请检查服务配置"
        with SessionLocal.begin() as session:
            job = session.get(EmbeddingJob, job_id)
            job.status, job.error, job.finished_at = "failed", error, datetime.now(UTC)


def fail_index(job_id: UUID, user_id: UUID):
    with SessionLocal.begin() as session:
        job = session.scalar(
            select(EmbeddingJob)
            .where(EmbeddingJob.id == job_id, EmbeddingJob.user_id == user_id)
            .with_for_update()
        )
        if job is None:
            raise EmbeddingError("索引任务不存在")
        if job.status in {"queued", "running"}:
            job.status, job.finished_at = "failed", datetime.now(UTC)
            job.error = "管理员在停止 Worker 后结束中断任务；部分资料可能已计费"
