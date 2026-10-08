import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.connectors import source_api
from app.connectors.contracts import SourceError
from app.models import ContentItem, LocalIdentity, Source, SourceDiscovery
from app.platforms import api as platform_api
from app.platforms import github
from app.services import credentials

TOKEN = "test-source-token-not-real"


@pytest.fixture
def vault(monkeypatch, tmp_path):
    settings = get_settings().model_copy(
        update={
            "source_credential_key_file": tmp_path / "key",
            "github_token": "environment-token-not-real",
        }
    )
    monkeypatch.setattr(credentials, "get_settings", lambda: settings)
    monkeypatch.setattr(github, "get_settings", lambda: settings)
    return settings


class Network:
    def __init__(self):
        self.calls, self.failed_tokens = [], set()

    def get(self, url, headers=None, **kwargs):
        headers = dict(headers or {})
        self.calls.append((url, headers))
        assert url.startswith("https://api.github.com/"), (
            "Credentials must never go to an arbitrary URL"
        )
        if headers.get("Authorization") in self.failed_tokens:
            raise SourceError("来源拒绝访问或不存在（HTTP 401）")
        if url.endswith("/user"):
            data = {"login": "fixture-user"}
        elif "/search/" in url:
            data = {"items": self.repositories()}
        else:
            data = self.repositories()
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    def repositories(self):
        return [
            {
                "id": 123,
                "full_name": "fixture/one",
                "html_url": "https://github.com/fixture/one",
                "owner": {"login": "fixture"},
                "description": "Public metadata",
                "topics": [],
            }
        ]

    def close(self):
        pass


@pytest.fixture
def network(monkeypatch, vault):
    net = Network()
    monkeypatch.setattr(source_api, "SafeHTTP", lambda: net)
    monkeypatch.setattr(platform_api, "SafeHTTP", lambda: net)
    return net


def detect(client, headers=None):
    response = client.post(
        "/api/sources/discover", json={"url": "https://github.com/"}, headers=headers
    )
    assert response.status_code == 200
    return response.json()


def verify(client, draft, token=TOKEN, headers=None, **extra):
    response = client.post(
        "/api/sources/verify-platform",
        json={
            "discovery_id": draft["discovery_id"],
            "credentials": {"token": token} if token else {},
            "credential_action": "replace" if token else "keep",
            **extra,
        },
        headers=headers,
    )
    return response


def connect(client, verified, headers=None, **extra):
    return client.post(
        "/api/sources/connect",
        json={
            "discovery_id": verified["discovery_id"],
            "selections": [{"candidate_id": verified["candidates"][0]["id"]}],
            **extra,
        },
        headers=headers,
    )


def add(client, headers=None, token=TOKEN):
    verified = verify(client, detect(client, headers), token, headers)
    assert verified.status_code == 200, verified.text
    saved = connect(client, verified.json(), headers)
    assert saved.status_code == 201, saved.text
    return saved.json()["items"][0]["source"]


def test_cipher_is_identity_and_row_bound_and_fails_closed(vault):
    user, row = uuid4(), uuid4()
    encrypted = credentials.encrypt_values({"token": TOKEN}, user, row)
    assert TOKEN not in encrypted
    assert credentials.decrypt_values(encrypted, user, row) == {"token": TOKEN}
    for wrong_user, wrong_row in [(uuid4(), row), (user, uuid4())]:
        with pytest.raises(SourceError, match="无法解密"):
            credentials.decrypt_values(encrypted, wrong_user, wrong_row)
    with pytest.raises(SourceError):
        credentials.decrypt_values(encrypted[:-2] + "XX", user, row)
    vault.source_credential_key_file.unlink()  # This fixture's exact temporary key only.
    with pytest.raises(SourceError, match="密钥不可用"):
        credentials.decrypt_values(encrypted, user, row)


@pytest.mark.integration
def test_root_detect_verify_save_and_use_source_token(network, database, api_client):
    draft = detect(api_client)
    assert draft["platform"]["kind"] == "github" and draft["candidates"] == []
    assert network.calls == []  # Recognition is not yet a validated connection.
    checked = verify(api_client, draft)
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert result["candidates"][0]["verified"]
    assert api_client.get("/api/sources").json() == []
    assert TOKEN not in checked.text
    with database() as session:
        pending = session.get(SourceDiscovery, UUID(result["discovery_id"]))
        assert pending.credentials_ciphertext and TOKEN not in json.dumps(pending.payload)
        assert TOKEN not in pending.credentials_ciphertext
    saved = connect(api_client, result)
    assert saved.status_code == 201, saved.text
    source = saved.json()["items"][0]["source"]
    assert source["kind"] == "github" and source["url"] == "https://github.com/"
    assert source["access_type"] == "platform_api" and not source["sync_supported"]
    assert source["credential_configured"] and source["credential_mode"] == "stored"
    assert "credentials_ciphertext" not in source and TOKEN not in saved.text
    with database() as session:
        row = session.get(Source, UUID(source["id"]))
        assert credentials.source_values(row) == {"token": TOKEN}
    assert api_client.get("/api/items?scope=library").json()["total"] == 0
    response = api_client.post(
        f"/api/platforms/{source['id']}/query", json={"operation": "search", "query": "security"}
    )
    assert response.status_code == 200 and TOKEN not in response.text
    assert network.calls[-1][1]["Authorization"] == "Bearer " + TOKEN
    assert (
        sum(url.endswith("/user") for url, _ in network.calls) == 2
    )  # Verify + save revalidation.


