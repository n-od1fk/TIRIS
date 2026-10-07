from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.main import app
from app.models import (
    ContentItem,
    ContentOccurrence,
    ContentVersion,
    EmbeddingChunk,
    EmbeddingJob,
    Source,
)
from app.retrieval import providers, service
from app.retrieval.api import index_embedding
from app.retrieval.providers import EmbeddingError, EmbeddingResult
from tests.test_embedding_providers import vector

pytestmark = pytest.mark.integration


def material(database, title="Vector search", body="vector database", user_id=None, item_id=None):
    now = datetime.now(UTC)
    with database.begin() as session:
        source = Source(
            user_id=user_id or get_settings().user_id,
            name="Fixture",
            kind="rss",
            url=f"https://example.com/{uuid4()}",
        )
        session.add(source)
        session.flush()
        item = (
            session.get(ContentItem, item_id)
            if item_id
            else ContentItem(
                user_id=source.user_id,
                title=title,
                canonical_url=f"https://example.com/{uuid4()}",
                content_type="article",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        session.add(item)
        session.flush()
        number = (
            session.scalar(
                select(func.coalesce(func.max(ContentVersion.version_number), 0)).where(
                    ContentVersion.item_id == item.id
                )
            )
            + 1
        )
        version = ContentVersion(
            user_id=source.user_id,
            item_id=item.id,
            source_id=source.id,
            version_number=number,
            title=title,
            fetched_at=now,
            acquisition_level="abstract_only",
            clean_text=body,
            body_hash="fixture-hash",
            fingerprint=str(uuid4()),
            metadata_json={"evidence_scope": "Fixture excerpt"},
        )
        session.add_all(
            [
                version,
                ContentOccurrence(
                    user_id=source.user_id,
                    source_id=source.id,
                    item_id=item.id,
                    external_id=str(uuid4()),
                    original_url=item.canonical_url,
                    first_seen_at=now,
                    last_seen_at=now,
                ),
            ]
        )
        session.flush()
        return item.id, version.id, source.id


class FakeEmbedder:
    """Synthetic basis vectors prove plumbing, not embedding/recommendation quality."""

    def __init__(self):
        self.calls = []
        self.fail_at = None

    def encode(self, texts, *, query=False):
        self.calls.append((texts, query))
        if len(self.calls) == self.fail_at:
            raise EmbeddingError("Synthetic provider failure")
        return EmbeddingResult([vector(0 if "vector" in text.lower() else 1) for text in texts], 7)


@pytest.fixture
def fake(monkeypatch):
    model = FakeEmbedder()
    monkeypatch.setattr(providers, "readiness", lambda *args: None)
    monkeypatch.setattr(providers, "get_embedder", lambda *args: model)
    monkeypatch.setattr(service, "get_embedder", lambda *args: model)
    monkeypatch.setattr(index_embedding, "delay", lambda *args: None)
    return model


def index(client, max_items=3, provider="local", consent=False):
    response = client.post(
        "/api/retrieval/index",
        json={"provider": provider, "max_items": max_items, "consent_external": consent},
    )
    assert response.status_code == 202, response.text
    service.execute_index(UUID(response.json()["id"]))
    return response.json()["id"]


def search(client, provider="local", **fields):
    return client.post(
        "/api/retrieval/search", json={"provider": provider, "query": "vector retrieval", **fields}
    )


def test_exact_cosine_retrieval_dedup_and_source_filter(database, api_client, fake):
    relevant, _, source_id = material(database, body="vector " * 300)
    other, _, _ = material(database, title="Firewall", body="security")
    index(api_client)
    result = search(api_client).json()
    assert result["indexed_items"] == 2
    assert [row["id"] for row in result["items"]] == [str(relevant), str(other)]
    assert result["items"][0]["similarity"] == pytest.approx(1.0)
    assert result["items"][0]["acquisition_level"] == "abstract_only"
    assert result["items"][0]["matched_passage"].startswith("Title:")
    assert len(search(api_client, source_id=str(source_id)).json()["items"]) == 1
    before = len(fake.calls)
    assert search(api_client, source_id=str(uuid4())).json()["items"] == []
    assert len(fake.calls) == before


def test_latest_version_only_and_repeat_index_idempotence(database, api_client, fake):
    item, _, _ = material(database)
    index(api_client)
    before = len(fake.calls)
    index(api_client)
    assert len(fake.calls) == before
    material(database, title="Changed", body="new text", item_id=item)
    stale = search(api_client).json()
    assert stale["indexed_items"] == 0 and stale["items"] == []
    assert len(fake.calls) == before
    index(api_client)
    assert search(api_client).json()["items"][0]["version"] == 2
    with database() as session:
        assert session.scalar(select(func.count()).select_from(EmbeddingChunk)) == 2


def test_active_job_dedup_and_worker_redelivery(database, api_client, fake):
    material(database)
    first = api_client.post("/api/retrieval/index", json={"provider": "local"}).json()
    second = api_client.post("/api/retrieval/index", json={"provider": "local"}).json()
    assert first["id"] == second["id"]
    service.execute_index(UUID(first["id"]))
    count = len(fake.calls)
    service.execute_index(UUID(first["id"]))
    assert len(fake.calls) == count


def test_partial_failure_keeps_completed_documents_and_manual_resume(database, api_client, fake):
    material(database)
    material(database, title="Security", body="security")
    fake.fail_at = 2
    job_id = index(api_client)
    with database() as session:
        job = session.get(EmbeddingJob, UUID(job_id))
        assert job.status == "failed" and job.indexed_items == 1
        assert session.scalar(select(func.count()).select_from(EmbeddingChunk)) == 1
    index(api_client)
    assert len(fake.calls) == 3
    assert search(api_client).json()["indexed_items"] == 2


def test_no_index_does_not_call_model_or_infer_profile(database, api_client, fake):
    material(database)
    assert search(api_client).json()["items"] == []
    assert fake.calls == []
    assert api_client.get("/api/profile").json()["version"] == 0


def test_models_same_dimension_are_separate_spaces(database, api_client, fake, monkeypatch):
    settings = get_settings().model_copy(
        update={
            "bailian_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "bailian_api_key": "synthetic-test-key",
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    material(database)
    index(api_client)
    assert search(api_client, "bailian", consent_external=True).json()["indexed_items"] == 0
    assert api_client.post("/api/retrieval/index", json={"provider": "bailian"}).status_code == 422
    index(api_client, provider="bailian", consent=True)
    assert search(api_client, "bailian").status_code == 422
    assert search(api_client, "bailian", consent_external=True).json()["indexed_items"] == 1
    backends = api_client.get("/api/retrieval/backends").json()
    assert backends[0]["key"] != backends[1]["key"]


def test_user_scope_excludes_other_material_and_index(database, api_client, fake):
    material(database)
    material(database, title="Other user", user_id=uuid4())
    index(api_client)
    assert search(api_client).json()["indexed_items"] == 1
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"user_id": uuid4()}
    )
    result = search(api_client).json()
    assert result["items"] == [] and result["indexed_items"] == 0


def test_queue_failure_saved_without_provider_call(database, api_client, fake, monkeypatch):
    material(database)

    def broken(*args):
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(index_embedding, "delay", broken)
    response = api_client.post("/api/retrieval/index", json={"provider": "local"})
    assert response.status_code == 503
    assert "synthetic-secret" not in response.text
    assert fake.calls == []
    with database() as session:
        assert session.scalar(select(EmbeddingJob)).status == "failed"


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "unknown"},
        {"provider": "local", "max_items": 21},
        {"provider": "local", "max_items": True},
        {"provider": "local", "user_id": str(uuid4())},
    ],
)
def test_index_validation_does_not_call_provider(api_client, fake, body):
    assert api_client.post("/api/retrieval/index", json=body).status_code == 422
    assert fake.calls == []


def test_blank_query_and_changed_descriptor_no_call(database, api_client, fake, monkeypatch):
    material(database)
    assert search(api_client, query="  ").status_code == 422
    submitted = api_client.post("/api/retrieval/index", json={"provider": "local"}).json()
    original = service.descriptor
    monkeypatch.setattr(
        service, "descriptor", lambda *args: {**original(*args), "revision": "changed"}
    )
    service.execute_index(UUID(submitted["id"]))
    with database() as session:
        assert session.get(EmbeddingJob, UUID(submitted["id"])).status == "failed"
    assert fake.calls == []
