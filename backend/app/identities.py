"""Create/select local profiles without passwords, sessions, or login."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select

from app.api import DB
from app.config import Settings, get_settings
from app.models import LocalIdentity
from app.profiles.api import lock_preferences

router = APIRouter(prefix="/api/identities", tags=["local-identities"])
GlobalConfig = Annotated[Settings, Depends(get_settings)]


class IdentityName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


@router.get("")
def identities(session: DB, config: GlobalConfig):
    default = session.get(LocalIdentity, config.user_id)
    return {
        "default_id": config.user_id,
        "items": [{"id": config.user_id, "name": default.name if default else "我的身份"}]
        + [
            {"id": row.id, "name": row.name}
            for row in session.scalars(
                select(LocalIdentity)
                .where(LocalIdentity.id != config.user_id)
                .order_by(LocalIdentity.created_at, LocalIdentity.id)
            )
        ],
    }


@router.post("", status_code=201)
def create_identity(body: IdentityName, session: DB, config: GlobalConfig):
    lock_preferences(session, config.user_id)
    if session.scalar(select(func.count()).select_from(LocalIdentity)) >= 20:
        raise HTTPException(422, "本地身份最多 20 个")
    row = LocalIdentity(name=body.name)
    session.add(row)
    session.flush()
    # Every learning space starts empty; sources are explicitly added in that space.
    session.commit()
    return {"id": row.id, "name": row.name}


@router.patch("/{identity_id}")
def rename_identity(identity_id: UUID, body: IdentityName, session: DB, config: GlobalConfig):
    lock_preferences(session, config.user_id)
    row = session.get(LocalIdentity, identity_id)
    if row is None:
        if identity_id != config.user_id:
            raise HTTPException(404, "本地身份不存在")
        row = LocalIdentity(id=identity_id, name=body.name)
        session.add(row)
    else:
        row.name = body.name
    session.commit()
    return {"id": row.id, "name": row.name}
