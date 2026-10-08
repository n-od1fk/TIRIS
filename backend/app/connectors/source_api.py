"""Identity-scoped discover -> verify -> save -> ingest, independent of platform."""

import hashlib
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import SourceRead
from app.config import Settings
from app.connectors.adapters import next_cursor
from app.connectors.contracts import SourceError
from app.connectors.http import SafeHTTP
from app.connectors.platform_onboarding import (
    PreparedConnection,
    VerifyPlatformRequest,
    revalidate_platform,
    verify_platform,
)
from app.connectors.source_discovery import discover_sources, validate_feed
from app.database import get_session
from app.models import IngestionRun, Job, Source, SourceDiscovery
from app.services.credentials import encrypt_values
from app.services.ingestion import store_items
from app.user_context import request_settings

router = APIRouter(prefix="/api/sources", tags=["source onboarding"])
DB = Annotated[Session, Depends(get_session)]
Config = Annotated[Settings, Depends(request_settings)]


class DiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2000)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID
    name: str = Field(default="", max_length=200)


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discovery_id: UUID
    selections: list[Selection] = Field(min_length=1, max_length=8)
    enabled: bool = True
    edit_source_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)


@router.post("/verify-platform")
def verify_platform_connection(body: VerifyPlatformRequest, session: DB, config: Config):
    http = SafeHTTP()
    try:
        return verify_platform(body, session, config, http)
    finally:
        http.close()


@router.post("/discover")
def discover(body: DiscoverRequest, session: DB, config: Config):
    http = SafeHTTP()
    try:
        try:
            result = discover_sources(body.url, http)
        except SourceError as exc:
            result = {
                "input_url": body.url,
                "effective_url": None,
                "candidates": [],
                "warnings": [],
                "message": str(exc),
                "needs": [
                    "检查地址/网络；401 或 403 可能需要官方授权。请提供公开订阅地址，"
                    "或 API 文档及其要求的 Key/授权；不会绕过访问限制"
                ],
            }
    finally:
        http.close()
    now = datetime.now(UTC)
    session.execute(
        delete(SourceDiscovery).where(
            SourceDiscovery.user_id == config.user_id, SourceDiscovery.expires_at < now
        )
    )
    draft = SourceDiscovery(
        user_id=config.user_id, payload=result, expires_at=now + timedelta(minutes=15)
    )
    session.add(draft)
    session.commit()
    return {**result, "discovery_id": draft.id, "expires_at": draft.expires_at}


