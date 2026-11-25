# syntax=docker/dockerfile:1.7
# Central API / scheduler image. Optional build secret `extra_ca` lets the build
# run behind a TLS-intercepting corporate proxy (not used at runtime).

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

FROM base AS builder
WORKDIR /build
COPY requirements.txt requirements-dev.txt ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --prefix=/install -r requirements.txt && \
    pip install --prefix=/install-dev -r requirements-dev.txt

FROM base AS runtime
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /certs && chown app:app /certs
COPY --from=builder /install /usr/local
WORKDIR /app
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-server-header"]

# Test image: runtime + dev dependencies + tests (docker compose --profile test)
FROM runtime AS test
USER root
COPY --from=builder /install-dev /usr/local
COPY --chown=app:app pytest.ini ruff.toml ./
COPY --chown=app:app tests ./tests
USER app
CMD ["pytest", "-q"]
