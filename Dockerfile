FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Makassar

WORKDIR /app

# Dependencies first (cached layer), then the project itself
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra gcp --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --extra gcp

ENV PATH="/app/.venv/bin:$PATH"

# The image tag (deploy/release.sh), recorded in the landing manifests and the run ledger
ARG RELEASE=""
ENV RELEASE=$RELEASE

# One job for everything: Cloud Scheduler starts it with `tick --scheduler` (the daily tick),
# the release's dry run with `tick --dry-run --all-scheduled`. hari-lari-pipeline hands every
# other command (`run`, `validate`, ...) to hari-lari-report, which a Cloud Run job cannot swap
# for another program.
ENTRYPOINT ["hari-lari-pipeline"]
