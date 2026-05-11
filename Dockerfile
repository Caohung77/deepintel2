# syntax=docker/dockerfile:1.6

# Use Playwright official image: bundles Chromium + all OS deps + Python 3.11.
# Saves ~5 min of crawl4ai-setup install pain.
FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEEPINTEL_JOBS_DIR=/data/jobs \
    DEEPINTEL_CACHE_DIR=/data/cache

WORKDIR /app

# Python deps first for layer cache friendliness
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install -r /app/requirements.txt

# Initialise crawl4ai DB and (re-)check Playwright browsers
RUN python -c "import crawl4ai" && crawl4ai-setup || true

COPY app.py worker.py pipeline.py fast_extractor.py company_extractor.py /app/
COPY enrichment /app/enrichment
COPY synthesis  /app/synthesis
COPY api        /app/api
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Persistent state
RUN mkdir -p /data/jobs /data/cache && \
    ln -sf /data/cache /app/cache

EXPOSE 8501 8000

ENTRYPOINT ["/app/entrypoint.sh"]
