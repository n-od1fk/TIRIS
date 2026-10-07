from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


def build_database_url() -> URL:
    """Build a URL without manually interpolating or escaping the password."""

    settings = get_settings()
    return URL.create(
        drivername="postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


engine = create_engine(
    build_database_url(),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_timeout=5,
    connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_session():
    with SessionLocal() as session:
        yield session


def get_database_engine() -> Engine:
    """FastAPI dependency that exposes the process-wide SQLAlchemy engine."""

    return engine


def check_database(database_engine: Engine) -> bool:
    """Borrow one pooled connection and verify PostgreSQL can execute a query."""

    with database_engine.connect() as connection:
        return connection.execute(text("SELECT 1")).scalar_one() == 1
