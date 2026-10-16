# API and trading loop (python -m bot). Runtime data lives in the /data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOT_RUNTIME_ROOT=/data \
    BOT_API_HOST=0.0.0.0

WORKDIR /app
COPY pyproject.toml README.md ./
COPY bot ./bot
RUN pip install --no-cache-dir . \
    && useradd --uid 1000 --create-home bot \
    && mkdir -p /data && chown bot:bot /data
# docker-compose.yml runs this as BOT_UID:BOT_GID, the owner of the mounted /data.

USER bot
EXPOSE 8000
CMD ["python", "-m", "bot"]
