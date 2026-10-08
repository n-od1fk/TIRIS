from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.config import get_settings
from app.connectors.contracts import FetchResult, RawItem, RetryableSourceError, SourceError
from app.main import app
from app.models import ContentItem, ContentOccurrence, ContentVersion, IngestionRun, Job, Source
from app.workers import tasks

pytestmark = pytest.mark.integration


def source(database, url="https://example.com/rss"):
    with database.begin() as session:
        result = Source(user_id=get_settings().user_id, name="Fixture source", kind="rss", url=url)
        session.add(result)
        session.flush()
        return result.id


def job(database, source_id):
    with database.begin() as session:
        result = Job(user_id=get_settings().user_id, source_id=source_id, status="queued")
        session.add(result)
        session.flush()
        return result.id


def ingest(monkeypatch, database, source_id, raw):
    monkeypatch.setattr(
        tasks, "fetch_source", lambda *args: FetchResult([raw], cursor={"etag": "v1"})
    )
    job_id = job(database, source_id)
    assert tasks.execute_sync(job_id) is False
    with database() as session:
        assert session.get(Job, job_id).status == "succeeded"
        run = session.scalar(select(IngestionRun).where(IngestionRun.job_id == job_id))
        return run


def raw_item():
    return RawItem(
        external_id="stable-id",
        url="https://example.com/article?utm_source=test",
        title="Evidence-based retrieval",
        content_type="article",
        clean_text="First abstract",
        acquisition_level="abstract_only",
    )


def test_repeat_and_update_preserve_old_version(monkeypatch, database, api_client):
    source_id, raw = source(database), raw_item()
    assert ingest(monkeypatch, database, source_id, raw).new_count == 1
    assert ingest(monkeypatch, database, source_id, raw).unchanged_count == 1
    assert (
        ingest(
            monkeypatch, database, source_id, replace(raw, clean_text="Updated evidence")
        ).updated_count
        == 1
    )
    with database() as session:
        assert session.scalar(select(func.count()).select_from(ContentItem)) == 1
        versions = session.scalars(
            select(ContentVersion).order_by(ContentVersion.version_number)
        ).all()
        assert [version.clean_text for version in versions] == [
            "First abstract",
            "Updated evidence",
        ]
        assert versions[0].published_at is None
        item_id = versions[0].item_id
    listed = api_client.get("/api/items?q=RETRIEVAL").json()
    assert listed["total"] == 1 and listed["items"][0]["version"] == 2
    assert listed["items"][0]["sources"] == ["Fixture source"]
    detail = api_client.get(f"/api/items/{item_id}").json()
    assert detail["versions"][1]["text"] == "First abstract"
    assert detail["sources"][0]["original_url"] == raw.url


def test_cross_source_normalized_url_and_source_specific_idempotency(monkeypatch, database):
    first, second = source(database), source(database, "https://other.example/rss")
    raw = raw_item()
    ingest(monkeypatch, database, first, raw)
    other = replace(
        raw,
        external_id="other-id",
        url="https://example.com/article#part",
        clean_text="Different source excerpt",
    )
    ingest(monkeypatch, database, second, other)
    assert ingest(monkeypatch, database, first, raw).unchanged_count == 1
    assert ingest(monkeypatch, database, second, other).unchanged_count == 1
    with database() as session:
        assert session.scalar(select(func.count()).select_from(ContentItem)) == 1
        assert session.scalar(select(func.count()).select_from(ContentOccurrence)) == 2
        assert session.scalar(select(func.count()).select_from(ContentVersion)) == 2


def test_exact_copy_merge_but_paper_code_and_old_snapshot_not_merged(monkeypatch, database):
    source_id = source(database)
    full = replace(raw_item(), clean_text="Verified full text " * 30, acquisition_level="full_text")
    ingest(monkeypatch, database, source_id, full)
    copy = replace(full, external_id="copy", url="https://copy.example/article")
    ingest(monkeypatch, database, source_id, copy)
    code = replace(
        full, external_id="code", url="https://github.com/a/b", content_type="repository"
    )
    ingest(monkeypatch, database, source_id, code)
    ingest(monkeypatch, database, source_id, replace(full, clean_text="Changed full text " * 30))
    stale = replace(full, external_id="old-copy", url="https://stale.example/article")
    ingest(monkeypatch, database, source_id, stale)
    with database() as session:
        assert session.scalar(select(func.count()).select_from(ContentItem)) == 3


def test_shorter_updated_excerpt_does_not_delete_old_fulltext(monkeypatch, database):
    source_id = source(database)
    raw = replace(raw_item(), clean_text="Full body " * 30, acquisition_level="full_text")
    ingest(monkeypatch, database, source_id, raw)
    ingest(
        monkeypatch,
        database,
        source_id,
        replace(raw, clean_text="New excerpt", acquisition_level="abstract_only"),
    )
    with database() as session:
        versions = session.scalars(
            select(ContentVersion).order_by(ContentVersion.version_number)
        ).all()
        assert [v.acquisition_level for v in versions] == ["full_text", "abstract_only"]
        assert versions[0].clean_text == raw.clean_text


