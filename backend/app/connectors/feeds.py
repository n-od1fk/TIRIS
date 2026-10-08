"""Format detection and normalized public feeds; no network calls in parsers."""

import json
from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree

import feedparser

from app.connectors.adapters import parse_date, parse_rss, plain_text
from app.connectors.contracts import FetchResult, RawItem, SourceError
from app.connectors.http import canonicalize_url


@dataclass
class FeedDocument:
    format: str
    title: str
    result: FetchResult


def parse_document(data: bytes, url: str, limit: int = 100) -> FeedDocument:
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith((b"{", b"[")):
        return parse_json_feed(data, url, limit)
    declarations = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise SourceError("订阅 XML 含 DTD/实体声明，未安全解析")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise SourceError("不是有效的 RSS/Atom/JSON Feed") from None
    local = root.tag.rsplit("}", 1)[-1].lower()
    if local not in {"rss", "feed", "rdf"}:
        raise SourceError("不是有效的 RSS/Atom/JSON Feed")
    parsed = feedparser.parse(data)
    result = parse_rss(data, url, limit)
    if parsed.entries and not result.items:
        raise SourceError("订阅包含条目，但均缺少有效标题或原文链接")
    for item in result.items:
        item.metadata["feed_format"] = "atom" if local == "feed" else "rss"
    return FeedDocument(
        "atom" if local == "feed" else "rss",
        plain_text(parsed.feed.get("title", "")) or url,
        result,
    )


def parse_json_feed(data: bytes, url: str, limit: int) -> FeedDocument:
    try:
        feed = json.loads(data)
    except (ValueError, UnicodeError):
        raise SourceError("JSON Feed 不是有效 JSON") from None
    if (
        not isinstance(feed, dict)
        or not isinstance(feed.get("version"), str)
        or feed.get("version")
        not in {
            "https://jsonfeed.org/version/1",
            "https://jsonfeed.org/version/1.1",
        }
    ):
        raise SourceError(
            "返回了 JSON，但不是 JSON Feed；需要 API 文档和专用适配，不能自动理解任意 API"
        )
    if (
        not isinstance(feed.get("title"), str)
        or not feed["title"].strip()
        or not isinstance(feed.get("items"), list)
    ):
        raise SourceError("JSON Feed 缺少 title 或 items 列表")
    items, rejected = [], 0
    for entry in feed["items"][:limit]:
        try:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not entry["id"]
            ):
                raise ValueError
            link = entry.get("url") or entry.get("external_url")
            if not isinstance(link, str) or not link:
                raise ValueError
            for field in (
                "title",
                "content_text",
                "content_html",
                "summary",
                "language",
                "date_published",
                "date_modified",
            ):
                if (
                    field in entry
                    and entry[field] is not None
                    and not isinstance(entry[field], str)
                ):
                    raise ValueError
            original = canonicalize_url(urljoin(url, link))
            title = plain_text(entry.get("title") or "")
            title_missing = not title
            title = title or "未提供标题"
            content = (
                entry.get("content_text")
                or plain_text(entry.get("content_html") or "")
                or entry.get("summary")
                or ""
            )
            authors = entry.get("authors") or (
                [entry["author"]]
                if entry.get("author")
                else feed.get("authors") or ([feed["author"]] if feed.get("author") else [])
            )
            if not isinstance(authors, list):
                raise ValueError
            author = ", ".join(
                a["name"] for a in authors if isinstance(a, dict) and isinstance(a.get("name"), str)
            )
            items.append(
                RawItem(
                    entry["id"],
                    original,
                    title,
                    "article",
                    clean_text=content,
                    acquisition_level="abstract_only" if content else "metadata_only",
                    author=author or None,
                    published_at=parse_date(entry.get("date_published")),
                    language=entry.get("language")
                    or (
                        feed.get("language") if isinstance(feed.get("language"), str) else "unknown"
                    )
                    or "unknown",
                    metadata={
                        "feed_url": url,
                        "feed_format": "json_feed",
                        "title_missing": title_missing,
                        "updated_at": entry.get("date_modified"),
                        "evidence_scope": "JSON Feed 提供的正文或摘要，未核验原文全文",
                    },
                )
            )
        except (ValueError, TypeError, AttributeError, KeyError, SourceError):
            rejected += 1
    if feed["items"] and not items:
        raise SourceError("JSON Feed 条目缺少有效 ID 或原文链接，或字段类型不符合规范")
    return FeedDocument(
        "json_feed", plain_text(feed["title"]), FetchResult(items, rejected_count=rejected)
    )


def parse_feed(data: bytes, url: str, limit: int) -> FetchResult:
    return parse_document(data, url, limit).result
