"""Small URL-to-public-feed adapters, independent of the user's add-source flow."""

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit


@dataclass
class FeedHint:
    url: str
    title: str
    method: str


class FeedAdapter(Protocol):
    def discover(self, url: str, document=None) -> list[FeedHint]: ...


class GitHubFeeds:
    def discover(self, url, document=None):
        parsed = urlsplit(url)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            return []
        parts = parsed.path.strip("/").split("/")
        if not all(re.fullmatch(r"[\w.-]+", part) for part in parts[:2]):
            return []
        if len(parts) == 1 and parts[0] not in {
            "explore",
            "topics",
            "trending",
            "search",
            "login",
            "features",
        }:
            return [FeedHint(f"https://github.com/{parts[0]}.atom", "公开活动", "github")]
        if len(parts) >= 2:
            base = f"https://github.com/{parts[0]}/{parts[1].removesuffix('.git')}"
            names = [("releases", "Releases"), ("commits", "Commits"), ("tags", "Tags")]
            if len(parts) > 2 and parts[2] in {name for name, _ in names}:
                names = [row for row in names if row[0] == parts[2]]
            return [FeedHint(f"{base}/{path}.atom", title, "github") for path, title in names]
        return []


class YouTubeFeeds:
    def discover(self, url, document=None):
        parsed = urlsplit(url)
        if parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            return []
        query = parse_qs(parsed.query)
        playlist = query.get("list", [""])[0]
        if re.fullmatch(r"[\w-]{10,100}", playlist):
            return [
                FeedHint(
                    "https://www.youtube.com/feeds/videos.xml?"
                    + urlencode({"playlist_id": playlist}),
                    "播放列表",
                    "youtube",
                )
            ]
        match = re.match(r"^/channel/(UC[\w-]{22})(?:/|$)", parsed.path)
        channel = match.group(1) if match else ""
        if not channel and document is not None:
            node = document.select_one('meta[itemprop="channelId"], meta[name="channelId"]')
            channel = node.get("content", "") if node else ""
            if not re.fullmatch(r"UC[\w-]{22}", channel):
                channel = ""
        if channel:
            return [
                FeedHint(
                    "https://www.youtube.com/feeds/videos.xml?"
                    + urlencode({"channel_id": channel}),
                    "频道更新",
                    "youtube",
                )
            ]
        return []


ADAPTERS: list[FeedAdapter] = [GitHubFeeds(), YouTubeFeeds()]


def register_feed_adapter(adapter: FeedAdapter):
    ADAPTERS.append(adapter)


def discover_special(url, document=None):
    return [hint for adapter in ADAPTERS for hint in adapter.discover(url, document)]
