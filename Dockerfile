# syntax=docker/dockerfile:1

# ---- Stage 1: build the web UI ----
FROM node:22-alpine AS ui-builder
WORKDIR /src
COPY ui/package.json ui/package-lock.json ./ui/
RUN cd ui && npm ci
COPY ui ./ui
# vite.config.ts outputs to ../backend/finmeta/web relative to ui/
RUN mkdir -p backend/finmeta/web && cd ui && npm run build

# ---- Stage 2: Python runtime ----
FROM python:3.12-slim AS final

LABEL org.opencontainers.image.title="Finmeta" \
      org.opencontainers.image.description="Jellyfin-native metadata, artwork and overlay manager (Kometa-equivalent)" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/Virtext/finmeta"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FINMETA_CONFIG=/config/config.yml \
    FINMETA_HOST=0.0.0.0 \
    FINMETA_PORT=8765

# Fixed non-root user. Override at run time with `docker run --user UID:GID`
# or compose's `user:` key if your media/config directories are owned by a
# different uid/gid (e.g. 99:100 on Unraid) -- see README.
RUN groupadd --gid 1000 finmeta \
 && useradd --uid 1000 --gid finmeta --no-create-home --shell /usr/sbin/nologin finmeta

WORKDIR /app

COPY pyproject.toml ./
COPY backend ./backend
# Editable install (-e), not a regular one: api/app.py resolves the built UI's
# location via `Path(__file__)...` relative to the package itself (D1: resolved,
# not hardcoded to a path). A regular `pip install .` copies the package into
# site-packages, so __file__ no longer points at /app/backend/finmeta -- the
# web/ directory COPY'd in below (built separately by the UI stage) then sits
# next to a copy of the source nobody imports, and _web_dir() finds nothing,
# silently 404ing every UI route while /api/* keeps working (the bug this
# fixes: caught by actually building and running this Dockerfile end-to-end
# against a fresh clone of the public repo, not just checking /api/health).
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -e .

COPY --from=ui-builder /src/backend/finmeta/web ./backend/finmeta/web
COPY config/config.template.yml ./config.template.yml
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /config \
 && chown -R finmeta:finmeta /app /config

USER finmeta
WORKDIR /app
VOLUME ["/config"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('FINMETA_PORT', '8765') + '/api/health', timeout=3)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "finmeta.api.server"]
