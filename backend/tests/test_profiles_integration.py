from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.main import app
from app.models import FixedColumn, LongTermProfile, ProfileRevision, Source

pytestmark = pytest.mark.integration


def save_profile(client, version=0, **fields):
    return client.patch("/api/profile", json={"expected_version": version, **fields})


def create_column(client, **fields):
    return client.post(
        "/api/fixed-columns",
        json={
            "name": "Security",
            "keywords": ["security"],
            **fields,
        },
    )


def test_empty_profile_is_unknown_and_get_does_not_write(database, api_client):
    data = api_client.get("/api/profile").json()
    assert data["version"] == 0
    assert data["data"]["background"] == ""
    assert data["data"]["language_preferences"] == []
    assert data["data"]["daily_reading_minutes"] is None
    assert data["field_metadata"] == {}
    assert api_client.get("/api/profile/history").json() == []
    assert save_profile(api_client).json()["version"] == 0
    with database() as session:
        assert session.scalar(select(func.count()).select_from(LongTermProfile)) == 0


def test_profile_partial_update_metadata_and_immutable_history(api_client):
    first = save_profile(
        api_client, background="  网安本科生  ", foundations=["Python", "python", " HTTP "]
    ).json()
    assert first["data"]["background"] == "网安本科生"
    assert first["data"]["foundations"] == ["Python", "HTTP"]
    assert set(first["field_metadata"]) == {"background", "foundations"}
    assert first["field_metadata"]["background"]["source"] == "user_explicit"
    second = save_profile(api_client, 1, learning_goals=["理解 RAG"]).json()
    assert second["version"] == 2
    assert second["data"]["background"] == first["data"]["background"]
    assert second["field_metadata"]["background"] == first["field_metadata"]["background"]
    history = api_client.get("/api/profile/history").json()
    assert [r["version"] for r in history] == [2, 1]
    assert history[0]["changed_fields"] == ["learning_goals"]
    assert history[1]["data"]["learning_goals"] == []
    assert api_client.get("/api/profile/history?offset=1&limit=1").json() == [history[1]]


def test_profile_noop_clear_and_stale_write(api_client):
    first = save_profile(api_client, daily_reading_minutes=30, foundations=["Python"]).json()
    noop = save_profile(api_client, 1, foundations=["Python"]).json()
    assert noop == first
    clear = save_profile(api_client, 1, foundations=[], daily_reading_minutes=None).json()
    assert clear["version"] == 2
    assert clear["data"]["foundations"] == []
    assert clear["data"]["daily_reading_minutes"] is None
    response = save_profile(api_client, 1, background="Stale page")
    assert response.status_code == 409
    assert response.json()["detail"]["current_version"] == 2
    assert api_client.get("/api/profile").json() == clear
    assert len(api_client.get("/api/profile/history").json()) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"dynamic_interest": "AI"},
        {"daily_reading_minutes": True},
        {"daily_reading_minutes": 241},
        {"daily_reading_minutes": "30"},
        {"foundations": [""]},
        {"language_preferences": ["unknown"]},
        {"expected_version": -1},
        {"background": None},
    ],
)
def test_invalid_profile_is_rejected(api_client, payload):
    assert save_profile(api_client, **payload).status_code == 422
    assert api_client.get("/api/profile").json()["version"] == 0


def test_profile_and_columns_do_not_change_each_other(api_client):
    column = create_column(api_client).json()
    profile = save_profile(api_client, long_term_directions=["网络安全"]).json()
    assert api_client.get("/api/fixed-columns").json() == [column]
    assert (
        api_client.patch(
            f"/api/fixed-columns/{column['id']}",
            json={
                "expected_version": 1,
                "enabled": False,
            },
        ).status_code
        == 200
    )
    assert api_client.get("/api/profile").json() == profile


def test_column_name_normalization_and_keyword_deduplication(api_client):
    one = create_column(api_client, name=" ＡＩ   Security ", keywords=["AI", "ai"]).json()
    assert one["keywords"] == ["AI"]
    assert create_column(api_client, name="ai security").status_code == 409
    assert create_column(api_client, name="\u0130" * 100).status_code == 422


