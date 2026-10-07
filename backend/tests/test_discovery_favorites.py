from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.connectors.adapters import fetch_source
from app.connectors.contracts import FetchResult, RawItem
from app.connectors.http import SafeHTTP
from app.discovery import service
from app.main import app
from app.models import (
    ContentItem,
    ContentVersion,
    DiscoveryRun,
    DiscoverySettings,
    Favorite,
    Recommendation,
    Source,
    UpdateNotice,
)
from app.workers import tasks

pytestmark = pytest.mark.integration


@pytest.fixture
def material(database, monkeypatch):
    with database.begin() as session:
        source = Source(
            user_id=get_settings().user_id,
            name="Fixture feed",
            kind="rss",
            url="https://example.com/feed",
        )
        session.add(source)
        session.flush()
        source_id = source.id

    def ingest(raw=None):
        raw = raw or RawItem(
            external_id="one",
            url="https://example.com/one",
            title="Vector database",
            clean_text="vector search tutorial",
            content_type="article",
            acquisition_level="abstract_only",
            metadata={"evidence_scope": "RSS excerpt only"},
        )
        monkeypatch.setattr(tasks, "fetch_source", lambda *args: FetchResult([raw]))
        with database.begin() as session:
            from app.models import Job

            job = Job(user_id=get_settings().user_id, source_id=source_id)
            session.add(job)
            session.flush()
            job_id = job.id
        assert tasks.execute_sync(job_id) is False
        with database() as session:
            item = session.scalar(select(ContentItem).where(ContentItem.canonical_url == raw.url))
            return item.id, raw

    return ingest


def star(client, item_id, enabled=True):
    response = client.put(f"/api/items/{item_id}/favorite", json={"enabled": enabled})
    assert response.status_code == 200, response.text
    return response.json()


def settings(client, **data):
    response = client.patch("/api/discovery/settings", json={"expected_version": 0, **data})
    assert response.status_code == 200, response.text


def test_library_requires_explicit_star_and_unstar_preserves_material(
    database, api_client, material
):
    item_id, _ = material()
    assert api_client.get("/api/items?scope=library").json()["total"] == 0
    assert api_client.get("/api/items").json()["total"] == 1
    star(api_client, item_id)
    star(api_client, item_id)
    assert api_client.get("/api/items?scope=library").json()["total"] == 1
    assert api_client.get("/api/items").json()["total"] == 0
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Favorite)) == 1
    star(api_client, item_id, False)
    assert api_client.get("/api/items?scope=library").json()["total"] == 0
    assert api_client.get("/api/items").json()["total"] == 1
    with database() as session:
        assert session.scalar(select(func.count()).select_from(ContentVersion)) == 1


def test_candidate_expiry_does_not_expire_favorite(database, api_client, material):
    item_id, _ = material()
    with database.begin() as session:
        session.get(ContentItem, item_id).candidate_expires_at = datetime.now(UTC) - timedelta(
            days=1
        )
    assert api_client.get("/api/items").json()["total"] == 0
    star(api_client, item_id)
    assert api_client.get("/api/items?scope=library").json()["total"] == 1


def test_changes_notify_only_after_star_ignore_activity_and_dedup(database, api_client, material):
    item_id, raw = material()
    material(replace(raw, clean_text="first change before star"))
    assert api_client.get("/api/updates").json()["total"] == 0
    star(api_client, item_id)
    changed = replace(raw, clean_text="real content update")
    material(changed)
    material(changed)
    material(replace(changed, metadata={**changed.metadata, "updated_at": "2026-10-07"}))
    result = api_client.get("/api/updates").json()
    assert result["total"] == 1 and result["unread"] == 1
    assert result["items"][0]["previous_version_id"]
    notice_id = result["items"][0]["id"]
    assert api_client.patch(f"/api/updates/{notice_id}/read").status_code == 200
    assert api_client.get("/api/updates").json()["unread"] == 0
    star(api_client, item_id, False)
    material(replace(raw, clean_text="after unstar"))
    assert api_client.get("/api/updates").json()["total"] == 0
    with database() as session:
        assert session.scalar(select(func.count()).select_from(UpdateNotice)) == 1


