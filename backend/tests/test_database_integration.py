import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.integration
def test_database_health_with_local_postgres() -> None:
    with TestClient(app) as client:
        response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "reachable"}
