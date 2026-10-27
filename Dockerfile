# Official multi-architecture manifest verified in docker-library/repo-info.
FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f AS dependencies
WORKDIR /build
COPY requirements.txt requirements-scraper.txt ./
ARG INSTALL_SCRAPER=false
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && if [ "$INSTALL_SCRAPER" = "true" ]; then /opt/venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements-scraper.txt; fi

FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////app/data/flight_deals.db \
    CONFIG_PATH=/app/config.yaml \
    WORKER_HEARTBEAT_PATH=/app/data/worker-heartbeat.json
RUN groupadd --gid 10001 flight \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin flight \
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
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "tools.healthcheck", "worker"]
CMD ["python", "main.py"]
