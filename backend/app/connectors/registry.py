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
)
from app.connectors.contracts import FetchResult, SourceError
from app.connectors.feeds import parse_feed
from app.connectors.http import SafeHTTP, canonicalize_url

Reader = Callable[[str, dict, SafeHTTP], FetchResult]
Validator = Callable[[str], None]


def validate_github(url: str):
    parsed = urlsplit(url)
    if parsed.hostname == "github.com" and parsed.path == "/":
        return
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname != "github.com" or len(parts) != 2 or not all(parts):
        raise SourceError("GitHub 来源应为平台 https://github.com/ 或具体仓库地址")
    if not all(re.fullmatch(r"[\w.-]+", part) for part in parts):
        raise SourceError("GitHub 仓库地址格式错误")


def validate_blackhat(url: str):
    if urlsplit(url).hostname not in {"blackhat.com", "www.blackhat.com"}:
        raise SourceError("Black Hat 来源必须是 blackhat.com 的公开索引地址")


def validate_arxiv(url: str):
    if urlsplit(url).hostname != "arxiv.org":
        raise SourceError("arXiv 平台连接请填写 https://arxiv.org/")


def platform_only_reader(url, cursor, http):
    raise SourceError("这是平台连接，请使用平台查询和详情工具；不是固定订阅同步")


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
    default_url: str | None = None

    def validate_url(self, url: str) -> str:
        normalized = canonicalize_url(url)
        if self.validator:
            self.validator(normalized)
        return normalized

    def fetch(self, url: str, cursor: dict, http: SafeHTTP) -> FetchResult:
        return self.reader(self.validate_url(url), cursor, http)

    def describe(self) -> dict:
        description = {
            "kind": self.kind,
            "label": self.label,
            "hint": self.hint,
            "placeholder": self.placeholder,
        }
        from app.platforms.registry import PLATFORMS

        if self.kind in PLATFORMS:
            description["platform_tools"] = True
        if self.default_url:
            description["default_url"] = self.default_url
        return description


CONNECTORS = {
    connector.kind: connector
    for connector in [
        SourceConnector(
            "rss",
            "RSS / Atom / JSON Feed",
            "通用订阅采集；网站地址可先通过统一发现接口检测。",
            "https://example.com/feed.xml",
            partial(fetch_feed, parser=parse_feed),
        ),
        SourceConnector(
            "github",
            "GitHub 平台 / 仓库",
            "平台连接填写 https://github.com/；也兼容具体仓库的旧同步。",
            "https://github.com/",
            fetch_github,
            validate_github,
            default_url="https://github.com/",
        ),
        SourceConnector(
            "blackhat",
            "Black Hat 索引",
            "填写 Black Hat 索引页；只读取公开索引，访问受限会显示错误。",
            "https://www.blackhat.com/html/archives.html",
            partial(fetch_feed, parser=parse_blackhat),
            validate_blackhat,
            default_url="https://www.blackhat.com/html/archives.html",
        ),
        SourceConnector(
            "arxiv",
            "arXiv 平台",
            "连接论文平台，不需要 Token。",
            "https://arxiv.org/",
            platform_only_reader,
            validate_arxiv,
            default_url="https://arxiv.org/",
        ),
        SourceConnector(
            "web",
            "网页 / 博客",
            "填写网站或列表地址；按需读取同站正文，遵守 robots.txt。",
            "https://example.com/",
            platform_only_reader,
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
