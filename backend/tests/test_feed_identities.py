from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_discovery_favorites import material as material

from app.config import get_settings
from app.discovery import service
from app.models import (
    ContentItem,
    ContentOccurrence,
    ContentVersion,
    DiscoveryRun,
    Favorite,
    FixedColumn,
    LocalIdentity,
    Recommendation,
    Source,
)
from app.workers import tasks

pytestmark = pytest.mark.integration


def populate(database, count=55, user_id=None):
    uid, now = user_id or get_settings().user_id, datetime.now(UTC)
    ids = []
    with database.begin() as session:
        source = Source(user_id=uid, name="Test news", kind="rss", url="https://example.com/feed")
        session.add(source)
        session.flush()
        for index in range(count):
            item = ContentItem(
                user_id=uid,
                canonical_url=f"https://example.com/{index}",
                title=f"vector news {index}",
                content_type="article",
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(item)
            session.flush()
            version = ContentVersion(
                user_id=uid,
                item_id=item.id,
                source_id=source.id,
                version_number=1,
                title=item.title,
                fetched_at=now,
                published_at=now,
                acquisition_level="abstract_only",
                clean_text="vector news source excerpt",
                body_hash=str(index).zfill(64),
                fingerprint=str(index).zfill(64),
                metadata_json={"evidence_scope": "RSS excerpt"},
            )
            session.add(version)
            session.flush()
            session.add(
                ContentOccurrence(
                    user_id=uid,
                    item_id=item.id,
                    source_id=source.id,
                    external_id=str(index),
                    original_url=item.canonical_url,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            ids.append((item.id, version.id))
    return ids


def recommend(database, ids, created=None, user_id=None):
    with database.begin() as session:
        run = DiscoveryRun(
            user_id=user_id or get_settings().user_id,
            status="succeeded",
            created_at=created or datetime.now(UTC),
            finished_at=datetime.now(UTC),
            report={"recommendation_count": len(ids)},
            snapshot={"profile_version": 0, "refresh_sources": True},
        )
        session.add(run)
        session.flush()
        for position, (item, version) in enumerate(ids, 1):
            session.add(
                Recommendation(
                    user_id=run.user_id,
                    run_id=run.id,
                    item_id=item,
                    version_id=version,
                    position=position,
                    reason="matches vector",
                    score={"source": "Test news"},
                )
            )
        return run.id


def test_identity_list_is_read_only_and_default_data_stays(database, api_client):
    uid = get_settings().user_id
    data = api_client.get("/api/identities").json()
    assert data == {"default_id": str(uid), "items": [{"id": str(uid), "name": "我的身份"}]}
    with database() as session:
        assert session.scalar(select(func.count()).select_from(LocalIdentity)) == 0
    assert (
        api_client.patch(f"/api/identities/{uid}", json={"name": "  我的网安  "}).json()["name"]
        == "我的网安"
    )
    assert api_client.get("/api/identities").json()["items"][0]["id"] == str(uid)
    assert api_client.post("/api/identities", json={"name": " "}).status_code == 422


def test_identity_switch_scopes_real_backend_not_just_label(api_client, material):
    item, _ = material()
    api_client.patch(
        "/api/profile", json={"expected_version": 0, "long_term_directions": ["security"]}
    )
    api_client.put(f"/api/items/{item}/favorite", json={"enabled": True})
    response = api_client.post("/api/identities", json={"name": "AI 学习"})
    assert response.status_code == 201
    other = response.json()["id"]
    headers = {"X-TIRIS-User": other}
    assert api_client.get("/api/profile", headers=headers).json()["version"] == 0
    assert api_client.get("/api/items?scope=library", headers=headers).json()["total"] == 0
    assert api_client.get(f"/api/items/{item}", headers=headers).status_code == 404
    assert (
        api_client.put(
            f"/api/items/{item}/favorite", json={"enabled": False}, headers=headers
        ).status_code
        == 404
    )
    assert (
        api_client.get("/api/discovery/settings", headers=headers).json()["data"]["github_search"]
        is False
    )
    assert api_client.get("/api/updates", headers=headers).json()["total"] == 0
    assert (
        api_client.patch(
            "/api/profile",
            json={"expected_version": 0, "long_term_directions": ["AI"]},
            headers=headers,
        ).status_code
        == 200
    )
    assert api_client.get("/api/profile").json()["data"]["long_term_directions"] == ["security"]
    assert api_client.get("/api/items?scope=library").json()["total"] == 1
    assert get_settings().user_id != UUID(other)
    assert api_client.get("/api/sources", headers=headers).json() == []


def test_unknown_identity_rejected_no_authentication(api_client):
    assert api_client.get("/api/profile", headers={"X-TIRIS-User": str(uuid4())}).status_code == 404
    assert api_client.get("/api/feed", headers={"X-TIRIS-User": "invalid"}).status_code == 422


def test_two_populated_identities_have_distinct_feeds_and_libraries(database, api_client):
    other = UUID(api_client.post("/api/identities", json={"name": "视频剪辑"}).json()["id"])
    headers = {"X-TIRIS-User": str(other)}
    first = populate(database, 2)
    second = populate(database, 2, user_id=other)
    recommend(database, first)
    recommend(database, second, user_id=other)
    # Same canonical URLs in distinct spaces remain independent records.
    api_client.put(f"/api/items/{first[0][0]}/favorite", json={"enabled": True})
    api_client.put(f"/api/items/{second[1][0]}/favorite", headers=headers, json={"enabled": True})
    assert {row["id"] for row in api_client.get("/api/feed").json()["items"]} == {
        str(row[0]) for row in first
    }
    assert {row["id"] for row in api_client.get("/api/feed", headers=headers).json()["items"]} == {
        str(row[0]) for row in second
    }
    assert [row["id"] for row in api_client.get("/api/items?scope=library").json()["items"]] == [
        str(first[0][0])
    ]
    assert [
        row["id"]
        for row in api_client.get("/api/items?scope=library", headers=headers).json()["items"]
    ] == [str(second[1][0])]


def test_source_creation_sync_and_history_are_scoped_to_identity(api_client, database, monkeypatch):
    from app.connectors.contracts import FetchResult, RawItem

    other = api_client.post("/api/identities", json={"name": "视频剪辑"}).json()["id"]
    headers = {"X-TIRIS-User": other}
    payload = {
        "name": "Black Hat",
        "kind": "blackhat",
        "url": "https://www.blackhat.com/us-26/briefings.html",
    }
    first = api_client.post("/api/sources", json=payload).json()["id"]
    second_response = api_client.post("/api/sources", json=payload, headers=headers)
    assert second_response.status_code == 201
    second = second_response.json()["id"]
    assert first != second
    assert first not in {
        row["id"] for row in api_client.get("/api/sources", headers=headers).json()
    }
    assert second not in {row["id"] for row in api_client.get("/api/sources").json()}
    assert (
        api_client.patch(
            f"/api/sources/{first}", headers=headers, json={"enabled": False}
        ).status_code
        == 404
    )
    assert (
        api_client.post("/api/sources/sync", headers=headers, json={"source_id": first}).status_code
        == 404
    )
    monkeypatch.setattr(tasks.sync_source, "delay", lambda *args: None)
    submitted = api_client.post("/api/sources/sync", headers=headers, json={"source_id": second})
    assert submitted.status_code == 202
    job_id = submitted.json()["id"]
    monkeypatch.setattr(
        tasks,
        "fetch_source",
        lambda *args: FetchResult(
            [
                RawItem(
                    external_id="briefing-one",
                    url="https://www.blackhat.com/test-briefings.html",
                    title="公开索引条目",
                    content_type="conference_index",
                    clean_text="索引标题",
                    acquisition_level="metadata_only",
                )
            ]
        ),
    )
    tasks.execute_sync(UUID(job_id))
    assert api_client.get(f"/api/jobs/{job_id}").status_code == 404
    assert api_client.get("/api/jobs").json() == []
    assert api_client.get(f"/api/jobs/{job_id}", headers=headers).json()["status"] == "succeeded"
    assert api_client.get(f"/api/sources/{second}/runs").status_code == 404
    assert len(api_client.get(f"/api/sources/{second}/runs", headers=headers).json()) == 1
    assert api_client.get("/api/items", headers=headers).json()["total"] == 1
    assert api_client.get("/api/items").json()["total"] == 0
    assert api_client.get("/api/items?scope=library", headers=headers).json()["total"] == 0


def test_manual_source_sync_wakes_empty_feed_without_another_network_refresh(
    database, api_client, monkeypatch
):
    populate(database, 1)
    api_client.patch(
        "/api/profile", json={"expected_version": 0, "long_term_directions": ["vector"]}
    )
    monkeypatch.setattr(tasks.discover, "delay", lambda *args: None)
    refreshes = []
    monkeypatch.setattr(service, "refresh_sources", lambda *args: refreshes.append(True) or [])
    first = UUID(api_client.post("/api/feed/refill").json()["run"]["id"])
    service.execute(first)
    with database.begin() as session:
        session.get(DiscoveryRun, first).finished_at = datetime.now(UTC) - timedelta(seconds=3)
    empty = UUID(api_client.post("/api/feed/refill").json()["run"]["id"])
    service.execute(empty)
    assert api_client.post("/api/feed/refill").json()["state"] == "waiting"
    new_item = populate_new(database)
    resumed = api_client.post("/api/feed/refill").json()
    assert resumed["state"] == "running"
    run = UUID(resumed["run"]["id"])
    service.execute(run)
    assert str(new_item[0]) in {row["id"] for row in api_client.get("/api/feed").json()["items"]}
    assert len(refreshes) == 1
    with database() as session:
        assert session.get(DiscoveryRun, run).snapshot["refresh_sources"] is False


def test_feed_keeps_batches_paginates_stably_and_deduplicates(database, api_client):
    ids = populate(database)
    recommend(database, ids[:30], datetime.now(UTC) - timedelta(minutes=2))
    recommend(database, ids[30:], datetime.now(UTC) - timedelta(minutes=1))
    recommend(database, ids[:1], datetime.now(UTC) - timedelta(seconds=20))
    page = api_client.get("/api/feed").json()
    assert len(page["items"]) == 20
    seen = [row["id"] for row in page["items"]]
    cursor = page["next_cursor"]
    newly = populate_new(database)
    recommend(database, [newly])
    while cursor:
        page = api_client.get("/api/feed", params={"cursor": cursor}).json()
        seen += [row["id"] for row in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 55
    assert str(newly[0]) not in seen
    assert api_client.get("/api/feed").json()["items"][0]["id"] == str(newly[0])
    assert api_client.get("/api/feed?cursor=broken").status_code == 422


def populate_new(database):
    with database.begin() as session:
        template = session.scalar(select(ContentVersion))
        uid, now = template.user_id, datetime.now(UTC)
        item = ContentItem(
            user_id=uid,
            canonical_url="https://example.com/new",
            title="new vector news",
            content_type="article",
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(item)
        session.flush()
        version = ContentVersion(
            user_id=uid,
            item_id=item.id,
            source_id=template.source_id,
            version_number=1,
            title=item.title,
            fetched_at=now,
            acquisition_level="abstract_only",
            clean_text="vector",
            body_hash="f" * 64,
            fingerprint="f" * 64,
        )
        session.add(version)
        session.flush()
        return item.id, version.id


def test_cursor_cannot_cross_identity_and_expiry_not_permanent_library(database, api_client):
    ids = populate(database, 22)
    recommend(database, ids)
    cursor = api_client.get("/api/feed").json()["next_cursor"]
    other = api_client.post("/api/identities", json={"name": "other"}).json()["id"]
    assert (
        api_client.get(
            "/api/feed", params={"cursor": cursor}, headers={"X-TIRIS-User": other}
        ).status_code
        == 422
    )
    item, _ = ids[0]
    api_client.put(f"/api/items/{item}/favorite", json={"enabled": True})
    with database.begin() as session:
        session.get(ContentItem, item).candidate_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert api_client.get("/api/items?scope=library").json()["total"] == 1
    assert str(item) not in {
        row["id"] for row in api_client.get("/api/feed?limit=50").json()["items"]
    }


def test_refill_multiple_batches_without_repeated_network_or_auto_star(
    database, api_client, monkeypatch
):
    populate(database, 32)
    assert api_client.post("/api/feed/refill").json()["state"] == "needs_profile"
    assert (
        api_client.patch(
            "/api/profile", json={"expected_version": 0, "long_term_directions": ["vector"]}
        ).status_code
        == 200
    )
    queued, refreshes = [], []
    monkeypatch.setattr(tasks.discover, "delay", lambda run: queued.append(run))
    monkeypatch.setattr(
        service, "refresh_sources", lambda uid, sources: refreshes.append(sources) or []
    )
    first = api_client.post("/api/feed/refill").json()["run"]["id"]
    assert api_client.post("/api/feed/refill").json()["run"]["id"] == first
    service.execute(UUID(first))
    with database.begin() as session:
        session.get(DiscoveryRun, UUID(first)).finished_at = datetime.now(UTC) - timedelta(
            seconds=3
        )
    second = api_client.post("/api/feed/refill").json()["run"]["id"]
    assert second != first
    service.execute(UUID(second))
    assert len(api_client.get("/api/feed?limit=50").json()["items"]) == 20
    assert len(refreshes) == 1
    assert len(queued) == 2
    with database() as session:
        assert session.get(DiscoveryRun, UUID(second)).snapshot["refresh_sources"] is False
        assert session.scalar(select(func.count()).select_from(Favorite)) == 0
    assert len(service.candidate_rows(get_settings().user_id, feed=True)) == 12


def test_empty_refill_backs_off_profile_change_can_continue(api_client, database, monkeypatch):
    api_client.patch(
        "/api/profile", json={"expected_version": 0, "long_term_directions": ["vector"]}
    )
    monkeypatch.setattr(tasks.discover, "delay", lambda run: None)
    monkeypatch.setattr(service, "refresh_sources", lambda *args: [])
    run = UUID(api_client.post("/api/feed/refill").json()["run"]["id"])
    service.execute(run)
    response = api_client.post("/api/feed/refill").json()
    assert response["state"] == "waiting" and response["retry_after"] > 800
    api_client.patch("/api/profile", json={"expected_version": 1, "long_term_directions": ["AI"]})
    assert api_client.post("/api/feed/refill").json()["run"]["id"] != str(run)


def test_periodic_refill_supports_all_explicit_local_profiles(api_client, database, monkeypatch):
    other = api_client.post("/api/identities", json={"name": "AI"}).json()["id"]
    api_client.patch(
        "/api/profile", json={"expected_version": 0, "long_term_directions": ["vector"]}
    )
    api_client.patch(
        "/api/profile",
        json={"expected_version": 0, "long_term_directions": ["AI"]},
        headers={"X-TIRIS-User": other},
    )
    calls = []
    monkeypatch.setattr(tasks.discover, "delay", lambda run: calls.append(run))
    tasks.feed_due()
    tasks.feed_due()
    assert len(calls) == 2
    with database() as session:
        assert {row.user_id for row in session.scalars(select(DiscoveryRun))} == {
            get_settings().user_id,
            UUID(other),
        }


def test_feed_preserves_daily_fixed_quota_and_new_blocked_filter(database, api_client, monkeypatch):
    populate(database, 32)
    api_client.patch(
        "/api/profile", json={"expected_version": 0, "long_term_directions": ["vector"]}
    )
    with database.begin() as session:
        session.add(
            FixedColumn(
                user_id=get_settings().user_id,
                name="保留栏目",
                name_key="保留栏目",
                keywords=["vector"],
                items_per_day=2,
                updated_at=datetime.now(UTC),
            )
        )
    monkeypatch.setattr(tasks.discover, "delay", lambda run: None)
    monkeypatch.setattr(service, "refresh_sources", lambda *args: [])
    first = UUID(api_client.post("/api/feed/refill").json()["run"]["id"])
    service.execute(first)
    with database.begin() as session:
        session.get(DiscoveryRun, first).finished_at = datetime.now(UTC) - timedelta(seconds=3)
    second = UUID(api_client.post("/api/feed/refill").json()["run"]["id"])
    service.execute(second)
    with database() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Recommendation)
                .where(Recommendation.column_name == "保留栏目")
            )
            == 2
        )
    assert len(api_client.get("/api/feed?limit=50").json()["items"]) == 20
    api_client.patch("/api/profile", json={"expected_version": 1, "blocked_topics": ["vector"]})
    assert api_client.get("/api/feed").json()["items"] == []