def test_saved_repo_only_new_releases_notify(database, api_client, material):
    repo = RawItem(
        external_id="repo:1",
        url="https://github.com/example/repo",
        title="example/repo",
        content_type="repository",
        clean_text="vector database",
    )
    repo_id, _ = material(repo)
    star(api_client, repo_id)
    old = RawItem(
        external_id="release:1",
        url=repo.url + "/releases/tag/old",
        title="old",
        content_type="release",
        published_at=datetime.now(UTC) - timedelta(days=1),
        metadata={"repository": repo.url},
    )
    material(old)
    new = replace(
        old,
        external_id="release:2",
        url=repo.url + "/releases/tag/new",
        title="new",
        published_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    material(new)
    material(new)
    result = api_client.get("/api/updates").json()
    assert result["total"] == 1 and result["items"][0]["kind"] == "new_release"
    with database() as session:
        assert session.scalar(
            select(Source.id).where(Source.kind == "github", Source.url == repo.url)
        )


def test_favorites_and_updates_are_user_scoped(database, api_client, material):
    item_id, raw = material()
    star(api_client, item_id)
    material(replace(raw, clean_text="change"))
    notice_id = api_client.get("/api/updates").json()["items"][0]["id"]
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"user_id": uuid4()}
    )
    assert (
        api_client.put(f"/api/items/{item_id}/favorite", json={"enabled": True}).status_code == 404
    )
    assert api_client.get("/api/items?scope=library").json()["total"] == 0
    assert api_client.get("/api/updates").json()["total"] == 0
    assert api_client.patch(f"/api/updates/{notice_id}/read").status_code == 404


def test_settings_explicit_versioned_noop_and_no_profile_inference(database, api_client):
    result = api_client.get("/api/discovery/settings").json()
    assert result["version"] == 0 and not result["data"]["daily_enabled"]
    with database() as session:
        assert session.scalar(select(func.count()).select_from(DiscoverySettings)) == 0
    settings(api_client, recent_topics=["vector", "VECTOR"])
    assert api_client.get("/api/discovery/settings").json()["data"]["recent_topics"] == ["vector"]
    assert (
        api_client.patch(
            "/api/discovery/settings", json={"expected_version": 0, "hour": 10}
        ).status_code
        == 409
    )
    assert (
        api_client.patch("/api/discovery/settings", json={"expected_version": 1, "hour": 9}).json()[
            "version"
        ]
        == 1
    )
    assert api_client.get("/api/profile").json()["version"] == 0


def test_discovery_snapshots_interests_runs_once_and_does_not_star(
    database, api_client, material, monkeypatch
):
    item_id, _ = material()
    settings(api_client, recent_topics=["vector"])
    deliveries = []
    monkeypatch.setattr(tasks.discover, "delay", lambda job: deliveries.append(job))
    one = api_client.post("/api/discovery/runs")
    two = api_client.post("/api/discovery/runs")
    assert one.status_code == 202 and one.json()["id"] == two.json()["id"]
    assert len(deliveries) == 1
    run_id = UUID(one.json()["id"])
    service.execute(run_id)
    service.execute(run_id)
    response = api_client.get(f"/api/discovery/runs/{run_id}/recommendations").json()
    assert len(response["items"]) == 1
    assert response["items"][0]["reason"].startswith("匹配近期明确兴趣")
    assert response["items"][0]["summary_label"] == "来源节选（非 AI 总结）"
    assert not response["items"][0]["favorite"]
    assert api_client.get("/api/items?scope=library").json()["total"] == 0
    with database() as session:
        assert session.scalar(select(func.count()).select_from(Recommendation)) == 1


