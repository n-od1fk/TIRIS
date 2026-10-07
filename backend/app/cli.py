"""Explicit seed/sync commands for learning and debugging, using the same worker logic."""

import argparse
import time
from uuid import UUID

from sqlalchemy import select

from app.config import get_settings
from app.connectors.registry import get_connector
from app.database import SessionLocal
from app.models import IngestionRun, Job, Source
from app.workers.tasks import execute_sync

DEFAULT_SOURCES = [
    ("Cloudflare 博客", "rss", "https://blog.cloudflare.com/rss/"),
    ("pgvector 仓库与版本", "github", "https://github.com/pgvector/pgvector"),
    ("Black Hat 档案索引", "blackhat", "https://www.blackhat.com/html/archives.html"),
]


def seed():
    user_id = get_settings().user_id
    with SessionLocal.begin() as session:
        for name, kind, url in DEFAULT_SOURCES:
            url = get_connector(kind, for_creation=True).validate_url(url)
            exists = session.scalar(
                select(Source).where(
                    Source.user_id == user_id, Source.kind == kind, Source.url == url
                )
            )
            if not exists:
                session.add(Source(user_id=user_id, name=name, kind=kind, url=url))
    print("初始来源已就绪（重复执行不会重复创建）")


def sync_all():
    user_id = get_settings().user_id
    with SessionLocal() as session:
        ids = session.scalars(
            select(Source.id).where(
                Source.user_id == user_id, Source.enabled.is_(True), Source.archived.is_(False)
            )
        ).all()
    for source_id in ids:
        with SessionLocal.begin() as session:
            source = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
            active = session.scalar(
                select(Job).where(
                    Job.source_id == source_id, Job.status.in_(["queued", "running", "retrying"])
                )
            )
            if active:
                print(f"{source.name}: 已有活动任务 {active.id}，跳过")
                continue
            job = Job(user_id=user_id, source_id=source_id, status="queued")
            session.add(job)
            session.flush()
            job_id, name = job.id, source.name
        for attempt in range(4):
            if not execute_sync(job_id):
                break
            time.sleep(min(60, 5 * 2**attempt))
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            run = session.scalar(
                select(IngestionRun)
                .where(IngestionRun.job_id == job_id)
                .order_by(IngestionRun.attempt.desc())
                .limit(1)
            )
            counts = (
                (
                    f" fetched={run.fetched_count} new={run.new_count}"
                    f" updated={run.updated_count} unchanged={run.unchanged_count}"
                )
                if run
                else ""
            )
            print(
                f"{name}: {job.status}, attempts={job.attempts}{counts}, error={job.error or '-'}"
            )


def fail_job(job_id: UUID):
    """Only after stopping the worker: recover a task interrupted by process death."""
    from datetime import UTC, datetime

    with SessionLocal.begin() as session:
        job = session.scalar(
            select(Job)
            .where(Job.id == job_id, Job.user_id == get_settings().user_id)
            .with_for_update()
        )
        if job is None:
            raise SystemExit("任务不存在")
        if job.status in {"queued", "running", "retrying"}:
            job.status, job.error = "failed", "管理员在停止 Worker 后结束了中断任务"
            job.finished_at = datetime.now(UTC)
            for run in session.scalars(
                select(IngestionRun).where(
                    IngestionRun.job_id == job_id, IngestionRun.status == "running"
                )
            ):
                run.status, run.error, run.finished_at = "failed", job.error, job.finished_at
    print("任务已结束，可重新同步来源")


def main():
    parser = argparse.ArgumentParser(description="TIRIS 维护与学习工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed")
    sub.add_parser("sync")
    sub.add_parser("prepare-model", help="显式下载固定版本本地模型，不发送资料")
    discovery_recovery = sub.add_parser("fail-discovery", help="必须先停止 Worker")
    discovery_recovery.add_argument("run_id", type=UUID)
    discovery_recovery.add_argument("--worker-stopped", action="store_true", required=True)
    index_recovery = sub.add_parser("fail-index", help="必须先停止 Worker")
    index_recovery.add_argument("job_id", type=UUID)
    index_recovery.add_argument("--worker-stopped", action="store_true", required=True)
    recovery = sub.add_parser("fail-job", help="必须先停止 Worker")
    recovery.add_argument("job_id", type=UUID)
    recovery.add_argument("--worker-stopped", action="store_true", required=True)
    args = parser.parse_args()
    if args.command == "seed":
        seed()
    elif args.command == "sync":
        sync_all()
    elif args.command == "prepare-model":
        from app.retrieval.providers import prepare_model

        prepare_model(get_settings())
        print("固定版本模型下载完成；尚未建立资料索引")
    elif args.command == "fail-index":
        from app.retrieval.service import fail_index

        fail_index(args.job_id, get_settings().user_id)
        print("中断索引任务已结束，可检查部分结果后重新提交")
    elif args.command == "fail-discovery":
        from app.discovery.service import fail_run

        fail_run(args.run_id, get_settings().user_id)
        print("中断发现任务已结束；已获取的候选和更新事件保留")
    else:
        fail_job(args.job_id)


if __name__ == "__main__":
    main()
