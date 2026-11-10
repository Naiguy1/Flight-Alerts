# Official multi-architecture manifest verified in docker-library/repo-info.
FROM python:3.12-alpine@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7 AS dependencies
WORKDIR /build
COPY requirements.txt requirements-scraper.txt ./
ARG INSTALL_SCRAPER=false
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && if [ "$INSTALL_SCRAPER" = "true" ]; then /opt/venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements-scraper.txt; fi \
    && /opt/venv/bin/python -m pip check

FROM python:3.12-alpine@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////app/data/flight_deals.db \
    CONFIG_PATH=/app/config.yaml \
    WORKER_HEARTBEAT_PATH=/app/data/worker-heartbeat.json
# The Python runtime pulls Kerberos transitively through libnsl/libtirpc.
# Require Alpine's security fix even if the pinned base predates that package.
RUN apk add --no-cache --upgrade ca-certificates tzdata 'krb5-libs>=1.22.3-r0' \
    && addgroup -S -g 10001 flight \
    && adduser -S -D -H -u 10001 -G flight flight \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data \
    && chmod 0700 /app/data
COPY --from=dependencies /opt/venv /opt/venv
# Keep secrets, local state, tests, and unrelated files out of application layers.
COPY --chown=10001:10001 analyzer/ analyzer/
COPY --chown=10001:10001 db/ db/
COPY --chown=10001:10001 fetcher/ fetcher/
COPY --chown=10001:10001 notifier/ notifier/
COPY --chown=10001:10001 services/ services/
COPY --chown=10001:10001 web/ web/
COPY --chown=10001:10001 migrations/ migrations/
COPY --chown=10001:10001 tools/ tools/
COPY --chown=10001:10001 main.py dashboard.py settings.py config.yaml ./
# Installation is finished. Remove both complete installer distributions,
# including their vulnerable vendored code; retain all application metadata.
RUN /usr/local/bin/python -m pip uninstall --yes pip \
    && /opt/venv/bin/python -m pip uninstall --yes pip
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "tools.healthcheck", "worker"]
CMD ["python", "main.py"]
