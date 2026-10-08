from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api import source_or_404
from app.config import Settings
from app.connectors.contracts import RetryableSourceError, SourceError
from app.connectors.http import SafeHTTP
from app.database import get_session
from app.models import (
    ContentItem,
    ContentOccurrence,
    ContentVersion,
    Favorite,
    OriginalMaterial,
    Source,
)
from app.platforms.contracts import DetailRequest, PlatformQuery
from app.platforms.registry import bound_platform, catalog
from app.platforms.service import document_response, store_document
from app.services.ingestion import content_external_id
from app.user_context import request_settings

router = APIRouter(prefix="/api/platforms", tags=["platform tools"])
DB = Annotated[Session, Depends(get_session)]
Config = Annotated[Settings, Depends(request_settings)]


def store_available(session, source, document):
    expected = source.connection_version
    current = session.scalar(
        select(Source)
        .where(Source.id == source.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current.archived or not current.enabled or current.connection_version != expected:
        raise HTTPException(409, "来源已停用或删除，未保存迟到的详情结果")
    return store_document(session, current, document)


def platform_source(session, source_id, config):
    source = source_or_404(session, source_id, config.user_id)
    if not source.enabled:
        raise HTTPException(409, "平台连接已停用")
    try:
        provider = bound_platform(source)
    except SourceError as exc:
        raise HTTPException(422, str(exc)) from None
    return source, provider


def safe_call(operation):
    try:
        return operation()
    except RetryableSourceError as exc:
        raise HTTPException(503, str(exc)) from None
    except SourceError as exc:
        raise HTTPException(422, str(exc)) from None
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        raise HTTPException(502, "平台返回数据格式异常，未把失败当作零条结果") from None


@router.get("")
def platforms():
    return catalog()


@router.get("/{source_id}/capabilities")
def capabilities(source_id: UUID, session: DB, config: Config):
    source, provider = platform_source(session, source_id, config)
    return {
        **next(r for r in catalog() if r["kind"] == provider.kind),
        "source_id": source.id,
        "credential_configured": source.credential_configured,
        "credential_mode": source.credential_mode,
    }


@router.post("/{source_id}/query")
def query(source_id: UUID, body: PlatformQuery, session: DB, config: Config):
    source, provider = platform_source(session, source_id, config)
    if body.operation not in provider.operations:
        raise HTTPException(422, "此平台不支持该操作，没有伪装成搜索成功")
    http = SafeHTTP()
    try:
        page = safe_call(lambda: provider.query(source.url, body, http))
        return {
            "platform": source.kind,
            "source_id": source.id,
            "items": [asdict(item) for item in page.items],
            "next_cursor": page.next_cursor,
            "scope": page.scope,
            "persisted": False,
        }
    finally:
        http.close()


@router.post("/{source_id}/detail")
def detail(source_id: UUID, body: DetailRequest, session: DB, config: Config):
    source, provider = platform_source(session, source_id, config)
    http = SafeHTTP()
    try:
        document = safe_call(lambda: provider.detail(source.url, body, http))
        version = store_available(session, source, document)
        response = document_response(session, version)
        session.commit()
        return response
    finally:
        http.close()


@router.post("/{source_id}/check-update")
def check_update(source_id: UUID, body: DetailRequest, session: DB, config: Config):
    source, provider = platform_source(session, source_id, config)
    http = SafeHTTP()
    try:
        document = safe_call(lambda: provider.detail(source.url, body, http))
    finally:
        http.close()
    item_id = session.scalar(
        select(ContentOccurrence.item_id).where(
            ContentOccurrence.source_id == source.id,
            ContentOccurrence.user_id == config.user_id,
            ContentOccurrence.external_id == content_external_id(source, document.item.external_id),
        )
    )
    old_version = (
        session.scalar(
            select(ContentVersion.id)
            .where(
                ContentVersion.item_id == item_id,
                ContentVersion.source_id == source.id,
                ContentVersion.user_id == config.user_id,
            )
            .order_by(ContentVersion.version_number.desc())
            .limit(1)
        )
        if item_id
        else None
    )
    version = store_available(session, source, document)
    result = document_response(session, version)
    session.commit()
    return {
        "changed": old_version is not None and str(old_version) != result["version_id"],
        "first_capture": old_version is None,
        "document": result,
    }


def visible_version(session, version_id, user_id):
    saved = select(Favorite.item_id).where(Favorite.user_id == user_id, Favorite.active.is_(True))
    version = session.scalar(
        select(ContentVersion)
        .join(ContentItem)
        .where(
            ContentVersion.id == version_id,
            ContentVersion.user_id == user_id,
            ContentItem.user_id == user_id,
            or_(ContentItem.candidate_expires_at > datetime.now(UTC), ContentItem.id.in_(saved)),
        )
    )
    if version is None:
        raise HTTPException(404, "材料不存在、候选已过期或不属于当前身份")
    return version


@router.get("/documents/{version_id}")
def stored_document(version_id: UUID, session: DB, config: Config):
    return document_response(session, visible_version(session, version_id, config.user_id))


@router.get("/materials/{material_id}")
def material(material_id: UUID, session: DB, config: Config):
    row = session.scalar(
        select(OriginalMaterial).where(
            OriginalMaterial.id == material_id,
            OriginalMaterial.user_id == config.user_id,
        )
    )
    if row is None:
        raise HTTPException(404, "原始材料不存在")
    visible_version(session, row.version_id, config.user_id)
    return Response(
        row.data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
