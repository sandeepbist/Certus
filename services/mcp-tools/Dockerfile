FROM python:3.12.16-slim@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07

WORKDIR /app

COPY services/mcp-tools/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock

COPY services/mcp-tools/app/ ./app/
COPY services/shared/ ./services/shared/

RUN groupadd --gid 10001 certus \
    && useradd --uid 10001 --gid certus --create-home --no-log-init certus \
    && chmod -R a=rX /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    NODE_ENV=production \
    PORT=8003

EXPOSE 8003

USER certus
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8003", "--no-server-header"]
