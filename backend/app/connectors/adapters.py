import calendar
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urljoin, urlsplit

import feedparser
from bs4 import BeautifulSoup

from app.config import get_settings
from app.connectors.contracts import FetchResult, RawItem, SourceError
from app.connectors.http import SafeHTTP, canonicalize_url


def plain_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "iframe", "noscript"]):
        element.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


def conditional_headers(cursor: dict) -> dict:
    headers = {}
    if cursor.get("etag"):
        headers["If-None-Match"] = cursor["etag"]
    if cursor.get("last_modified"):
        headers["If-Modified-Since"] = cursor["last_modified"]
    return headers


def next_cursor(response) -> dict:
    return {
        key: response.headers.get(header)
        for key, header in [("etag", "ETag"), ("last_modified", "Last-Modified")]
    }


def parse_rss(data: bytes, feed_url: str, limit: int) -> FetchResult:
    parsed = feedparser.parse(data)
    if not parsed.get("version"):
        raise SourceError("来源不是可识别的 RSS/Atom；请填写订阅源地址")
    items = []
    rejected = 0
    for entry in parsed.entries[:limit]:
        try:
            if not entry.get("link") or not entry.get("title"):
                raise ValueError
            url = canonicalize_url(urljoin(feed_url, entry.link))
            published = entry.get("published_parsed")
            published_at = (
                datetime.fromtimestamp(calendar.timegm(published), UTC) if published else None
            )
            content = entry.get("content") or []
            excerpt = content[0].get("value", "") if content else entry.get("summary", "")
            clean = plain_text(excerpt)
            items.append(
                RawItem(
                    external_id=entry.get("id") or url,
                    url=url,
                    title=plain_text(entry.title),
                    content_type="article",
                    clean_text=clean,
                    author=entry.get("author"),
                    published_at=published_at,
                    acquisition_level="abstract_only" if clean else "metadata_only",
                    language=parsed.feed.get("language", "unknown"),
                    metadata={
                        "evidence_scope": "RSS/Atom 提供的内容，未核验原文全文",
                        "updated_at": entry.get("updated"),
                        "feed_url": feed_url,
                    },
                )
            )
        except (ValueError, TypeError, OverflowError, SourceError):
            rejected += 1
    if parsed.bozo and not items:
        raise SourceError("RSS/Atom 解析失败，未把失败当作零条新内容")
    return FetchResult(items, rejected_count=rejected)


def parse_blackhat(data: bytes, page_url: str, limit: int) -> FetchResult:
    soup = BeautifulSoup(data, "html.parser")
    items = []
    seen = set()
    for link in soup.select("a[href]"):
        url = urljoin(page_url, link["href"])
        parsed = urlsplit(url)
        label = " ".join(link.get_text(" ", strip=True).split())
        if parsed.hostname not in {"blackhat.com", "www.blackhat.com"} or not label or url in seen:
            continue
        if not re.search(
            r"(briefings|arsenal|archives|presentations|materials)", parsed.path, re.I
        ):
            continue
        if canonicalize_url(url) == canonicalize_url(page_url):
            continue
        seen.add(url)
        items.append(
            RawItem(
                external_id=canonicalize_url(url),
                url=url,
                title=label,
                content_type="conference_index",
                acquisition_level="metadata_only",
                metadata={
                    "evidence_scope": "Black Hat 公开索引链接，尚未读取演讲材料",
                    "index_url": page_url,
                },
            )
        )
        if len(items) == limit:
            break
    if not items:
        raise SourceError("未识别到 Black Hat 档案链接；页面可能改版，需要检查适配器")
    return FetchResult(items)


def fetch_github_search(url: str, cursor: dict, http: SafeHTTP) -> FetchResult:
    settings = get_settings()
    parsed = urlsplit(url)
    if (
        parsed.hostname != "api.github.com"
        or parsed.path != "/search/repositories"
        or not parse_qs(parsed.query).get("q")
    ):
        raise SourceError("GitHub 搜索地址无效")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": settings.github_api_version,
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        data = http.get(url, headers).json()
        if not isinstance(data.get("items"), list) or data.get("incomplete_results"):
            raise SourceError("GitHub 搜索结果不完整，请稍后手动重试")
        return FetchResult(
            [
                RawItem(
                    external_id=f"repo:{row['id']}",
                    url=row["html_url"],
                    title=row["full_name"],
                    content_type="repository",
                    clean_text=row.get("description") or "",
                    author=row["owner"]["login"],
                    published_at=parse_date(row.get("created_at")),
                    metadata={
                        "evidence_scope": (
                            "GitHub 搜索返回的仓库元数据，未读取 README；不是新闻全文"
                        ),
                        "topics": row.get("topics", []),
                        "updated_at": row.get("updated_at"),
                    },
                )
                for row in data["items"][:10]
                if not row.get("private")
            ]
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError("GitHub 搜索返回格式异常") from exc


def fetch_feed(url: str, cursor: dict, http: SafeHTTP, *, parser) -> FetchResult:
    response = http.get(url, conditional_headers(cursor))
    if response.status_code == 304:
        return FetchResult([], cursor=cursor, not_modified=True)
    result = parser(response.content, url, get_settings().fetch_max_items)
    result.cursor = next_cursor(response)
    return result


def fetch_github(url: str, cursor: dict, http: SafeHTTP) -> FetchResult:
    settings = get_settings()
    owner, repo = urlsplit(url).path.strip("/").split("/")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": settings.github_api_version,
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        data = http.get(f"https://api.github.com/repos/{owner}/{repo}", headers).json()
        item = RawItem(
            external_id=f"repo:{data['id']}",
            url=data["html_url"],
            title=data["full_name"],
            content_type="repository",
            clean_text=data.get("description") or "",
            author=data["owner"]["login"],
            published_at=parse_date(data.get("created_at")),
            metadata={
                "evidence_scope": "GitHub 仓库元数据，尚未读取 README",
                "topics": data.get("topics", []),
                "updated_at": data.get("updated_at"),
            },
        )
        releases = http.get(
            f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=10", headers
        ).json()
        items = [item]
        for release in releases:
            if release.get("draft"):
                continue
            items.append(
                RawItem(
                    external_id=f"release:{release['id']}",
                    url=release["html_url"],
                    title=release.get("name") or release["tag_name"],
                    content_type="release",
                    clean_text=release.get("body") or "",
                    acquisition_level="full_text" if release.get("body") else "metadata_only",
                    author=(release.get("author") or {}).get("login"),
                    published_at=parse_date(release.get("published_at")),
                    metadata={
                        "evidence_scope": "GitHub Release 说明",
                        "repository": item.url,
                        "tag": release["tag_name"],
                    },
                )
            )
        return FetchResult(items)
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError("GitHub API 返回格式异常，请检查适配器") from exc


def fetch_source(kind: str, url: str, cursor: dict, http: SafeHTTP) -> FetchResult:
    """Compatibility entry shared by worker/discovery; all dispatch goes via registry."""
    from app.connectors.registry import get_connector

    return get_connector(kind).fetch(url, cursor, http)
