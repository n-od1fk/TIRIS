import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.connectors import source_api
from app.connectors.adapters import fetch_source
from app.connectors.contracts import SourceError
from app.connectors.feed_adapters import ADAPTERS, FeedHint, discover_special, register_feed_adapter
from app.connectors.feeds import parse_document
from app.connectors.http import SafeHTTP
from app.connectors.source_discovery import discover_sources
from app.models import ContentItem, ContentOccurrence, Job, LocalIdentity, Source, SourceDiscovery
from app.workers import tasks

BASE = "https://example.com/"
RSS = b"""<rss version="2.0"><channel><title>Public news</title><link>https://example.com/</link>
<description>News</description><item><guid>one</guid><title>First story</title>
<link>https://example.com/story</link><author>Alice</author>
<pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate>
<description>Evidence, not a generated summary.</description>
</item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom news</title><id>feed</id>
<updated>2026-10-08T10:00:00Z</updated><entry><id>two</id><title>Second story</title>
<link href="https://example.com/second"/><author><name>Bob</name></author>
<published>2026-10-08T10:00:00Z</published><summary>Atom excerpt</summary></entry></feed>"""
JSON = json.dumps(
    {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "JSON news",
        "authors": [{"name": "Carol"}],
        "items": [
            {
                "id": "three",
                "url": "third",
                "title": "Third story",
                "content_html": "<p>Body</p><script>untrusted()</script>",
                "date_published": "2026-10-08T10:00:00Z",
            }
        ],
    }
).encode()


class Network:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, *args, **kwargs):
        self.calls.append(url)
        value = self.routes.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise SourceError("HTTP 404")
        if isinstance(value, tuple):
            content, actual, media = value
        else:
            content, actual, media = value, url, "application/octet-stream"
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": media, "ETag": "v1"},
            request=httpx.Request("GET", actual),
        )

    def close(self):
        pass


@pytest.mark.parametrize(
    "body,format,title,author",
    [
        (RSS, "rss", "First story", "Alice"),
        (ATOM, "atom", "Second story", "Bob"),
        (JSON, "json_feed", "Third story", "Carol"),
    ],
)
def test_direct_formats_normalized_without_trusting_mime(body, format, title, author):
    detected = discover_sources(BASE + "feed", Network({BASE + "feed": body}))
    row = detected["candidates"][0]
    assert row["verified"] and row["access_type"] == format
    item = row["preview"][0]
    assert item["title"] == title and item["author"] == author
    assert item["published_at"].startswith("2026-10-08")
    assert item["acquisition_level"] == "abstract_only"
    assert "untrusted" not in item["clean_text"]


def test_html_redirect_base_relative_links_duplicates_and_failed_candidate():
    html = b"""<html><head><base href="/feeds/"/>
    <link rel="alternate" type="application/rss+xml" href="news.xml"/>
    <link type="application/atom+xml; charset=utf-8" href="../atom"/>
    <link type="application/feed+json" href="https://example.com/json"/>
    <link type="application/rss+xml" href="news.xml"/>
    <link type="application/rss+xml" href="broken.xml"/></head></html>"""
    network = Network(
        {
            BASE: (html, BASE + "blog/", "text/html"),
            BASE + "feeds/news.xml": (RSS, BASE + "actual.xml", "text/html"),
            BASE + "atom": ATOM,
            BASE + "json": JSON,
        }
    )
    result = discover_sources("example.com", network)
    assert result["input_url"] == BASE and result["effective_url"] == BASE + "blog/"
    assert [r["access_type"] for r in result["candidates"]] == [
        "rss",
        "atom",
        "json_feed",
        "unknown",
    ]
    assert result["candidates"][0]["url"] == BASE + "actual.xml"
    assert result["candidates"][-1]["verified"] is False
    assert network.calls.count(BASE + "feeds/news.xml") == 1


def test_html_is_not_a_connection_and_common_paths_are_actually_verified():
    network = Network({BASE: (b"<html>No subscriptions</html>", BASE, "text/html")})
    result = discover_sources(BASE, network)
    assert not any(r["verified"] for r in result["candidates"])
    assert result["needs"] and "不等于接入成功" in result["message"]
    network.routes[BASE + "feed.json"] = JSON
    result = discover_sources(BASE, network)
    assert result["candidates"][-1]["access_type"] == "json_feed"


@pytest.mark.parametrize(
    "bad",
    [
        b'{"items":[]}',
        b'{"version":[],"items":[]}',
        b"<html>Not feed</html>",
        b"<rss>",
        b'<!DOCTYPE rss [<!ENTITY x "body">]><rss version="2.0"><channel/></rss>',
        '<!DOCTYPE rss [<!ENTITY x "body">]><rss version="2.0"><channel/></rss>'.encode("utf-16"),
    ],
)
def test_not_arbitrary_json_html_or_unsafe_xml(bad):
    with pytest.raises(SourceError):
        parse_document(bad, BASE)


