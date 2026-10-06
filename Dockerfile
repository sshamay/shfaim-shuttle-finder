# Production image for the Shfaim Shuttle Finder.
# Non-root, minimal surface, no CDN dependency, runs on any container host
# (Railway, Fly.io, a VPS, Kubernetes). The stops cache is fetched from the
# live API on boot (or reused if still fresh), so the build never breaks on a
# 20fl outage and restarting always refreshes data.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /srv/shfaim

# Dependencies first so this layer is cached across edits to app code.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
COPY static ./static

RUN mkdir -p data \
    && useradd --system --uid 10001 appuser \
    && chown -R appuser:appuser /srv/shfaim

USER appuser

EXPOSE 8000

COPY --chown=appuser:appuser docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]