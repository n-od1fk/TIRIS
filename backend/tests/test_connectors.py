import gzip
import socket

import httpx
import pytest

from app.connectors.adapters import fetch_source, parse_blackhat, parse_rss
from app.connectors.contracts import RetryableSourceError, SourceError
from app.connectors.http import PublicTransport, SafeHTTP, canonicalize_url, resolve_public_host

RSS = b"""<rss version="2.0"><channel><title>Test feed</title>
<item><guid>one</guid><title>Useful article</title><link>https://example.com/article?utm_source=x</link>
<description><![CDATA[<p>Evidence</p><script>bad()</script>]]></description></item>
<item><title>Incomplete</title></item></channel></rss>"""


def test_rss_scope_unknown_date_and_invalid_entry():
    result = parse_rss(RSS, "https://example.com/rss", 100)
    assert len(result.items) == 1 and result.rejected_count == 1
    item = result.items[0]
    assert item.published_at is None
    assert item.clean_text == "Evidence"
    assert item.acquisition_level == "abstract_only"
    assert item.url == "https://example.com/article"


@pytest.mark.parametrize("data", [b"<html>not a feed</html>", b"<rss version='2.0'><channel>"])
def test_parser_failure_is_not_silent_success(data):
    with pytest.raises(SourceError):
        parse_rss(data, "https://example.com/rss", 100)


def test_blackhat_only_index_and_same_link_once():
    data = b"""<a href="/us-26/briefings.html">Briefings</a>
    <a href="/us-26/briefings.html">Duplicate</a>
    <a href="https://evil.example/archives">Ignore</a>"""
    result = parse_blackhat(data, "https://www.blackhat.com/html/archives.html", 10)
    assert len(result.items) == 1
    assert result.items[0].acquisition_level == "metadata_only"
    assert result.items[0].content_type == "conference_index"
    with pytest.raises(SourceError):
        parse_blackhat(b"<html>changed layout</html>", "https://blackhat.com/", 10)


def test_url_normalization_preserves_meaningful_query():
    assert canonicalize_url("HTTPS://Example.COM:443/a?utm_x=1&version=2#part") == (
        "https://example.com/a?version=2"
    )


@pytest.mark.parametrize(
    "url", ["file:///tmp/a", "https://user:password@example.com/", "http://example.com:8000/"]
)
def test_invalid_url(url):
    with pytest.raises(SourceError):
        canonicalize_url(url)


@pytest.mark.parametrize(
    "addresses",
    [["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"], ["::1"], ["8.8.8.8", "192.168.1.1"]],
)
def test_dns_must_only_resolve_to_public_addresses(monkeypatch, addresses):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443)) for ip in addresses
        ],
    )
    with pytest.raises(SourceError):
        resolve_public_host("https://example.com/")


def test_transport_pins_validated_ip_and_keeps_tls_identity(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))],
    )
    transport = PublicTransport()

    def handler(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["Host"] == "example.com"
        assert request.headers["Connection"] == "close"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(200, content=b"ok")

    transport.transport.close()
    transport.transport = httpx.MockTransport(handler)
    http = SafeHTTP(transport)
    try:
        assert http.get("https://example.com/").content == b"ok"
    finally:
        http.close()


def test_redirect_to_private_ip_is_rechecked(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **kw: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("127.0.0.1" if host == "127.0.0.1" else "8.8.8.8", 443),
            )
        ],
    )
    transport = PublicTransport()
    transport.transport.close()
    transport.transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
    )
    http = SafeHTTP(transport)
    try:
        with pytest.raises(SourceError):
            http.get("https://example.com/")
    finally:
        http.close()


def test_redirect_strips_token_and_gzip_is_decoded_once():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "one.example":
            return httpx.Response(302, headers={"Location": "https://two.example/"})
        assert "Authorization" not in request.headers
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b"plain")
        )

    http = SafeHTTP(httpx.MockTransport(handler))
    try:
        assert (
            http.get("https://one.example/", {"authorization": "Bearer fake"}).content == b"plain"
        )
        assert len(requests) == 2
    finally:
        http.close()


