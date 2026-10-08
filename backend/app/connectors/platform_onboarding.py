"""Platform recognition, credential validation and source-bound configuration helpers."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from app.connectors.contracts import FetchResult, SourceError
from app.connectors.source_discovery import preview
from app.models import Source, SourceDiscovery
from app.platforms.registry import get_platform
from app.services.credentials import decrypt_values, encrypt_values, source_values


class VerifyPlatformRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    discovery_id: UUID
    credentials: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    credential_action: Literal["keep", "replace", "clear"] = "keep"
    edit_source_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)


@dataclass
class PreparedConnection:
    kind: str
    url: str
    format: str
    title: str
    result: FetchResult
    response: object | None = None
    credentials: dict | None = field(default=None, repr=False)


def verified_platform(info, values, http):
    provider = get_platform(info["kind"])
    binder, verifier = (
        getattr(provider, "with_credentials", None),
        getattr(provider, "verify_connection", None),
    )
    if not binder or not verifier:
        raise SourceError("此平台尚未实现凭证验证，不能仅保存地址就声称接入成功")
    page = binder(values).verify_connection(info["url"], http)
    return PreparedConnection(
        info["kind"],
        info["url"],
        "platform_api",
        info["title"],
        FetchResult(page.items),
        credentials=values,
    )


def verify_platform(body, session, config, http):
    draft = session.scalar(
        select(SourceDiscovery).where(
            SourceDiscovery.id == body.discovery_id,
            SourceDiscovery.user_id == config.user_id,
            SourceDiscovery.expires_at > datetime.now(UTC),
        )
    )
    if draft is None or "platform" not in draft.payload or draft.payload.get("mode"):
        raise HTTPException(404, "平台识别结果不存在或已过期，请重新检测")
    info = draft.payload["platform"]
    allowed = {field["name"] for field in info["credential_fields"]}
    if set(body.credentials) - allowed:
        raise HTTPException(422, "包含此平台不支持的凭证字段")
    supplied = {name: value.get_secret_value().strip() for name, value in body.credentials.items()}
    if any(len(value) > 4096 or any(c in value for c in "\r\n\x00") for value in supplied.values()):
        raise HTTPException(422, "凭证长度或格式不正确")
    if body.credentials and body.credential_action != "replace":
        raise HTTPException(422, "提交新凭证时请选择 replace，不接受含糊的覆盖操作")
    existing = None
    if body.edit_source_id:
        existing = session.scalar(
            select(Source).where(
                Source.id == body.edit_source_id,
                Source.user_id == config.user_id,
                Source.archived.is_(False),
            )
        )
        if existing is None:
            raise HTTPException(404, "待编辑来源不存在")
        if body.expected_version is None or existing.connection_version != body.expected_version:
            raise HTTPException(409, "来源连接已变化，请刷新后重新检测")
    try:
        values = (
            supplied
            if body.credential_action == "replace"
            else (
                source_values(existing)
                if existing and existing.kind == info["kind"] and body.credential_action == "keep"
                else {}
            )
        )
        values = {k: v for k, v in values.items() if v}
        for field in info["credential_fields"]:
            if field["required"] and not values.get(field["name"]):
                raise SourceError("缺少平台要求的凭证，请补充后验证")
        content = verified_platform(info, values, http)
        record = SourceDiscovery(
            id=uuid4(),
            user_id=config.user_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            payload={
                "mode": "platform_verified",
                "platform": info,
                "input_url": draft.payload["input_url"],
                "binding": {
                    "edit_source_id": str(body.edit_source_id) if body.edit_source_id else None,
                    "expected_version": body.expected_version,
                },
                "candidates": [
                    {
                        "id": str(uuid4()),
                        "url": content.url,
                        "title": content.title,
                        "access_type": "platform_api",
                        "method": "platform_api",
                        "verified": True,
                        "error": "",
                        "preview": preview(content),
                    }
                ],
                "warnings": [],
                "needs": [],
                "message": "平台 API 已实际验证，可保存；预览是公共仓库元数据，"
                "不是新闻或 README 全文",
            },
        )
        record.credentials_ciphertext = encrypt_values(values, config.user_id, record.id)
    except SourceError as exc:
        raise HTTPException(422, f"平台连接验证失败：{exc}；未保存来源或更改已有凭证") from None
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise HTTPException(502, "平台返回格式异常，未保存配置") from None
    session.add(record)
    session.commit()
    return {
        **record.payload,
        "discovery_id": record.id,
        "expires_at": record.expires_at,
        "credential_configured": bool(values),
    }


def revalidate_platform(draft, body, http):
    binding = draft.payload["binding"]
    if len(body.selections) != 1 or binding != {
        "edit_source_id": str(body.edit_source_id) if body.edit_source_id else None,
        "expected_version": body.expected_version,
    }:
        raise SourceError("本次凭证验证绑定了其他编辑目标，请重新验证")
    values = decrypt_values(draft.credentials_ciphertext, draft.user_id, draft.id)
    return verified_platform(draft.payload["platform"], values, http)
