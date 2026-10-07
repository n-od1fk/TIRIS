import hashlib
import unicodedata
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_session
from app.models import FixedColumn, LongTermProfile, ProfileRevision, Source
from app.profiles.schemas import (
    ColumnData,
    ColumnPatch,
    ColumnRead,
    ProfileData,
    ProfilePatch,
    ProfileRead,
    RevisionRead,
)
from app.user_context import request_settings

router = APIRouter(prefix="/api", tags=["profile-settings"])
DB = Annotated[Session, Depends(get_session)]
Config = Annotated[Settings, Depends(request_settings)]


def lock_preferences(session: Session, user_id: UUID):
    # Separate from ingestion locks; serializes first creation and quota checks as well as edits.
    key = int.from_bytes(
        hashlib.sha256(f"preferences:{user_id}".encode()).digest()[:8], "big", signed=True
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def profile_read(profile: LongTermProfile | None) -> ProfileRead:
    if profile is None:
        return ProfileRead(version=0, data=ProfileData(), field_metadata={}, updated_at=None)
    return ProfileRead(
        version=profile.version,
        data=ProfileData.model_validate(profile.data),
        field_metadata=profile.field_metadata,
        updated_at=profile.updated_at,
    )


def require_version(expected: int, actual: int):
    if expected != actual:
        raise HTTPException(
            409, {"message": "配置已被另一页面修改，请重新载入后再保存", "current_version": actual}
        )


@router.get("/profile", response_model=ProfileRead)
def get_profile(session: DB, config: Config):
    # GET returns an unsaved empty profile instead of writing inferred/default user data.
    return profile_read(
        session.scalar(select(LongTermProfile).where(LongTermProfile.user_id == config.user_id))
    )


@router.patch("/profile", response_model=ProfileRead)
def patch_profile(body: ProfilePatch, session: DB, config: Config):
    lock_preferences(session, config.user_id)
    profile = session.scalar(
        select(LongTermProfile).where(LongTermProfile.user_id == config.user_id)
    )
    before = profile_read(profile)
    require_version(body.expected_version, before.version)
    patch = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    data = before.data.model_dump()
    changes = {key: value for key, value in patch.items() if data[key] != value}
    if not changes:
        return before
    now = datetime.now(UTC)
    data = ProfileData.model_validate({**data, **changes}).model_dump()
    metadata = dict(profile.field_metadata) if profile else {}
    for field in changes:
        metadata[field] = {"source": "user_explicit", "updated_at": now.isoformat()}
    if profile is None:
        profile = LongTermProfile(
            user_id=config.user_id, data=data, field_metadata=metadata, version=1, updated_at=now
        )
        session.add(profile)
        session.flush()
    else:
        profile.data, profile.field_metadata, profile.updated_at = data, metadata, now
        profile.version += 1
    session.add(
        ProfileRevision(
            user_id=config.user_id,
            profile_id=profile.id,
            version=profile.version,
            data=data,
            field_metadata=metadata,
            changed_fields=sorted(changes),
            created_at=now,
        )
    )
    session.commit()
    return profile_read(profile)


@router.get("/profile/history", response_model=list[RevisionRead])
def profile_history(
    session: DB, config: Config, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)
):
    return session.scalars(
        select(ProfileRevision)
        .where(ProfileRevision.user_id == config.user_id)
        .order_by(ProfileRevision.version.desc())
        .offset(offset)
        .limit(limit)
    ).all()


def name_key(name: str) -> str:
    key = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    if len(key) > 100:
        raise HTTPException(422, "栏目名称规范化后超过 100 字符，请使用更短的名称")
    return key


def validate_column(
    session: Session, user_id: UUID, data: ColumnData, exclude_id: UUID | None = None
):
    duplicate = session.scalar(
        select(FixedColumn).where(
            FixedColumn.user_id == user_id,
            FixedColumn.name_key == name_key(data.name),
            FixedColumn.id != exclude_id if exclude_id else True,
        )
    )
    if duplicate:
        raise HTTPException(409, "此栏目名称已存在，可编辑原栏目")
    if data.source_ids:
        owned = set(
            session.scalars(
                select(Source.id).where(
                    Source.user_id == user_id,
                    Source.archived.is_(False),
                    Source.id.in_(data.source_ids),
                )
            ).all()
        )
        if owned != set(data.source_ids):
            raise HTTPException(404, "栏目中有不存在或不属于当前用户的来源")
    total = session.scalar(
        select(func.coalesce(func.sum(FixedColumn.items_per_day), 0)).where(
            FixedColumn.user_id == user_id,
            FixedColumn.enabled.is_(True),
            FixedColumn.id != exclude_id if exclude_id else True,
        )
    )
    if data.enabled and total + data.items_per_day > 10:
        raise HTTPException(422, "启用栏目目标条数合计不能超过当前每日预算 10 条")


@router.get("/fixed-columns", response_model=list[ColumnRead])
def list_columns(session: DB, config: Config):
    return session.scalars(
        select(FixedColumn)
        .where(FixedColumn.user_id == config.user_id)
        .order_by(FixedColumn.created_at, FixedColumn.id)
    ).all()


@router.post("/fixed-columns", response_model=ColumnRead, status_code=201)
def create_column(body: ColumnData, session: DB, config: Config):
    lock_preferences(session, config.user_id)
    count = session.scalar(
        select(func.count()).select_from(FixedColumn).where(FixedColumn.user_id == config.user_id)
    )
    if count >= 20:
        raise HTTPException(422, "第一版最多保存 20 个栏目，请编辑已有栏目")
    validate_column(session, config.user_id, body)
    column = FixedColumn(
        user_id=config.user_id,
        **body.model_dump(mode="json"),
        name_key=name_key(body.name),
        updated_at=datetime.now(UTC),
    )
    session.add(column)
    session.commit()
    return column


@router.patch("/fixed-columns/{column_id}", response_model=ColumnRead)
def patch_column(column_id: UUID, body: ColumnPatch, session: DB, config: Config):
    lock_preferences(session, config.user_id)
    column = session.scalar(
        select(FixedColumn).where(
            FixedColumn.id == column_id, FixedColumn.user_id == config.user_id
        )
    )
    if column is None:
        raise HTTPException(404, "栏目不存在")
    require_version(body.expected_version, column.version)
    current = ColumnData.model_validate(column, from_attributes=True).model_dump(mode="json")
    changes = body.model_dump(mode="json", exclude_unset=True, exclude={"expected_version"})
    try:
        data = ColumnData.model_validate({**current, **changes})
    except ValidationError:
        raise HTTPException(422, "栏目配置无效：至少保留一个匹配条件") from None
    validate_column(session, config.user_id, data, column_id)
    if current != data.model_dump(mode="json"):
        for field, value in data.model_dump(mode="json").items():
            setattr(column, field, value)
        column.name_key, column.updated_at = name_key(column.name), datetime.now(UTC)
        column.version += 1
        session.commit()
    return column
