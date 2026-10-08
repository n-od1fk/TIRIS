"""Bounded, read-only discovery. A URL hint is never a verified connection."""

import time
from dataclasses import asdict
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup

from app.connectors.contracts import SourceError
from app.connectors.feed_adapters import FeedHint, discover_special
from app.connectors.feeds import parse_document
from app.connectors.http import canonicalize_url

MAX_CANDIDATES = 8


def input_address(value):
    value = value.strip()
    if "://" not in value and not value.startswith(("file:", "javascript:", "data:")):
        value = "https://" + value
    return canonicalize_url(value)


def validate_feed(url, http):
    response = http.get(url)
    actual = canonicalize_url(str(response.request.url))
    feed = parse_document(response.content, actual)
    return feed, actual, response


def preview(feed):
    rows = []
    for item in feed.result.items[:3]:
        row = asdict(item)
        row["published_at"] = item.published_at.isoformat() if item.published_at else None
        row["clean_text"] = row["clean_text"][:1200]
        rows.append(row)
    return rows


def discover_sources(value, http):
    original = input_address(value)
    from app.platforms.registry import connection_info

    platform = connection_info(original)
    if platform:
        return {
            "input_url": original,
            "effective_url": platform["url"],
            "platform": platform,
            "candidates": [],
            "warnings": [],
            "needs": [],
            "message": "已识别平台 API 接入方式，请填写所需凭证并验证连接；尚未保存",
        }
    candidates, hints, warnings, seen = [], [], [], set()
    deadline = time.monotonic() + 45
    special = discover_special(original)
    if urlsplit(original).path.lower().endswith((".atom", ".xml", ".rss", ".json")):
        special = []
    # Known public transformations can work even when the platform's home page is blocked.
    if special:
        hints.extend(special)
        effective = original
    else:
        response = http.get(original)
        effective = canonicalize_url(str(response.request.url))
        try:
            feed = parse_document(response.content, effective)
        except SourceError as exc:
            if (
                "html" not in response.headers.get("content-type", "").lower()
                and b"<html" not in response.content[:1000].lower()
                and b"<!doctype html" not in response.content[:1000].lower()
            ):
                return {
                    "input_url": original,
                    "effective_url": effective,
                    "candidates": [],
                    "warnings": [],
                    "message": str(exc),
                    "needs": ["有效订阅地址；若为专有 API，请提供 API 文档及所需 API Key/授权信息"],
                }
        else:
            return {
                "input_url": original,
                "effective_url": effective,
                "candidates": [
                    {
                        "id": str(uuid4()),
                        "url": effective,
                        "title": feed.title,
                        "access_type": feed.format,
                        "method": "direct",
                        "verified": True,
                        "preview": preview(feed),
                        "error": "",
                    }
                ],
                "warnings": [],
                "message": "订阅源已验证，可选择保存",
                "needs": [],
            }
        soup = BeautifulSoup(response.content, "html.parser")
        base_node = soup.select_one("base[href]")
        base = urljoin(effective, base_node["href"]) if base_node else effective
        for link in soup.select("link[href][type]"):
            media = link.get("type", "").split(";")[0].strip().lower()
            if media in {
                "application/rss+xml",
                "application/atom+xml",
                "application/feed+json",
                "application/json",
            }:
                hints.append(
                    FeedHint(urljoin(base, link["href"]), link.get("title", "订阅源"), "html_link")
                )
        hints.extend(discover_special(effective, soup))
        if not hints:
            if urlsplit(effective).hostname in {
                "github.com",
                "www.github.com",
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
            }:
                warnings.append(
                    "平台首页没有可代表整个平台的公开订阅；请提供 GitHub 仓库/用户地址"
                    "或 YouTube 频道/播放列表地址"
                )
            else:
                hints.extend(
                    FeedHint(urljoin(effective, path), path, "common_path")
                    for path in ("feed", "rss.xml", "atom.xml", "feed.json")
                )
    for hint in hints:
        if len(candidates) >= MAX_CANDIDATES or time.monotonic() > deadline:
            warnings.append(
                "达到本次检测数量/时间预算，未声称检查了全部订阅；可直接输入遗漏的订阅地址"
            )
            break
        try:
            target = canonicalize_url(hint.url)
        except SourceError:
            continue
        if target in seen:
            continue
        seen.add(target)
        row = {
            "id": str(uuid4()),
            "url": target,
            "title": hint.title,
            "method": hint.method,
            "verified": False,
            "preview": [],
            "access_type": "unknown",
            "error": "",
        }
        try:
            feed, actual, _ = validate_feed(target, http)
            if any(c["verified"] and c["url"] == actual for c in candidates):
                continue
            row.update(
                url=actual,
                title=feed.title,
                access_type=feed.format,
                verified=True,
                preview=preview(feed),
            )
        except SourceError as exc:
            row["error"] = str(exc)
        candidates.append(row)
    valid = any(c["verified"] for c in candidates)
    return {
        "input_url": original,
        "effective_url": effective,
        "candidates": candidates,
        "warnings": warnings,
        "message": "请选择已验证订阅源保存"
        if valid
        else "未找到可验证接入方式；识别为网页不等于接入成功",
        "needs": []
        if valid
        else [
            "网站公开的 RSS/Atom/JSON Feed 地址，或平台 API 文档和所需授权信息；"
            "当前不支持自动推断任意 API"
        ],
    }


def main():
    """Read-only network check: no discovery drafts, sources or articles are saved."""
    import argparse
    import json

    from app.connectors.http import SafeHTTP

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("url", help="Public website, section or feed URL")
    args = parser.parse_args()
    http = SafeHTTP()
    try:
        result = discover_sources(args.url, http)
        for candidate in result["candidates"]:
            candidate["preview_count"] = len(candidate.pop("preview"))
        print(json.dumps(result, ensure_ascii=False))
        if not any(row["verified"] for row in result["candidates"]):
            raise SystemExit(1)
    except SourceError as exc:
        print(json.dumps({"verified": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None
    finally:
        http.close()


if __name__ == "__main__":
    main()
