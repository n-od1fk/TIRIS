from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_database_engine
from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_application_health_does_not_require_database(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_database_health_reports_reachable(client: TestClient) -> None:
    database_engine = MagicMock(spec=Engine)
    connection = database_engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = 1
    app.dependency_overrides[get_database_engine] = lambda: database_engine

    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "reachable"}


def test_database_health_returns_503_when_database_is_unavailable(
    client: TestClient,
) -> None:
    database_engine = MagicMock(spec=Engine)
    database_engine.connect.side_effect = SQLAlchemyError("database unavailable")
    app.dependency_overrides[get_database_engine] = lambda: database_engine

    response = client.get("/health/db")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unreachable"}
