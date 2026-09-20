FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

WORKDIR /app

COPY services/orchestration/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock

COPY services/orchestration/app/ ./app/
COPY services/shared/ ./services/shared/

RUN groupadd --gid 10001 certus \
    && useradd --uid 10001 --gid certus --create-home --no-log-init certus \
    && chmod -R a=rX /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    NODE_ENV=production \
    PORT=8002

EXPOSE 8002

USER certus
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002", "--no-server-header"]
