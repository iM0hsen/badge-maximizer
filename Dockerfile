FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY games.py stream.py web_backend.py server.py ./
COPY webui ./webui

RUN useradd --system --uid 10001 --create-home badge \
    && mkdir -p /data \
    && chown -R badge:badge /data /app

USER badge

ENV BADGE_DATA_DIR=/data \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4)"

CMD ["python", "server.py"]
