import io
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select

from app.config import get_settings
from app.connectors.contracts import RawItem, SourceError
from app.connectors.http import SafeHTTP
from app.models import ContentItem, ContentVersion, OriginalMaterial
from app.platforms import github, providers, registry
from app.platforms.contracts import DetailRequest, Document, Material, Page, PlatformQuery
from app.platforms.web import extract_html, get_page, pdf_text


def response(url, data, headers=None):
    return httpx.Response(
        200, content=data, headers=headers or {}, request=httpx.Request("GET", url)
    )


class HTTP:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers or {}, kwargs))
        return self.handler(url)


def repository():
    return {
        "id": 123,
        "html_url": "https://github.com/example/project",
        "full_name": "example/project",
        "owner": {"login": "example"},
        "description": "Metadata",
        "topics": ["security"],
    }


def test_github_platform_search_and_readme_no_preset_repository():
    row = repository()

    def handler(url):
        if "/search/" in url:
            return httpx.Response(200, json={"items": [row]}, request=httpx.Request("GET", url))
        if url.endswith("/readme"):
            return response(url, b"# README\nComplete original README with code and links.")
        return httpx.Response(200, json=row, request=httpx.Request("GET", url))

    http = HTTP(handler)
    provider = github.GitHubPlatform()
    page = provider.query(
        "https://github.com/", PlatformQuery(operation="search", query="security"), http
    )
    assert page.items[0].url == row["html_url"]
    doc = provider.detail("https://github.com/", DetailRequest(url=row["html_url"]), http)
    assert doc.status == "full" and "Complete original" in doc.item.clean_text
    assert [m.filename for m in doc.materials] == ["repository.json", "README.md"]
    assert all(url.startswith("https://api.github.com/") for url, _, _ in http.calls)


def test_public_repository_id_cursor_does_not_skip_rows():
    rows = [{**repository(), "id": n} for n in range(10001, 10101)]
    http = HTTP(lambda url: httpx.Response(200, json=rows, request=httpx.Request("GET", url)))
    result = github.GitHubPlatform().query(
        "https://github.com/", PlatformQuery(limit=5, cursor="10000"), http
    )
    assert len(result.items) == 5 and result.next_cursor == "10005"
    assert "since=10000" in http.calls[0][0]


def test_star_requires_credential_without_network(monkeypatch):
    monkeypatch.setattr(
        github, "get_settings", lambda: get_settings().model_copy(update={"github_token": ""})
    )
    http = HTTP(lambda url: pytest.fail("No network without required credentials"))
    with pytest.raises(SourceError, match="GITHUB_TOKEN"):
        github.GitHubPlatform().query("https://github.com/", PlatformQuery(operation="stars"), http)


def test_missing_readme_not_false_full():
    def handler(url):
        if url.endswith("readme"):
            raise SourceError("来源拒绝访问或不存在（HTTP 404）")
        return httpx.Response(200, json=repository(), request=httpx.Request("GET", url))

    doc = github.GitHubPlatform().detail(
        "https://github.com/",
        DetailRequest(url="https://github.com/example/project"),
        HTTP(handler),
    )
    assert doc.status == "partial" and doc.materials[0].filename == "repository.json"


def test_html_original_and_body_are_separate():
    body = "This is the complete article content explaining tools and state management. " * 12
    html = (
        f"<html><head><title>Article</title></head><body><article><h1>Article</h1><p>{body}</p>"
        '<pre>print(1)</pre><a href="/paper.pdf">Paper</a></article></body></html>'
    ).encode()
    doc = extract_html("https://example.com/article", response("https://example.com/article", html))
    doc.prepare()
    assert doc.status == "full" and body.strip() in doc.item.clean_text
    assert doc.materials[0].data == html
    assert doc.attachments[0]["url"] == "https://example.com/paper.pdf"
    assert doc.item.metadata["summary_status"] == "not_generated"


def test_crawler_respects_robots_before_article():
    http = HTTP(lambda url: response(url, b"User-agent: *\nDisallow: /private\n"))
    with pytest.raises(SourceError, match="robots"):
        get_page("https://example.com/private", http)
    assert len(http.calls) == 1 and http.calls[0][0].endswith("robots.txt")


