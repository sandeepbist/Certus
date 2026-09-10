FROM python:3.12-slim

WORKDIR /app

COPY services/mcp-tools/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/mcp-tools/app/ ./app/

RUN groupadd --gid 10001 certus \
    && useradd --uid 10001 --gid certus --create-home --no-log-init certus \
    && chmod -R a=rX /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8003

EXPOSE 8003

USER certus
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8003"]