def test_column_quota_enable_edit_noop_and_stale_write(api_client):
    one = create_column(api_client, items_per_day=6).json()
    assert create_column(api_client, name="Too much", items_per_day=5).status_code == 422
    two = create_column(api_client, name="Disabled", items_per_day=6, enabled=False).json()
    url = f"/api/fixed-columns/{two['id']}"
    assert api_client.patch(url, json={"expected_version": 1, "enabled": True}).status_code == 422
    edited = api_client.patch(
        url,
        json={
            "expected_version": 1,
            "enabled": True,
            "items_per_day": 4,
        },
    ).json()
    assert edited["version"] == 2
    assert api_client.patch(url, json={"expected_version": 2, "items_per_day": 4}).json() == edited
    assert api_client.patch(url, json={"expected_version": 1, "enabled": False}).status_code == 409
    assert api_client.patch(url, json={"expected_version": 2, "keywords": []}).status_code == 422
    assert api_client.patch(url, json={"expected_version": 2, "enabled": None}).status_code == 422
    assert (
        api_client.patch(url, json={"expected_version": 2, "name": one["name"]}).status_code == 409
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"keywords": []},
        {"content_types": ["invalid"]},
        {"items_per_day": True},
        {"items_per_day": 0},
        {"enabled": "yes"},
        {"name": " "},
        {"unexpected": "value"},
    ],
)
def test_invalid_columns_rejected(api_client, payload):
    assert create_column(api_client, **payload).status_code == 422
    assert api_client.get("/api/fixed-columns").json() == []


def test_owned_sources_and_user_scope(database, api_client):
    other_id = uuid4()
    with database.begin() as session:
        owned = Source(
            user_id=get_settings().user_id, name="Owned", kind="rss", url="https://example.com/own"
        )
        foreign = Source(
            user_id=other_id, name="Other", kind="rss", url="https://example.com/other"
        )
        session.add_all([owned, foreign])
        session.flush()
        owned_id, foreign_id = str(owned.id), str(foreign.id)
    for source_id in [foreign_id, str(uuid4())]:
        assert create_column(api_client, source_ids=[source_id]).status_code == 404
    column = create_column(api_client, keywords=[], source_ids=[owned_id, owned_id]).json()
    assert column["source_ids"] == [owned_id]
    save_profile(api_client, background="Private data")
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"user_id": other_id}
    )
    assert api_client.get("/api/profile").json()["version"] == 0
    assert api_client.get("/api/profile/history").json() == []
    assert api_client.get("/api/fixed-columns").json() == []
    assert (
        api_client.patch(
            f"/api/fixed-columns/{column['id']}",
            json={
                "expected_version": 1,
                "enabled": False,
            },
        ).status_code
        == 404
    )
    assert create_column(api_client, name="Security", source_ids=[foreign_id]).status_code == 201


def test_maximum_saved_columns_includes_disabled(api_client):
    for index in range(20):
        assert create_column(api_client, name=f"Rule {index}", enabled=False).status_code == 201
    assert create_column(api_client, name="Rule 21", enabled=False).status_code == 422


def test_concurrent_first_profile_write_preserves_one_revision(database, api_client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda title: save_profile(api_client, background=title), ["Page A", "Page B"])
        )
    assert sorted(r.status_code for r in responses) == [200, 409]
    with database() as session:
        assert session.scalar(select(func.count()).select_from(LongTermProfile)) == 1
        assert session.scalar(select(func.count()).select_from(ProfileRevision)) == 1


def test_concurrent_column_creation_cannot_exceed_quota(database, api_client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda title: create_column(api_client, name=title, items_per_day=6),
                ["Rule A", "Rule B"],
            )
        )
    assert sorted(r.status_code for r in responses) == [201, 422]
    with database() as session:
        assert session.scalar(select(func.sum(FixedColumn.items_per_day))) == 6
