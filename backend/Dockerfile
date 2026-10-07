FROM python:3.11-slim-bookworm@sha256:2333bd330d12de02514770b3585cad313644316047cdee24a7acfdece6de6efb
COPY --from=ghcr.io/astral-sh/uv:0.12.16@sha256:adc68cd785ca65ea25c0611043b0a00b4ea3a22e1b54102fc084406d888082ee /uv /usr/local/bin/uv
WORKDIR /app/backend
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra local-embedding --python /usr/local/bin/python
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
ENV PATH="/app/backend/.venv/bin:$PATH" PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 tiris && mkdir -p /models && chown tiris:tiris /models
USER tiris
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