def paper_pdf(blank=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 10 700 Td (This paper describes a complete text extraction test "
        b"with explicit evidence and original materials. "
        b"A second sentence makes the test body long enough.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    if blank:
        writer.add_blank_page(width=600, height=800)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("blank,complete", [(False, True), (True, False)])
def test_pdf_all_pages_not_silent_truncation(blank, complete):
    body, sections, error = pdf_text(paper_pdf(blank))
    assert "explicit evidence" in body
    assert (not error) == complete
    assert len(sections) == (2 if blank else 1)


def test_arxiv_metadata_pdf_and_stable_version_identity():
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>arXiv</title><entry>
    <id>http://arxiv.org/abs/2401.12345v2</id><title>A paper</title><summary>Abstract only</summary>
    <author><name>Author</name></author><published>2024-01-01T00:00:00Z</published>
    <updated>2024-02-01T00:00:00Z</updated></entry></feed>"""
    http = HTTP(lambda url: response(url, paper_pdf() if "/pdf/" in url else atom))
    doc = providers.ArxivPlatform().detail(
        "https://arxiv.org/", DetailRequest(url="2401.12345"), http
    )
    assert doc.item.external_id == "2401.12345" and doc.item.metadata["version"].endswith("v2")
    assert doc.status == "full" and "explicit evidence" in doc.item.clean_text
    assert doc.item.metadata["source_abstract"] == "Abstract only"
    assert doc.materials[1].filename == "paper.pdf"


def test_blackhat_description_not_presentation_full_text():
    html = (
        b'<html><article id="talk"><h1>Session</h1><p>Session abstract describing tools '
        b'and methods.</p><a href="/materials/slides.pdf">Slides</a></article></html>'
    )
    http = HTTP(
        lambda url: response(
            url,
            b"User-agent: *\nAllow: /" if url.endswith("robots.txt") else html,
            {"content-type": "text/html"},
        )
    )
    doc = providers.BlackHatPlatform().detail(
        "https://blackhat.com/",
        DetailRequest(url="https://blackhat.com/us-26/briefings.html#talk"),
        http,
    )
    assert doc.status == "partial" and "演讲页面" in doc.reason
    assert doc.item.url.endswith("#talk") and doc.materials[0].data == html


class FixturePlatform:
    kind, label, operations = "github", "Fixture", ("list",)
    body = "First complete body"

    def query(self, source_url, request, http):
        return Page([RawItem("x", "https://github.com/example/project", "A", "repository")])

    def detail(self, source_url, request, http):
        return Document(
            RawItem(
                "x", "https://github.com/example/project", "A", "repository", clean_text=self.body
            ),
            "full",
            materials=[
                Material(
                    "README.md",
                    "text/markdown",
                    "https://github.com/example/project",
                    self.body.encode(),
                )
            ],
        )


@pytest.mark.integration
def test_uniform_api_original_version_update_identity_and_no_auto_favorite(
    api_client, database, monkeypatch
):
    provider = FixturePlatform()
    monkeypatch.setitem(registry.PLATFORMS, "github", provider)
    connection = api_client.post(
        "/api/sources", json={"kind": "github", "name": "Platform", "url": "https://github.com/"}
    )
    assert connection.status_code == 201
    source_id = connection.json()["id"]
    assert api_client.get(f"/api/platforms/{source_id}/capabilities").json()["operations"] == [
        "list"
    ]
    page = api_client.post(f"/api/platforms/{source_id}/query", json={})
    assert page.status_code == 200 and page.json()["persisted"] is False
    assert api_client.get("/api/items").json()["total"] == 0
    body = {"url": "https://github.com/example/project"}
    first = api_client.post(f"/api/platforms/{source_id}/detail", json=body)
    assert first.status_code == 200, first.text
    capture = first.json()
    assert capture["status"] == "full" and capture["summary"] is None
    download = api_client.get(capture["materials"][0]["download_url"])
    assert download.content == provider.body.encode()
    assert download.headers["x-content-type-options"] == "nosniff"
    assert api_client.get("/api/items?scope=library").json()["total"] == 0
    # Alternate references (e.g. an arXiv ID versus its URL) must resolve by stable ID.
    same = api_client.post(
        f"/api/platforms/{source_id}/check-update", json={"url": "https://github.com/alias/project"}
    ).json()
    assert same["changed"] is False and same["first_capture"] is False
    provider.body = "Second complete body"
    changed = api_client.post(f"/api/platforms/{source_id}/check-update", json=body).json()
    assert changed["changed"] is True
    assert api_client.get(capture["materials"][0]["download_url"]).content == b"First complete body"
    with database() as session:
        assert session.scalar(select(func.count()).select_from(ContentVersion)) == 2
        assert session.scalar(select(func.count()).select_from(OriginalMaterial)) == 2
    other = api_client.post("/api/identities", json={"name": "Other"}).json()["id"]
    headers = {"X-TIRIS-User": other}
    assert (
        api_client.get(capture["materials"][0]["download_url"], headers=headers).status_code == 404
    )
    assert (
        api_client.post(f"/api/platforms/{source_id}/query", json={}, headers=headers).status_code
        == 404
    )
    with database.begin() as session:
        item = session.get(ContentItem, UUID(capture["item_id"]))
        item.candidate_expires_at = datetime.now(UTC) - timedelta(days=1)
    assert api_client.get(capture["materials"][0]["download_url"]).status_code == 404
    reread = api_client.post(f"/api/platforms/{source_id}/detail", json=body)
    assert reread.status_code == 200
    assert api_client.get(capture["materials"][0]["download_url"]).status_code == 200
    assert api_client.get("/api/items?scope=library").json()["total"] == 0


@pytest.mark.integration
def test_new_platform_registers_without_changing_dispatch(api_client, database, monkeypatch):
    from app.connectors import registry as source_registry

    provider = FixturePlatform()
    provider.kind = "fixture"
    monkeypatch.setattr(registry, "PLATFORMS", dict(registry.PLATFORMS))
    monkeypatch.setattr(source_registry, "CONNECTORS", dict(source_registry.CONNECTORS))
    registry.register_platform(provider)
    created = api_client.post(
        "/api/sources", json={"kind": "fixture", "name": "New", "url": "https://example.com/"}
    ).json()
    result = api_client.post(f"/api/platforms/{created['id']}/query", json={})
    assert result.status_code == 200 and result.json()["platform"] == "fixture"


def test_original_size_budget_keeps_existing_http_security(monkeypatch):
    http = SafeHTTP(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 2000))
    )
    with pytest.raises(SourceError, match="超过"):
        http.get("https://example.com/", max_bytes=1000)
    http.close()