def test_json_invalid_items_skip_with_counts_and_empty_feed_is_valid():
    data = json.loads(JSON)
    data["items"] += [
        {"id": "bad", "url": "javascript:alert(1)", "title": "bad"},
        {"id": "wrong", "url": BASE, "title": "wrong", "content_text": ["bad"]},
    ]
    parsed = parse_document(json.dumps(data).encode(), BASE)
    assert len(parsed.result.items) == 1 and parsed.result.rejected_count == 2
    data["items"] = []
    assert parse_document(json.dumps(data).encode(), BASE).result.items == []


def test_json_optional_title_is_marked_missing_not_fabricated():
    data = json.loads(JSON)
    del data["items"][0]["title"]
    item = parse_document(json.dumps(data).encode(), BASE).result.items[0]
    assert item.title == "未提供标题" and item.metadata["title_missing"]
    assert item.clean_text == "Body"


def test_github_and_youtube_transformations_still_require_valid_feed():
    github = "https://github.com/miniflux/v2"
    hints = discover_special(github)
    assert [h.url for h in hints] == [
        github + "/" + name + ".atom" for name in ("releases", "commits", "tags")
    ]
    result = discover_sources(github, Network({hints[0].url: ATOM}))
    assert [r["verified"] for r in result["candidates"]] == [True, False, False]
    assert not discover_special("https://github.com/")
    assert not discover_special("https://github.com.attacker.example/a/b")
    channel = "UC" + "a" * 22
    url = "https://www.youtube.com/channel/" + channel
    hint = discover_special(url)[0]
    assert hint.url.endswith("channel_id=" + channel)
    assert discover_sources(url, Network({hint.url: ATOM}))["candidates"][0]["verified"]
    assert (
        "playlist_id=PL123456789012"
        in discover_special("https://youtube.com/playlist?list=PL123456789012")[0].url
    )


def test_adapter_extension_does_not_change_discovery_workflow(monkeypatch):
    class Extra:
        def discover(self, url, document=None):
            return [FeedHint(BASE + "feed", "New adapter", "fixture")] if url == BASE else []

    monkeypatch.setattr("app.connectors.feed_adapters.ADAPTERS", list(ADAPTERS))
    register_feed_adapter(Extra())
    assert discover_sources(BASE, Network({BASE + "feed": RSS}))["candidates"][0]["verified"]


def test_redirects_use_existing_ssrf_boundary(monkeypatch):
    from app.connectors import http as network

    monkeypatch.setattr(network, "resolve_system_host", lambda parsed: ["127.0.0.1"])
    client = SafeHTTP()
    try:
        with pytest.raises(SourceError, match="拒绝访问"):
            discover_sources("http://127.0.0.1/feed", client)
    finally:
        client.close()


def setup_network(monkeypatch, routes=None):
    network = Network(routes or {BASE + "feed": RSS})
    monkeypatch.setattr(source_api, "SafeHTTP", lambda: network)
    return network


