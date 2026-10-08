import re
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.config import get_settings
from app.connectors.adapters import fetch_source
from app.connectors.contracts import FetchResult, RawItem, SourceError
from app.connectors.registry import CONNECTORS, SourceConnector, get_connector
from app.models import ContentItem, FixedColumn, Job, LocalIdentity, Source
from app.workers import tasks


@pytest.mark.parametrize(
    "kind,url",
    [
        ("unknown", "https://example.com/feed"),
        ("github", "https://example.com/a/b"),
        ("github", "https://github.com/a"),
        ("blackhat", "https://example.com/archives.html"),
        ("rss", "file:///local.xml"),
    ],
)
def test_unified_dispatch_rejects_invalid_inputs_before_network(kind, url):
    class NoNetwork:
        def get(self, *args):
            raise AssertionError("Validation must run before network I/O")

    with pytest.raises(SourceError):
        fetch_source(kind, url, {}, NoNetwork())


@pytest.mark.integration
def test_api_validation_and_type_catalog_share_registry(api_client):
    catalog = api_client.get("/api/source-types").json()
    assert {row["kind"] for row in catalog} == {"rss", "github", "blackhat", "arxiv", "web"}
    for kind, url in [
        ("unknown", "https://example.com/rss"),
        ("github_search", "https://api.github.com/search/repositories?q=security"),
        ("github", "https://example.com/a/b"),
        ("blackhat", "https://example.com/archives.html"),
        ("rss", "file:///local.xml"),
    ]:
        assert (
            api_client.post(
                "/api/sources", json={"name": "Source", "kind": kind, "url": url}
            ).status_code
            == 422
        )
    assert (
        api_client.post(
            "/api/sources", json={"name": " ", "kind": "rss", "url": "https://example.com/rss"}
        ).status_code
        == 422
    )
    assert api_client.get("/api/sources").json() == []


@pytest.mark.integration
def test_new_connector_uses_same_add_sync_and_ingestion_pipeline(api_client, database, monkeypatch):
    calls = []

    def reader(url, cursor, http):
        calls.append((url, cursor))
        return FetchResult(
            [
                RawItem(
                    external_id="one",
                    url="https://example.com/article",
                    title="新平台内容",
                    content_type="article",
                )
            ]
        )

    connector = SourceConnector(
        "fixture", "测试平台", "测试说明", "https://example.com/feed", reader
    )
    monkeypatch.setitem(CONNECTORS, connector.kind, connector)
    monkeypatch.setattr(tasks.sync_source, "delay", lambda *args: None)
    assert api_client.get("/api/source-types").json()[-1] == connector.describe()
    assert get_connector("fixture") is connector
    created = api_client.post(
        "/api/sources",
        json={"name": "  新平台  ", "kind": "fixture", "url": "https://example.com/feed"},
    )
    assert created.status_code == 201 and created.json()["name"] == "新平台"
    submitted = api_client.post("/api/sources/sync", json={"source_id": created.json()["id"]})
    assert submitted.status_code == 202
    tasks.execute_sync(UUID(submitted.json()["id"]))
    assert calls == [("https://example.com/feed", {})]
    assert api_client.get(f"/api/jobs/{submitted.json()['id']}").json()["status"] == "succeeded"
    assert api_client.get("/api/items").json()["total"] == 1
    assert api_client.get("/api/items?scope=library").json()["total"] == 0
    with database() as session:
        assert session.scalar(select(ContentItem)).user_id == get_settings().user_id


@pytest.mark.integration
def test_archive_only_unused_automatic_templates_and_explicit_add_restores(
    api_client, database, monkeypatch
):
    migration = import_module("migrations.versions.b0198ed41008_empty_identity_sources")
    now = datetime.now(UTC)
    default_id, other, column_space = get_settings().user_id, uuid4(), uuid4()
    with database.begin() as session:
        session.add_all(
            [
                LocalIdentity(id=default_id, name="安全", created_at=now),
                LocalIdentity(id=other, name="视频", created_at=now),
                LocalIdentity(id=column_space, name="栏目", created_at=now),
            ]
        )
        archived = Source(
            user_id=other,
            name=migration.TEMPLATES[0][0],
            kind="rss",
            url=migration.TEMPLATES[0][2],
            created_at=now,
        )
        used = Source(
            user_id=other,
            name=migration.TEMPLATES[1][0],
            kind="github",
            url=migration.TEMPLATES[1][2],
            created_at=now,
        )
        manual = Source(
            user_id=other,
            name=migration.TEMPLATES[2][0],
            kind="blackhat",
            url=migration.TEMPLATES[2][2],
            created_at=now + timedelta(seconds=1),
        )
        default = Source(
            user_id=default_id,
            name=migration.TEMPLATES[0][0],
            kind="rss",
            url=migration.TEMPLATES[0][2],
            created_at=now,
        )
        referenced = Source(
            user_id=column_space,
            name=migration.TEMPLATES[0][0],
            kind="rss",
            url=migration.TEMPLATES[0][2],
            created_at=now,
        )
        session.add_all([archived, used, manual, default, referenced])
        session.flush()
        session.add(Job(user_id=other, source_id=used.id, status="queued"))
        session.add(
            FixedColumn(
                user_id=column_space,
                name="栏目",
                name_key="栏目",
                source_ids=[str(referenced.id)],
                keywords=[],
                updated_at=now,
            )
        )
        archived_id = archived.id
    bind = database.kw["bind"]
    schema = bind.get_execution_options()["schema_translate_map"][None]
    assert re.fullmatch(r"tiris_test_[0-9a-f]{32}", schema)
    with bind.begin() as connection:
        # Raw migration SQL must explicitly target this fixture's isolated schema.
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        migration.archive_unused_templates(connection, default_id)
        migration.archive_unused_templates(connection, default_id)
    with database() as session:
        assert session.get(Source, archived_id).archived is True
        assert {
            row.id for row in session.scalars(select(Source).where(Source.archived.is_(False)))
        } == {used.id, manual.id, default.id, referenced.id}
        assert (
            session.get(Source, archived_id).enabled is True
        )  # No destructive configuration rewrite.
    headers = {"X-TIRIS-User": str(other)}
    assert archived_id not in {
        UUID(row["id"]) for row in api_client.get("/api/sources", headers=headers).json()
    }
    assert (
        api_client.post(
            "/api/sources/sync", headers=headers, json={"source_id": str(archived_id)}
        ).status_code
        == 404
    )
    assert (
        api_client.patch(
            f"/api/sources/{archived_id}", headers=headers, json={"enabled": True}
        ).status_code
        == 404
    )

    def unexpected_fetch(*args):
        raise AssertionError("Archived source must not reach a network reader")

    monkeypatch.setattr(tasks, "fetch_source", unexpected_fetch)
    with database.begin() as session:
        stale_job = Job(user_id=other, source_id=archived_id, status="queued")
        session.add(stale_job)
        session.flush()
        stale_id = stale_job.id
    assert tasks.execute_sync(stale_id) is False
    with database() as session:
        assert session.get(Job, stale_id).status == "failed"
        assert session.get(Job, stale_id).attempts == 0
    restored = api_client.post(
        "/api/sources",
        headers=headers,
        json={"name": "我主动添加的博客", "kind": "rss", "url": migration.TEMPLATES[0][2]},
    )
    assert restored.status_code == 201 and restored.json()["id"] == str(archived_id)
    assert len(api_client.get("/api/sources", headers=headers).json()) == 3
    assert len(api_client.get("/api/sources").json()) == 1
