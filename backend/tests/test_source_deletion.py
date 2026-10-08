from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.connectors.contracts import FetchResult, RawItem, RetryableSourceError
from app.discovery.service import candidate_rows
from app.models import ContentItem, IngestionRun, Job, OriginalMaterial, Source
from app.platforms import registry
from app.platforms.contracts import Document, Material
from app.workers import tasks

pytestmark = pytest.mark.integration


def add_source(client, **kwargs):
    return client.post(
        "/api/sources",
        json={"name": "Test RSS", "kind": "rss", "url": "https://example.com/feed"},
        **kwargs,
    ).json()["id"]


class FixturePlatform:
    kind, label, operations = "rss", "Test", ("list",)

    def detail(self, source_url, request, http):
        return Document(
            RawItem(request.url, request.url, request.url, "article", clean_text="Full test body"),
            "full",
            materials=[
                Material("original.html", "text/html", request.url, b"<article>Original</article>")
            ],
        )


def test_delete_preserves_library_files_isolates_identity_and_readd_restores(
    api_client, database, monkeypatch
):
    monkeypatch.setitem(registry.PLATFORMS, "rss", FixturePlatform())
    monkeypatch.setattr(tasks.sync_source, "delay", lambda *args: None)
    source_id = add_source(api_client)
    capture = api_client.post(
        f"/api/platforms/{source_id}/detail", json={"url": "https://example.com/saved"}
    ).json()
    api_client.put(f"/api/items/{capture['item_id']}/favorite", json={"enabled": True})
    unsaved = api_client.post(
        f"/api/platforms/{source_id}/detail", json={"url": "https://example.com/candidate"}
    ).json()
    user_id = get_settings().user_id
    assert len(candidate_rows(user_id)) == 1
    other = api_client.post("/api/identities", json={"name": "Other"}).json()["id"]
    headers = {"X-TIRIS-User": other}
    other_source = add_source(api_client, headers=headers)
    assert api_client.delete(f"/api/sources/{source_id}", headers=headers).status_code == 404
    queued = api_client.post("/api/sources/sync", json={"source_id": source_id}).json()
    deleted = api_client.delete(f"/api/sources/{source_id}")
    assert deleted.status_code == 200 and deleted.json()["jobs_stopped"] == 1
    assert api_client.get("/api/sources").json() == []
    assert api_client.get("/api/sources", headers=headers).json()[0]["id"] == other_source
    assert api_client.delete(f"/api/sources/{source_id}").json()["jobs_stopped"] == 0
    assert (
        api_client.get(capture["materials"][0]["download_url"]).content
        == b"<article>Original</article>"
    )
    assert api_client.get("/api/items?scope=library").json()["total"] == 1
    assert api_client.get(f"/api/items/{unsaved['item_id']}").status_code == 200
    assert candidate_rows(user_id) == []
    assert api_client.post("/api/sources/sync", json={"source_id": source_id}).status_code == 404
    assert api_client.patch(f"/api/sources/{source_id}", json={"enabled": True}).status_code == 404
    assert (
        api_client.post(
            f"/api/platforms/{source_id}/detail", json={"url": "https://example.com/new"}
        ).status_code
        == 404
    )
    with database() as session:
        source = session.get(Source, UUID(source_id))
        assert source.archived and not source.enabled
        assert session.get(Job, UUID(queued["id"])).status == "failed"
        assert session.scalar(select(func.count()).select_from(OriginalMaterial)) == 2
    restored = api_client.post(
        "/api/sources", json={"kind": "rss", "name": "Re-added", "url": "https://example.com/feed"}
    )
    assert restored.status_code == 201 and restored.json()["id"] == source_id
    assert restored.json()["enabled"] is True
    assert len(candidate_rows(user_id)) == 1
    assert api_client.get(capture["materials"][0]["download_url"]).status_code == 200


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("network_error", [False, True])
def test_delete_during_sync_discards_result_and_never_retries(
    api_client, database, monkeypatch, restore, network_error
):
    monkeypatch.setattr(tasks.sync_source, "delay", lambda *args: None)
    source_id = add_source(api_client)
    job_id = UUID(api_client.post("/api/sources/sync", json={"source_id": source_id}).json()["id"])

    def fetch(*args):
        assert api_client.delete(f"/api/sources/{source_id}").status_code == 200
        if restore:
            assert add_source(api_client) == source_id
        if network_error:
            raise RetryableSourceError("Temporary network failure")
        return FetchResult([RawItem("x", "https://example.com/new", "Late result", "article")])

    monkeypatch.setattr(tasks, "fetch_source", fetch)
    assert tasks.execute_sync(job_id) is False
    with database() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(IngestionRun).where(IngestionRun.job_id == job_id))
        assert job.status == run.status == "failed" and job.attempts == 1
        assert session.scalar(select(func.count()).select_from(ContentItem)) == 0


def test_delete_during_detail_does_not_save_late_files(api_client, database, monkeypatch):
    source_id = add_source(api_client)

    class DeletingPlatform(FixturePlatform):
        def detail(self, source_url, request, http):
            assert api_client.delete(f"/api/sources/{source_id}").status_code == 200
            return super().detail(source_url, request, http)

    monkeypatch.setitem(registry.PLATFORMS, "rss", DeletingPlatform())
    result = api_client.post(
        f"/api/platforms/{source_id}/detail", json={"url": "https://example.com/new"}
    )
    assert result.status_code == 409
    with database() as session:
        assert session.scalar(select(func.count()).select_from(OriginalMaterial)) == 0