@router.post("/connect", status_code=201)
def connect(body: ConnectRequest, session: DB, config: Config):
    draft = session.scalar(
        select(SourceDiscovery).where(
            SourceDiscovery.id == body.discovery_id,
            SourceDiscovery.user_id == config.user_id,
            SourceDiscovery.expires_at > datetime.now(UTC),
        )
    )
    if draft is None:
        raise HTTPException(404, "识别结果不存在、已过期或不属于当前身份，请重新检测")
    if len({s.candidate_id for s in body.selections}) != len(body.selections):
        raise HTTPException(422, "不能重复选择同一订阅")
    choices = {r["id"]: r for r in draft.payload["candidates"] if r["verified"]}
    if any(str(s.candidate_id) not in choices for s in body.selections):
        raise HTTPException(422, "只能保存本次已验证的订阅，不能提交未经检测的地址")
    if body.edit_source_id:
        if len(body.selections) != 1 or body.expected_version is None:
            raise HTTPException(422, "编辑来源需要一个订阅和 expected_version")
        existing = session.scalar(
            select(Source).where(
                Source.id == body.edit_source_id,
                Source.user_id == config.user_id,
                Source.archived.is_(False),
            )
        )
        if existing is None:
            raise HTTPException(404, "待编辑来源不存在")
    http, prepared = SafeHTTP(), []
    deadline = time.monotonic() + 90
    try:
        # Do all network validation before changing any saved configuration.
        for selection in body.selections:
            if time.monotonic() > deadline:
                raise HTTPException(422, "本次复验超过时间预算，未保存配置；请减少选中的订阅后重试")
            chosen = choices[str(selection.candidate_id)]
            try:
                if draft.payload.get("mode") == "platform_verified":
                    content = revalidate_platform(draft, body, http)
                else:
                    feed, actual, response = validate_feed(chosen["url"], http)
                    content = PreparedConnection(
                        "rss", actual, feed.format, feed.title, feed.result, response
                    )
            except SourceError as exc:
                raise HTTPException(422, f"保存前复验失败：{exc}；未更改来源配置") from None
            except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
                raise HTTPException(502, "平台返回格式异常；未更改来源配置") from None
            prepared.append((selection, content))
    finally:
        http.close()
    if len({p[1].url for p in prepared}) != len(prepared):
        raise HTTPException(409, "所选订阅重定向到了同一地址，请合并选择")
    lock_key = int.from_bytes(
        hashlib.sha256(("source-config:" + str(config.user_id)).encode()).digest()[:8],
        "big",
        signed=True,
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    saved, pending = [], []
    now = datetime.now(UTC)
    try:
        for selection, content in prepared:
            actual, kind = content.url, content.kind
            if body.edit_source_id:
                source = session.scalar(
                    select(Source)
                    .where(
                        Source.id == body.edit_source_id,
                        Source.user_id == config.user_id,
                        Source.archived.is_(False),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if source is None or source.connection_version != body.expected_version:
                    raise HTTPException(409, "来源已被删除或修改，请重新检测")
                busy = session.scalar(
                    select(Job.id).where(
                        Job.source_id == source.id,
                        Job.status.in_(["queued", "running", "retrying"]),
                    )
                )
                if busy:
                    raise HTTPException(409, "来源正在同步，请等待完成后编辑连接")
                duplicate = session.scalar(
                    select(Source.id).where(
                        Source.user_id == config.user_id,
                        Source.kind == kind,
                        Source.url == actual,
                        Source.id != source.id,
                    )
                )
                if duplicate:
                    raise HTTPException(409, "此采集地址已有连接；可编辑或恢复原连接")
                # Every explicit platform re-verification invalidates older in-flight calls.
                # Replacement/clear must also work when an old key cannot be recovered.
                credentials_changed = content.credentials is not None
                if source.url != actual or source.kind != kind or credentials_changed:
                    source.connection_version += 1
                    source.cursor = {}
            else:
                source = session.scalar(
                    select(Source)
                    .where(
                        Source.user_id == config.user_id, Source.kind == kind, Source.url == actual
                    )
                    .with_for_update()
                )
                if source and not source.archived:
                    raise HTTPException(409, "此订阅已存在，本次未重复添加任何来源")
                if source is None:
                    source = Source(user_id=config.user_id, name="", kind=kind, url=actual)
                    session.add(source)
            source.name = selection.name.strip() or content.title[:200]
            source.kind, source.url = kind, actual
            source.input_url = draft.payload["input_url"]
            source.access_type, source.validated_at = content.format, now
            source.archived, source.enabled = False, body.enabled
            session.flush()
            source.credential_mode = "stored" if content.credentials else "anonymous"
            source.credentials_ciphertext = encrypt_values(
                content.credentials, source.user_id, source.id
            )
            pending.append((source, content))
        # Lock/validate all selected configurations before taking ingestion's user lock.
        for source, content in pending:
            job = Job(
                user_id=config.user_id,
                source_id=source.id,
                status="running",
                attempts=1,
                started_at=now,
            )
            session.add(job)
            session.flush()
            run = IngestionRun(
                user_id=config.user_id,
                source_id=source.id,
                job_id=job.id,
                attempt=1,
                status="running",
                started_at=now,
            )
            session.add(run)
            content.result.cursor = next_cursor(content.response) if content.response else {}
            store_items(session, source, content.result, run)
            run.status = job.status = "succeeded"
            run.finished_at = job.finished_at = datetime.now(UTC)
            session.flush()
            saved.append(
                {
                    "source": SourceRead.model_validate(source).model_dump(mode="json"),
                    "fetched_count": run.fetched_count,
                    "job_id": str(job.id),
                }
            )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "来源已存在或配置发生冲突，本次未保存任何连接") from None
    except SourceError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from None
    return {"items": saved, "auto_favorited": False}