def test_fixed_column_independent_blocked_priority_no_irrelevant_fill():
    now = datetime.now(UTC)

    def row(name, kind="article"):
        return {
            "item_id": uuid4(),
            "title": name,
            "body": "",
            "metadata": {},
            "kind": kind,
            "source_ids": ["one"],
            "fetched_at": now,
        }

    terms = [{"term": "vector", "origin": "近期明确兴趣", "weight": 2}]
    column = {
        "name": "security",
        "keywords": ["security"],
        "source_ids": [],
        "content_types": [],
        "items_per_day": 2,
    }
    chosen, gaps = service.rank(
        [row("security basics"), row("vector tutorial"), row("unrelated"), row("security blocked")],
        terms,
        [column],
        ["blocked"],
        {},
        0.55,
    )
    assert [r["title"] for r in chosen] == ["security basics", "vector tutorial"]
    assert gaps == [{"column": "security", "missing": 1}]
    restricted = {**column, "source_ids": ["other"]}
    fixed_terms = [{"term": "security", "origin": "固定栏目：security", "weight": 1}]
    assert service.rank([row("security")], fixed_terms, [restricted], [], {}, 0.55)[0] == []


def test_semantic_matching_is_internal_cache_and_no_cloud(
    database, api_client, material, monkeypatch
):
    item_id, _ = material()
    rows = service.candidate_rows(get_settings().user_id)
    calls = []

    class Embedder:
        def encode(self, texts, *, query=False):
            calls.append((list(texts), query))
            return type("Result", (), {"vectors": [[1.0] + [0.0] * 1023 for _ in texts]})()

    monkeypatch.setattr(
        service.providers,
        "get_embedder",
        lambda provider, cfg: Embedder() if provider == "local" else pytest.fail("cloud call"),
    )
    terms = [{"term": "semantic differently worded", "origin": "近期明确兴趣", "weight": 2}]
    scores = service.semantic_scores(get_settings().user_id, rows, terms)
    assert scores[item_id][0] == 1
    service.semantic_scores(get_settings().user_id, rows, terms)
    assert sum(not query for _, query in calls) == 1
    chosen, _ = service.rank(rows, terms, [], [], scores, 0.55)
    assert "语义接近" in chosen[0]["reason"]


def test_queue_failure_and_empty_interests_do_not_call_external(api_client, monkeypatch):
    assert api_client.post("/api/discovery/runs").status_code == 422
    settings(api_client, recent_topics=["vector"])

    def fail(*args):
        raise RuntimeError("secret-password")

    monkeypatch.setattr(tasks.discover, "delay", fail)
    assert api_client.post("/api/discovery/runs").status_code == 503
    row = api_client.get("/api/discovery/runs").json()[0]
    assert row["status"] == "failed" and "secret" not in str(row)


def test_daily_disabled_and_schedule_date_idempotent(database, api_client, monkeypatch):
    deliveries = []
    monkeypatch.setattr(tasks.discover, "delay", lambda job: deliveries.append(job))
    tasks.discovery_due()
    assert not deliveries
    settings(api_client, daily_enabled=True, hour=0, recent_topics=["vector"])
    tasks.discovery_due()
    tasks.discovery_due()
    assert len(deliveries) == 1
    with database.begin() as session:
        run = session.scalar(select(DiscoveryRun))
        assert run.schedule_key
        run.status = "succeeded"
    tasks.discovery_due()
    assert len(deliveries) == 1


def test_github_search_adapter_preserves_scope_and_incomplete_is_error(monkeypatch):
    from app.connectors.contracts import SourceError

    data = {
        "incomplete_results": False,
        "items": [
            {
                "id": 1,
                "html_url": "https://github.com/a/b",
                "full_name": "a/b",
                "owner": {"login": "a"},
                "description": "vector",
                "topics": [],
            }
        ],
    }
    with_response = SafeHTTP(httpx.MockTransport(lambda req: httpx.Response(200, json=data)))
    try:
        result = fetch_source(
            "github_search",
            "https://api.github.com/search/repositories?q=vector",
            {},
            with_response,
        )
        assert len(result.items) == 1 and result.items[0].acquisition_level == "metadata_only"
        data["incomplete_results"] = True
        with pytest.raises(SourceError, match="不完整"):
            fetch_source(
                "github_search",
                "https://api.github.com/search/repositories?q=vector",
                {},
                with_response,
            )
    finally:
        with_response.close()