def test_retry_stops_after_three_retries_and_errors_are_visible(monkeypatch, database, api_client):
    source_id = source(database)

    def fail(*args):
        raise RetryableSourceError("Source temporarily unavailable")

    monkeypatch.setattr(tasks, "fetch_source", fail)
    job_id = job(database, source_id)
    assert [tasks.execute_sync(job_id) for _ in range(4)] == [True, True, True, False]
    assert tasks.execute_sync(job_id) is False
    result = api_client.get(f"/api/jobs/{job_id}").json()
    assert result["status"] == "failed" and result["attempts"] == 4
    assert result["finished_at"] and "temporarily" in result["error"]
    assert len(api_client.get(f"/api/sources/{source_id}/runs").json()) == 4


def test_permanent_failure_and_unknown_exception_do_not_leak_secrets(monkeypatch, database):
    source_id = source(database)
    for exception, expected in [
        (SourceError("Invalid feed"), "Invalid feed"),
        (RuntimeError("secret-token-value"), "处理失败"),
    ]:

        def fail(*args, error=exception):
            raise error

        monkeypatch.setattr(tasks, "fetch_source", fail)
        job_id = job(database, source_id)
        assert tasks.execute_sync(job_id) is False
        with database() as session:
            assert expected in session.get(Job, job_id).error
            assert "secret-token" not in session.get(Job, job_id).error


def test_completed_job_delivery_is_idempotent(monkeypatch, database):
    source_id = source(database)
    monkeypatch.setattr(tasks, "fetch_source", lambda *args: FetchResult([raw_item()]))
    job_id = job(database, source_id)
    tasks.execute_sync(job_id)
    tasks.execute_sync(job_id)
    with database() as session:
        assert session.get(Job, job_id).attempts == 1
        assert session.scalar(select(func.count()).select_from(IngestionRun)) == 1


def test_source_api_duplicate_disable_and_active_job(monkeypatch, database, api_client):
    payload = {"name": "RSS", "kind": "rss", "url": "https://example.com/rss"}
    response = api_client.post("/api/sources", json=payload)
    assert response.status_code == 201
    source_id = response.json()["id"]
    assert api_client.post("/api/sources", json=payload).status_code == 409
    submitted = []
    monkeypatch.setattr(tasks.sync_source, "delay", lambda value: submitted.append(value))
    one = api_client.post("/api/sources/sync", json={"source_id": source_id})
    two = api_client.post("/api/sources/sync", json={"source_id": source_id})
    assert one.status_code == 202 and one.json()["id"] == two.json()["id"]
    assert len(submitted) == 1
    assert api_client.patch(f"/api/sources/{source_id}", json={"enabled": False}).status_code == 200
    assert api_client.post("/api/sources/sync", json={"source_id": source_id}).status_code == 409


def test_queue_failure_persists_failed_job(monkeypatch, database, api_client):
    source_id = source(database)

    def unavailable(*args):
        raise RuntimeError("secret broker credentials")

    monkeypatch.setattr(tasks.sync_source, "delay", unavailable)
    response = api_client.post("/api/sources/sync", json={"source_id": str(source_id)})
    assert response.status_code == 503
    result = api_client.get(f"/api/jobs/{response.json()['detail']['job_id']}").json()
    assert result["status"] == "failed" and result["finished_at"]
    assert "secret" not in result["error"]


def test_user_scope_on_sources_items_jobs_and_runs(monkeypatch, database, api_client):
    source_id = source(database)
    run = ingest(monkeypatch, database, source_id, raw_item())
    with database() as session:
        item_id = session.scalar(select(ContentItem.id))
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"user_id": uuid4()}
    )
    assert api_client.get("/api/sources").json() == []
    assert api_client.get("/api/items").json()["total"] == 0
    for url in [
        f"/api/items/{item_id}",
        f"/api/jobs/{run.job_id}",
        f"/api/sources/{source_id}/runs",
    ]:
        assert api_client.get(url).status_code == 404


def test_pgvector_distance_is_reproducible(database):
    with database() as session:
        distance = session.scalar(
            text("SELECT '[1,2,3]'::public.vector <-> '[1,2,4]'::public.vector")
        )
        assert distance == 1.0


def test_migration_and_schema_have_all_tables(database):
    from sqlalchemy import inspect

    with database() as session:
        schema = session.bind.get_execution_options()["schema_translate_map"][None]
        tables = inspect(session.bind).get_table_names(schema=schema)
        assert set(tables) == {
            "alembic_version",
            "sources",
            "source_discoveries",
            "jobs",
            "ingestion_runs",
            "content_items",
            "content_occurrences",
            "content_versions",
            "original_materials",
            "long_term_profiles",
            "profile_revisions",
            "fixed_columns",
            "embedding_spaces",
            "embedding_chunks",
            "embedding_jobs",
            "favorites",
            "update_notices",
            "local_identities",
            "discovery_settings",
            "discovery_runs",
            "recommendations",
        }
