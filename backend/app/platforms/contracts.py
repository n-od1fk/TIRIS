import hashlib
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.connectors.contracts import RawItem, SourceError
from app.connectors.http import SafeHTTP


class PlatformQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["list", "search", "stars", "releases"] = "list"
    query: str = Field(default="", max_length=500)
    subject: str = Field(default="", max_length=2000)
    cursor: str = Field(default="", max_length=100)
    limit: int = Field(default=10, ge=1, le=30)


class DetailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2000)
    download_attachments: bool = False


@dataclass
class Material:
    filename: str
    media_type: str
    original_url: str
    data: bytes

    def describe(self):
        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "original_url": self.original_url,
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "size": len(self.data),
        }


@dataclass
class Document:
    item: RawItem
    status: Literal["full", "partial", "unavailable"]
    reason: str = ""
    materials: list[Material] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)

    def prepare(self):
        self.item.acquisition_level = (
            "full_text"
            if self.status == "full"
            else "abstract_only"
            if self.item.clean_text
            else "metadata_only"
        )
        self.item.metadata.update(
            {
                "content_status": self.status,
                "content_reason": self.reason,
                "materials": [m.describe() for m in self.materials],
                "attachments": self.attachments,
                "sections": self.sections,
                "summary_status": "not_generated",
            }
        )


@dataclass
class Page:
    items: list[RawItem]
    next_cursor: str | None = None
    scope: str = ""


class Platform(Protocol):
    kind: str
    label: str
    operations: tuple[str, ...]

    def query(self, source_url: str, request: PlatformQuery, http: SafeHTTP) -> Page: ...

    def detail(self, source_url: str, request: DetailRequest, http: SafeHTTP) -> Document: ...


def page_number(value: str) -> int:
    if value and (not value.isdigit() or not 1 <= int(value) <= 1000):
        raise SourceError("分页游标无效")
    return int(value or "1")
