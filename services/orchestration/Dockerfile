FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/orchestration/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/orchestration/app/ ./app/
COPY services/shared/ ./services/shared/

RUN groupadd --gid 10001 certus \
    && useradd --uid 10001 --gid certus --create-home --no-log-init certus \
    && chmod -R a=rX /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8002

EXPOSE 8002

USER certus
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