def test_response_size_limit_and_retryable_status():
    http = SafeHTTP(httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 1001)))
    http.settings = http.settings.model_copy(update={"fetch_max_bytes": 1000})
    try:
        with pytest.raises(SourceError, match="大小"):
            http.get("https://example.com/")
    finally:
        http.close()
    http = SafeHTTP(httpx.MockTransport(lambda request: httpx.Response(429)))
    try:
        with pytest.raises(RetryableSourceError):
            http.get("https://example.com/")
    finally:
        http.close()


def test_github_repo_release_and_scope():
    def handler(request):
        assert request.headers.get("X-GitHub-Api-Version")
        if request.url.path.endswith("/releases"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 2,
                        "html_url": "https://github.com/a/b/releases/tag/v1",
                        "tag_name": "v1",
                        "body": "Changes",
                        "published_at": "2026-01-01T00:00:00Z",
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": 1,
                "html_url": "https://github.com/a/b",
                "full_name": "a/b",
                "owner": {"login": "a"},
                "description": "Repository metadata",
            },
        )

    http = SafeHTTP(httpx.MockTransport(handler))
    http.settings = http.settings.model_copy(update={"fetch_domain_interval_seconds": 0})
    try:
        result = fetch_source("github", "https://github.com/a/b", {}, http)
        assert [item.content_type for item in result.items] == ["repository", "release"]
        assert result.items[0].acquisition_level == "metadata_only"
        assert result.items[1].acquisition_level == "full_text"
    finally:
        http.close()


def test_conditional_not_modified_reuses_cursor():
    def handler(request):
        assert request.headers["If-None-Match"] == '"one"'
        return httpx.Response(304)

    http = SafeHTTP(httpx.MockTransport(handler))
    try:
        result = fetch_source("rss", "https://example.com/rss", {"etag": '"one"'}, http)
        assert result.not_modified and not result.items
        assert result.cursor == {"etag": '"one"'}
    finally:
        http.close()


def test_doh_cannot_allow_private_or_invalid_response(monkeypatch):
    import app.connectors.http as guarded

    for answers in [["127.0.0.1"], ["198.18.0.1"], ["8.8.8.8", "10.0.0.1"], ["not-an-ip"]]:
        monkeypatch.setattr(guarded, "resolve_doh", lambda host, result=answers: result)
        with pytest.raises(SourceError):
            resolve_public_host("https://example.com/", mode="doh")
    monkeypatch.setattr(guarded, "resolve_doh", lambda host: ["8.8.8.8"])
    assert resolve_public_host("https://example.com/", mode="doh") == ["8.8.8.8"]
    with pytest.raises(SourceError):
        resolve_public_host("http://127.0.0.1/", mode="doh")


def test_doh_actual_http_stream_and_tls_identity(monkeypatch):
    import app.connectors.http as guarded

    real_client = httpx.Client

    def handler(request):
        assert request.url.host == "1.1.1.1"
        assert request.headers["Host"] == "cloudflare-dns.com"
        assert request.extensions["sni_hostname"] == "cloudflare-dns.com"
        assert request.headers["Accept"] == "application/dns-json"
        return httpx.Response(
            200,
            json={
                "Status": 0,
                "Answer": [{"type": 5, "data": "alias.example"}, {"type": 1, "data": "8.8.8.8"}],
            },
        )

    monkeypatch.setattr(
        guarded.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert guarded.resolve_doh("example.com") == ["8.8.8.8"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"Status": 3}),
        httpx.Response(200, content=b"x" * 20000),
    ],
)
def test_doh_failure_does_not_fall_back(monkeypatch, response):
    import app.connectors.http as guarded

    real_client = httpx.Client
    monkeypatch.setattr(
        guarded.httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: response), **kwargs
        ),
    )
    with pytest.raises(SourceError):
        guarded.resolve_doh("example.com")
