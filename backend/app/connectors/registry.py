"""One connector contract for API validation, UI capabilities and worker dispatch.

New platforms implement Reader(url, cursor, http) -> FetchResult and register here.
All items still pass through the existing provenance/version/dedup ingestion service.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from urllib.parse import parse_qs, urlsplit

from app.connectors.adapters import (
    fetch_feed,
    fetch_github,
    fetch_github_search,
    parse_blackhat,
    parse_rss,
)
from app.connectors.contracts import FetchResult, SourceError
from app.connectors.http import SafeHTTP, canonicalize_url

Reader = Callable[[str, dict, SafeHTTP], FetchResult]
Validator = Callable[[str], None]


def validate_github(url: str):
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname != "github.com" or len(parts) != 2 or not all(parts):
        raise SourceError("GitHub 来源必须为 https://github.com/owner/repo")
    if not all(re.fullmatch(r"[\w.-]+", part) for part in parts):
        raise SourceError("GitHub 仓库地址格式错误")


def validate_blackhat(url: str):
    if urlsplit(url).hostname not in {"blackhat.com", "www.blackhat.com"}:
        raise SourceError("Black Hat 来源必须是 blackhat.com 的公开索引地址")


def validate_github_search(url: str):
    parsed = urlsplit(url)
    if (
        parsed.hostname != "api.github.com"
        or parsed.path != "/search/repositories"
        or not parse_qs(parsed.query).get("q")
    ):
        raise SourceError("GitHub 搜索地址无效")


@dataclass(frozen=True)
class SourceConnector:
    kind: str
    label: str
    hint: str
    placeholder: str
    reader: Reader
    validator: Validator | None = None
    addable: bool = True

    def validate_url(self, url: str) -> str:
        normalized = canonicalize_url(url)
        if self.validator:
            self.validator(normalized)
        return normalized

    def fetch(self, url: str, cursor: dict, http: SafeHTTP) -> FetchResult:
        return self.reader(self.validate_url(url), cursor, http)

    def describe(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "hint": self.hint,
            "placeholder": self.placeholder,
        }


CONNECTORS = {
    connector.kind: connector
    for connector in [
        SourceConnector(
            "rss",
            "RSS / Atom",
            "填写 RSS/Atom 订阅地址，不是网站首页。",
            "https://example.com/feed.xml",
            partial(fetch_feed, parser=parse_rss),
        ),
        SourceConnector(
            "github",
            "GitHub 仓库",
            "填写具体仓库地址，例如 https://github.com/owner/repo",
            "https://github.com/owner/repo",
            fetch_github,
            validate_github,
        ),
        SourceConnector(
            "blackhat",
            "Black Hat 索引",
            "填写 Black Hat 索引页；只读取公开索引，访问受限会显示错误。",
            "https://www.blackhat.com/html/archives.html",
            partial(fetch_feed, parser=parse_blackhat),
            validate_blackhat,
        ),
        SourceConnector(
            "github_search",
            "GitHub 兴趣搜索",
            "内部兴趣检索，需要另行明确授权。",
            "",
            fetch_github_search,
            validate_github_search,
            addable=False,
        ),
    ]
}


def get_connector(kind: str, *, for_creation: bool = False) -> SourceConnector:
    connector = CONNECTORS.get(kind)
    if connector is None:
        raise SourceError("尚不支持此来源类型")
    if for_creation and not connector.addable:
        raise SourceError("此来源类型仅供内部检索，不可手动添加")
    return connector


def public_source_types() -> list[dict]:
    return [connector.describe() for connector in CONNECTORS.values() if connector.addable]
