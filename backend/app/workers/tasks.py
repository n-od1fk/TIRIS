import logging
import traceback
from datetime import UTC, datetime
from uuid import UUID

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import select

from app.config import get_settings
from app.connectors.adapters import fetch_source
from app.connectors.contracts import RetryableSourceError, SourceError
from app.connectors.http import SafeHTTP
from app.database import SessionLocal
from app.models import IngestionRun, Job, Source
from app.services.ingestion import store_items

logger = logging.getLogger(__name__)
# Suppress request URLs: user-provided feed URLs may carry private query tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)
celery_app = Celery("tiris", broker=get_settings().redis_url)
celery_app.conf.update(
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    worker_prefetch_multiplier=1,
    task_publish_retry=False,
    beat_schedule={
        "discovery-due-check": {"task": "tiris.discovery_due", "schedule": crontab(minute="*/5")},
        "feed-stock-check": {"task": "tiris.feed_due", "schedule": crontab(minute="*/15")},
    },
)


def execute_sync(job_id: UUID, http: SafeHTTP | None = None) -> bool:
    """Process one attempt. Returns True only when the caller should retry."""
    with SessionLocal.begin() as session:
        job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.status not in {"queued", "retrying"}:
            return False
        source = session.get(Source, job.source_id)
        if source is None or source.archived or not source.enabled:
            job.status, job.error = "failed", "来源已停用或不存在"
            job.finished_at = datetime.now(UTC)
            return False
        job.status = "running"
        job.attempts += 1
        job.started_at = datetime.now(UTC)
        run = IngestionRun(
            user_id=job.user_id,
            source_id=source.id,
            job_id=job.id,
            attempt=job.attempts,
            status="running",
            started_at=job.started_at,
        )
        session.add(run)
        session.flush()
        run_id, kind, url, cursor = run.id, source.kind, source.url, source.cursor
        connection_version = source.connection_version
    owned_http = http is None
    retry = False
    try:
        http = http or SafeHTTP()
        result = fetch_source(kind, url, cursor, http)
        with SessionLocal.begin() as session:
            source = session.scalar(select(Source).where(Source.id == source.id).with_for_update())
            job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            run = session.get(IngestionRun, run_id)
            if (
                source.archived
                or not source.enabled
                or job.status != "running"
                or source.connection_version != connection_version
            ):
                run.status = job.status = "failed"
                run.error = job.error = "来源已停用或删除，未保存迟到的采集结果"
                run.finished_at = job.finished_at = datetime.now(UTC)
                return False
            store_items(session, source, result, run)
            run.status, job.status = "succeeded", "succeeded"
            run.finished_at = job.finished_at = datetime.now(UTC)
            job.error = None
            logger.info(
                "Sync succeeded: job=%s source=%s fetched=%s new=%s updated=%s unchanged=%s",
                job_id,
                source.id,
                run.fetched_count,
                run.new_count,
                run.updated_count,
                run.unchanged_count,
            )
    except Exception as exc:
        # Only controlled source messages reach UI; arbitrary exception text may contain secrets.
        error = str(exc) if isinstance(exc, SourceError) else "处理失败，请检查服务端错误日志"
        if not isinstance(exc, SourceError):
            frames = [
                (frame.filename, frame.lineno, frame.name)
                for frame in traceback.extract_tb(exc.__traceback__)
            ]
            logger.error(
                "Sync failed: job=%s exception=%s frames=%s", job_id, type(exc).__name__, frames
            )
        with SessionLocal.begin() as session:
            source = session.scalar(select(Source).where(Source.id == source.id).with_for_update())
            job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            run = session.get(IngestionRun, run_id)
            if (
                source.archived
                or not source.enabled
                or job.status != "running"
                or source.connection_version != connection_version
            ):
                run.status = job.status = "failed"
                run.error = job.error = "来源已停用或删除，未继续重试"
                run.finished_at = job.finished_at = datetime.now(UTC)
                return False
            retry = isinstance(exc, RetryableSourceError) and job.attempts < 4
            logger.warning(
                "Sync attempt failed: job=%s attempt=%s retry=%s reason=%s",
                job_id,
                job.attempts,
                retry,
                error,
            )
            run.status, run.error, run.finished_at = "failed", error, datetime.now(UTC)
            job.status, job.error = ("retrying" if retry else "failed"), error
            if not retry:
                job.finished_at = run.finished_at
            session.get(Source, job.source_id).last_error = error
    finally:
        if owned_http and http is not None:
            http.close()
    return retry


@celery_app.task(bind=True, max_retries=3, name="tiris.sync_source")
def sync_source(self, job_id: str):
    if execute_sync(UUID(job_id)):
        raise self.retry(countdown=min(60, 5 * 2**self.request.retries))


@celery_app.task(name="tiris.index_embedding")
def index_embedding(job_id: str):
    from app.retrieval.service import execute_index

    execute_index(UUID(job_id))


@celery_app.task(name="tiris.discover")
def discover(run_id: str):
    from app.discovery.service import execute

    execute(UUID(run_id))


@celery_app.task(name="tiris.discovery_due")
def discovery_due():
    from zoneinfo import ZoneInfo

    from app.discovery.schemas import DiscoveryData
    from app.discovery.service import enqueue
    from app.models import DiscoverySettings

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    user_id = get_settings().user_id
    with SessionLocal() as session:
        row = session.scalar(select(DiscoverySettings).where(DiscoverySettings.user_id == user_id))
        data = DiscoveryData.model_validate(row.data if row else {})
        if data.daily_enabled and now.hour >= data.hour:
            try:
                enqueue(session, user_id, schedule_key=now.date().isoformat())
            except (ValueError, RuntimeError):
                # No secrets in periodic logs; visible failed runs remain in the DB.
                logger.warning(
                    "Daily discovery not submitted: check interests or queue configuration"
                )


@celery_app.task(name="tiris.feed_due")
def feed_due():
    from app.discovery.feed import refill
    from app.models import LongTermProfile

    with SessionLocal() as session:
        profiles = session.scalars(select(LongTermProfile)).all()
        ids = [
            p.user_id
            for p in profiles
            if p.data.get("long_term_directions") or p.data.get("learning_goals")
        ]
    for user_id in ids:
        with SessionLocal() as session:
            try:
                refill(session, user_id)
            except RuntimeError:
                logger.warning("Feed refill not submitted: check queue configuration")
