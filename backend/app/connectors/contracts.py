from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawItem:
    external_id: str
    url: str
    title: str
    content_type: str
    clean_text: str = ""
    acquisition_level: str = "metadata_only"
    author: str | None = None
    published_at: datetime | None = None
    language: str = "unknown"
    metadata: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    items: list[RawItem]
    cursor: dict = field(default_factory=dict)
    rejected_count: int = 0
    not_modified: bool = False


class SourceError(Exception):
    """A safe, user-visible failure; never include credentials."""


class RetryableSourceError(SourceError):
    pass
