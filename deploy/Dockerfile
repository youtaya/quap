ARG LOCAL_RUNTIME_IMAGE=quant-platform-native:test
FROM ghcr.io/astral-sh/uv:0.9.5 AS uv
FROM postgres:16.10-bookworm AS pgtools
FROM python:3.11.14-slim-bookworm AS native
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=pgtools /usr/lib/postgresql/16/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=pgtools /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/pg_restore
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 liblz4-1 libzstd1 libgomp1 && apt-get clean
WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src /app/src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable && useradd --uid 10001 --create-home quant && \
    mkdir -p /data/artifacts /data/observations /backups && chown -R quant:quant /data /backups
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 QUANT_ARTIFACT_ROOT=/data/artifacts QUANT_BACKUP_ROOT=/backups HOME=/tmp XDG_CACHE_HOME=/tmp/cache
USER quant
ENTRYPOINT ["quant-platform"]
CMD ["api", "--host", "0.0.0.0"]

FROM native AS research-data
USER root
RUN --mount=type=cache,target=/tmp/cache/uv uv sync --frozen --no-dev --no-editable --extra research-data
USER quant
CMD ["worker", "--role", "research-data"]

FROM native AS qlib
USER root
RUN --mount=type=cache,target=/tmp/cache/uv uv sync --frozen --no-dev --no-editable --extra qlib
USER quant
CMD ["worker", "--role", "qlib-daily"]

# Offline application-only update; the supplied runtime must match uv.lock.
# Build the wheel first and provide --build-context wheelhouse=./dist.
FROM ${LOCAL_RUNTIME_IMAGE} AS local-native
USER root
COPY --from=wheelhouse / /tmp/wheelhouse/
RUN uv --no-cache pip install --offline --no-deps --reinstall --python /app/.venv/bin/python /tmp/wheelhouse/*.whl && \
    mkdir -p /data/observations && chown quant:quant /data/observations
USER quant
CMD ["api", "--host", "0.0.0.0"]
