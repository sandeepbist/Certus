FROM python:3.12-slim

WORKDIR /app

COPY services/ingestion/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock

COPY services/ingestion/app/ ./app/
COPY services/shared/ ./services/shared/

RUN groupadd --gid 10001 certus \
    && useradd --uid 10001 --gid certus --create-home --no-log-init certus \
    && chmod -R a=rX /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    NODE_ENV=production \
    PORT=8001

EXPOSE 8001

USER certus
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-server-header"]
