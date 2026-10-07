from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import engine, get_session
from app.main import app


@pytest.fixture
def api_client(database):
    def session_dependency():
        with database() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def database(monkeypatch):
    """Migrate an isolated random schema; never truncate/drop real application tables."""
    from app.workers import tasks

    schema = f"tiris_test_{uuid4().hex}"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    try:
        with engine.connect() as connection:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config.attributes.update(connection=connection, test_schema=schema)
            command.upgrade(config, "head")
            connection.execute(text("SET search_path TO public"))
            connection.commit()
        test_engine = engine.execution_options(schema_translate_map={None: schema})
        factory = sessionmaker(bind=test_engine, expire_on_commit=False)
        monkeypatch.setattr(tasks, "SessionLocal", factory)
        from app.retrieval import service

        monkeypatch.setattr(service, "SessionLocal", factory)
        from app.discovery import service as discovery_service

        monkeypatch.setattr(discovery_service, "SessionLocal", factory)
        yield factory
    finally:
        # The exact schema was generated above and holds only this test's data.
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
