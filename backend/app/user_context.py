"""Local identity selection only. This is intentionally NOT authentication."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_session
from app.models import LocalIdentity


def request_settings(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[Session, Depends(get_session)],
    x_tiris_user: Annotated[UUID | None, Header()] = None,
) -> Settings:
    if x_tiris_user is None or x_tiris_user == settings.user_id:
        return settings
    if session.get(LocalIdentity, x_tiris_user) is None:
        raise HTTPException(404, "本地身份不存在，请重新选择")
    # Never mutate cached global configuration or worker settings.
    return settings.model_copy(update={"user_id": x_tiris_user})