def test_same_version_cools_down_new_version_can_return(
    database, api_client, material, monkeypatch
):
    _, raw = material()
    settings(api_client, recent_topics=["vector"])
    monkeypatch.setattr(tasks.discover, "delay", lambda *args: None)
    first = api_client.post("/api/discovery/runs").json()["id"]
    service.execute(UUID(first))
    assert service.candidate_rows(get_settings().user_id) == []
    with database.begin() as session:
        session.get(DiscoveryRun, UUID(first)).created_at = datetime.now(UTC) - timedelta(days=8)
    assert len(service.candidate_rows(get_settings().user_id)) == 1
    with database.begin() as session:
        session.get(DiscoveryRun, UUID(first)).created_at = datetime.now(UTC)
    material(replace(raw, clean_text="vector search updated tutorial"))
    assert len(service.candidate_rows(get_settings().user_id)) == 1


def test_daily_requires_target_and_saved_only_checks_updates(api_client, material, monkeypatch):
    assert (
        api_client.patch(
            "/api/discovery/settings", json={"expected_version": 0, "daily_enabled": True}
        ).status_code
        == 422
    )
    item_id, _ = material()
    star(api_client, item_id)
    settings(api_client, daily_enabled=True, hour=0)
    monkeypatch.setattr(tasks.discover, "delay", lambda *args: None)
    tasks.discovery_due()
    runs = api_client.get("/api/discovery/runs").json()
    assert len(runs) == 1
    service.execute(UUID(runs[0]["id"]))
    assert (
        api_client.get(f"/api/discovery/runs/{runs[0]['id']}/recommendations").json()["items"] == []
    )


def test_update_check_only_saved_sources_and_explicit_recovery(
    database, api_client, material, monkeypatch
):
    item_id, _ = material()
    star(api_client, item_id)
    with database.begin() as session:
        session.add(
            Source(
                user_id=get_settings().user_id,
                name="not watched",
                kind="rss",
                url="https://example.com/not-watched",
            )
        )
    called = []
    original = service.refresh_sources

    def record(user, ids):
        called.extend(ids)
        return original(user, ids)

    monkeypatch.setattr(service, "refresh_sources", record)
    monkeypatch.setattr(tasks.discover, "delay", lambda *args: None)
    run_id = UUID(api_client.post("/api/discovery/check-updates").json()["id"])
    service.execute(run_id)
    assert len(called) == 1
    assert api_client.get(f"/api/discovery/runs/{run_id}/recommendations").json()["items"] == []
    pending = UUID(api_client.post("/api/discovery/check-updates").json()["id"])
    service.fail_run(pending, get_settings().user_id)
    service.execute(pending)
    with database() as session:
        assert session.get(DiscoveryRun, pending).status == "failed"


def test_local_failure_labeled_no_cloud_fallback(api_client, material, monkeypatch):
    from app.retrieval.providers import EmbeddingError

    material()
    settings(api_client, recent_topics=["vector"], matching="local")
    monkeypatch.setattr(tasks.discover, "delay", lambda *args: None)

    def fail(*args):
        raise EmbeddingError("本地模型未就绪")

    monkeypatch.setattr(service, "semantic_scores", fail)
    run_id = UUID(api_client.post("/api/discovery/runs").json()["id"])
    service.execute(run_id)
    response = api_client.get(f"/api/discovery/runs/{run_id}/recommendations").json()
    assert response["run"]["status"] == "partial"
    assert response["run"]["report"]["matching"] == "rules-fallback"
    assert "明确回退规则" in response["run"]["report"]["warnings"][0]
    assert len(response["items"]) == 1
