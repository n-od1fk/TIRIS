import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.api import router
from app.config import get_settings
from app.connectors.source_api import router as source_onboarding_router
from app.database import check_database, engine, get_database_engine
from app.discovery.api import router as discovery_router
from app.discovery.feed import router as feed_router
from app.identities import router as identities_router
from app.platforms.api import router as platform_router
from app.profiles.api import router as profile_router
from app.retrieval.api import router as retrieval_router

logger = logging.getLogger(__name__)
settings = get_settings()


class ApplicationHealth(BaseModel):
    status: Literal["ok"]


class DatabaseHealth(BaseModel):
    status: Literal["ok"]
    database: Literal["reachable"]


class DatabaseUnavailable(BaseModel):
    status: Literal["error"]
    database: Literal["unreachable"]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(profile_router)
app.include_router(retrieval_router)
app.include_router(discovery_router)
app.include_router(identities_router)
app.include_router(feed_router)
app.include_router(platform_router)
app.include_router(source_onboarding_router)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    # FastAPI's default includes raw inputs. Never echo submitted credentials in 422s.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": list(error["loc"]), "type": error["type"], "msg": "请求字段格式不正确"}
                for error in exc.errors()
            ]
        },
    )


@app.get("/health", response_model=ApplicationHealth)
def application_health() -> ApplicationHealth:
    """Report API process health without touching external dependencies."""

    return ApplicationHealth(status="ok")


@app.get(
    "/health/db",
    response_model=DatabaseHealth,
    responses={503: {"model": DatabaseUnavailable}},
)
def database_health(
    database_engine: Annotated[Engine, Depends(get_database_engine)],
) -> DatabaseHealth | JSONResponse:
    """Report whether the API can run a minimal PostgreSQL query."""

    try:
        if check_database(database_engine):
            return DatabaseHealth(status="ok", database="reachable")
    except SQLAlchemyError:
        logger.warning("Database health check failed", exc_info=True)

    return JSONResponse(
        status_code=503,
        content={"status": "error", "database": "unreachable"},
    )
