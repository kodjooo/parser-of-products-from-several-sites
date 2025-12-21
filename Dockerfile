# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        fonts-liberation \
        fonts-unifont \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libgtk-3-0 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && if [ -s requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi \
    && playwright install chromium

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests

CMD ["python", "-m", "app.main"]