@pytest.mark.integration
def test_bad_token_never_saves_or_falls_back_and_save_rechecks(network, api_client):
    network.failed_tokens.add("Bearer " + TOKEN)
    draft = detect(api_client)
    bad = verify(api_client, draft)
    assert bad.status_code == 422 and "401" in bad.text and TOKEN not in bad.text
    assert api_client.get("/api/sources").json() == []
    assert len(network.calls) == 1
    network.failed_tokens.clear()
    checked = verify(api_client, draft).json()
    network.failed_tokens.add("Bearer " + TOKEN)
    failed_save = connect(api_client, checked)
    assert failed_save.status_code == 422 and TOKEN not in failed_save.text
    assert api_client.get("/api/items").json()["total"] == 0
    assert api_client.get("/api/sources").json() == []


@pytest.mark.integration
def test_anonymous_connection_never_uses_environment_token(network, api_client):
    source = add(api_client, token="")
    assert source["credential_mode"] == "anonymous" and not source["credential_configured"]
    response = api_client.post(f"/api/platforms/{source['id']}/query", json={"operation": "list"})
    assert response.status_code == 200
    assert all("Authorization" not in headers for _, headers in network.calls)
    assert (
        api_client.post(
            f"/api/platforms/{source['id']}/query", json={"operation": "stars"}
        ).status_code
        == 422
    )


@pytest.mark.integration
def test_edit_keep_replace_clear_and_wrong_version(network, database, api_client):
    source = add(api_client)
    id, version = source["id"], source["connection_version"]
    draft = detect(api_client)
    checked = verify(api_client, draft, token="", edit_source_id=id, expected_version=version)
    assert checked.status_code == 200
    saved = connect(api_client, checked.json(), edit_source_id=id, expected_version=version)
    assert saved.status_code == 201
    version = saved.json()["items"][0]["source"]["connection_version"]
    with database() as session:
        assert credentials.source_values(session.get(Source, UUID(id))) == {"token": TOKEN}
    assert (
        verify(api_client, draft, edit_source_id=id, expected_version=version - 1).status_code
        == 409
    )
    replacement = verify(
        api_client, draft, token="replacement-not-real", edit_source_id=id, expected_version=version
    )
    assert replacement.status_code == 200
    saved = connect(api_client, replacement.json(), edit_source_id=id, expected_version=version)
    assert saved.status_code == 201
    version = saved.json()["items"][0]["source"]["connection_version"]
    clear = verify(
        api_client,
        draft,
        token="",
        credential_action="clear",
        edit_source_id=id,
        expected_version=version,
    )
    assert clear.status_code == 200
    saved = connect(api_client, clear.json(), edit_source_id=id, expected_version=version)
    assert (
        saved.status_code == 201 and not saved.json()["items"][0]["source"]["credential_configured"]
    )
    assert "Authorization" not in network.calls[-1][1]
    with database() as session:
        row = session.get(Source, UUID(id))
        assert row.credentials_ciphertext is None and credentials.source_values(row) == {}


@pytest.mark.integration
def test_identity_and_target_binding_and_expired_verification(network, database, api_client):
    other = uuid4()
    with database.begin() as session:
        session.add(LocalIdentity(id=other, name="Other"))
    headers = {"X-TIRIS-User": str(other)}
    draft = detect(api_client)
    assert verify(api_client, draft, headers=headers).status_code == 404
    checked = verify(api_client, draft).json()
    assert connect(api_client, checked, headers=headers).status_code == 404
    assert (
        connect(api_client, checked, edit_source_id=str(uuid4()), expected_version=1).status_code
        == 404
    )
    with database.begin() as session:
        session.get(SourceDiscovery, UUID(checked["discovery_id"])).expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    assert connect(api_client, checked).status_code == 404
    one = add(api_client)
    two = add(api_client, headers, token="second-identity-not-real")
    for source, request_headers, expected in [
        (one, None, TOKEN),
        (two, headers, "second-identity-not-real"),
    ]:
        assert (
            api_client.post(
                f"/api/platforms/{source['id']}/query",
                headers=request_headers,
                json={"operation": "list"},
            ).status_code
            == 200
        )
        assert network.calls[-1][1]["Authorization"] == "Bearer " + expected
    assert (
        api_client.post(
            f"/api/platforms/{one['id']}/query", headers=headers, json={"operation": "list"}
        ).status_code
        == 404
    )


@pytest.mark.integration
def test_validation_errors_never_echo_secret_inputs(network, database, api_client):
    draft = detect(api_client)
    for payload in [
        {"discovery_id": "invalid", "credentials": {"token": TOKEN}},
        {"discovery_id": draft["discovery_id"], "unexpected": TOKEN},
        {"discovery_id": draft["discovery_id"], "credentials": {"token": {"nested": TOKEN}}},
        {
            "discovery_id": draft["discovery_id"],
            "credentials": {"token": TOKEN},
            "credential_action": "keep",
        },
    ]:
        result = api_client.post("/api/sources/verify-platform", json=payload)
        assert (
            result.status_code == 422 and TOKEN not in result.text and '"input"' not in result.text
        )
    assert network.calls == []
    with database() as session:
        assert session.scalar(select(func.count()).select_from(ContentItem)) == 0