def detect(client, url=BASE + "feed", headers=None):
    response = client.post("/api/sources/discover", json={"url": url}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def selection(draft, **extra):
    return {
        "discovery_id": draft["discovery_id"],
        "selections": [{"candidate_id": r["id"]} for r in draft["candidates"] if r["verified"]],
        **extra,
    }


@pytest.mark.integration
def test_discover_then_save_ingests_but_does_not_favorite(monkeypatch, database, api_client):
    network = setup_network(monkeypatch)
    draft = detect(api_client)
    assert api_client.get("/api/sources").json() == []
    assert api_client.get("/api/items").json()["total"] == 0
    connected = api_client.post("/api/sources/connect", json=selection(draft))
    assert connected.status_code == 201, connected.text
    data = connected.json()
    source = data["items"][0]["source"]
    assert source["input_url"] == source["fetch_url"] == BASE + "feed"
    assert source["access_type"] == "rss" and source["validated_at"] and source["enabled"]
    assert data["items"][0]["fetched_count"] == 1 and data["auto_favorited"] is False
    assert network.calls == [BASE + "feed"] * 2
    assert api_client.get("/api/items?scope=library").json()["total"] == 0
    assert api_client.get("/api/items").json()["total"] == 1
    assert api_client.post("/api/sources/connect", json=selection(draft)).status_code == 409
    with database() as session:
        assert session.scalar(select(func.count()).select_from(ContentItem)) == 1
        assert session.get(Source, UUID(source["id"])).cursor["etag"] == "v1"


@pytest.mark.integration
def test_save_revalidation_failure_and_unverified_selection_never_mutate(monkeypatch, api_client):
    network = setup_network(monkeypatch)
    draft = detect(api_client)
    forged = selection(draft)
    forged["selections"][0]["candidate_id"] = str(uuid4())
    assert api_client.post("/api/sources/connect", json=forged).status_code == 422
    network.routes[BASE + "feed"] = SourceError("HTTP 403")
    assert api_client.post("/api/sources/connect", json=selection(draft)).status_code == 422
    assert api_client.get("/api/sources").json() == []
    assert api_client.get("/api/items").json()["total"] == 0
    failure = detect(api_client)
    assert failure["needs"] and failure["candidates"] == [] and "403" in failure["message"]


@pytest.mark.integration
def test_draft_isolation_expiry_and_atomic_multi_save(monkeypatch, database, api_client):
    html = b"""<html><link type="application/rss+xml" href="/feed"/>
    <link type="application/atom+xml" href="/atom"/></html>"""
    setup_network(
        monkeypatch, {BASE: (html, BASE, "text/html"), BASE + "feed": RSS, BASE + "atom": ATOM}
    )
    draft = detect(api_client, BASE)
    other = uuid4()
    with database.begin() as session:
        session.add(LocalIdentity(id=other, name="Other identity"))
    assert (
        api_client.post(
            "/api/sources/connect", headers={"X-TIRIS-User": str(other)}, json=selection(draft)
        ).status_code
        == 404
    )
    one = selection(draft)
    one["selections"] = one["selections"][:1]
    assert api_client.post("/api/sources/connect", json=one).status_code == 201
    reversed_body = selection(draft)
    reversed_body["selections"].reverse()  # New row would be inserted before the duplicate.
    assert api_client.post("/api/sources/connect", json=reversed_body).status_code == 409
    assert len(api_client.get("/api/sources").json()) == 1
    with database.begin() as session:
        session.get(SourceDiscovery, UUID(draft["discovery_id"])).expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    assert api_client.post("/api/sources/connect", json=one).status_code == 404
    fresh = detect(api_client, BASE)
    body = selection(fresh)
    body["selections"] = body["selections"][1:]
    assert api_client.post("/api/sources/connect", json=body).status_code == 201
    assert len(api_client.get("/api/sources").json()) == 2


@pytest.mark.integration
def test_edit_connection_versions_preserve_history_and_lifecycle(monkeypatch, database, api_client):
    network = setup_network(monkeypatch)
    first = api_client.post("/api/sources/connect", json=selection(detect(api_client))).json()[
        "items"
    ][0]["source"]
    id = first["id"]
    patch = api_client.patch(f"/api/sources/{id}", json={"name": "Renamed", "expected_version": 1})
    assert patch.status_code == 200 and patch.json()["name"] == "Renamed"
    # Two independent feeds can reuse the same entry id but mean different articles.
    network.routes[BASE + "second-feed"] = RSS.replace(b"First story", b"New article").replace(
        b"/story", b"/new"
    )
    draft = detect(api_client, BASE + "second-feed")
    edited = api_client.post(
        "/api/sources/connect", json=selection(draft, edit_source_id=id, expected_version=1)
    )
    assert edited.status_code == 201, edited.text
    changed = edited.json()["items"][0]["source"]
    assert changed["id"] == id and changed["connection_version"] == 2
    assert api_client.get("/api/items").json()["total"] == 2
    with database() as session:
        assert set(session.scalars(select(ContentOccurrence.external_id))) == {
            "one",
            "connection:2:one",
        }
    assert (
        api_client.post(
            "/api/sources/connect", json=selection(draft, edit_source_id=id, expected_version=1)
        ).status_code
        == 409
    )
    assert api_client.patch(f"/api/sources/{id}", json={"enabled": False}).status_code == 200
    assert api_client.post("/api/sources/sync", json={"source_id": id}).status_code == 409
    assert api_client.patch(f"/api/sources/{id}", json={"enabled": True}).status_code == 200
    assert api_client.delete(f"/api/sources/{id}").status_code == 200
    assert api_client.get("/api/sources").json() == []
    assert api_client.get("/api/items").json()["total"] == 2
    assert api_client.post("/api/sources/connect", json=selection(draft)).status_code == 201
    assert api_client.get("/api/sources").json()[0]["id"] == id


@pytest.mark.integration
def test_json_save_and_existing_worker_pipeline(monkeypatch, database, api_client):
    network = setup_network(monkeypatch, {BASE + "feed": JSON})
    source = api_client.post("/api/sources/connect", json=selection(detect(api_client))).json()[
        "items"
    ][0]["source"]
    assert source["access_type"] == "json_feed" and source["sync_supported"]
    assert len(fetch_source("rss", source["url"], {}, network).items) == 1
    monkeypatch.setattr(tasks.sync_source, "delay", lambda *args: None)
    submitted = api_client.post("/api/sources/sync", json={"source_id": source["id"]}).json()
    assert tasks.execute_sync(UUID(submitted["id"]), network) is False
    with database() as session:
        assert session.get(Job, UUID(submitted["id"])).status == "succeeded"
    assert api_client.get("/api/items").json()["total"] == 1
