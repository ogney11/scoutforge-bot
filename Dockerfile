# syntax=docker/dockerfile:1
#
# ScoutForge runtime image.
#
# One image, two roles - the command selects the role:
#   scoutforge run        bot + scheduler
#   scoutforge worker     headless discovery (no Discord connection)
#
# Dev/test dependencies are deliberately NOT shipped; install .[dev] locally to
# run pytest, ruff and mypy.

# ---------------------------------------------------------------------------
# Builder: resolve and install dependencies once, without the test tooling.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Everything ships as a wheel: asyncmy is pure Python and cryptography bundles
# its own Rust extension, so no compiler is needed at runtime or build time.

WORKDIR /build

# Install dependencies from pyproject.toml alone. A stub package is enough for
# setuptools to resolve metadata, so this layer survives source-only edits.
COPY pyproject.toml README.md ./
RUN mkdir -p scoutforge \
    && printf '"""Build-time stub; replaced by the real package below."""\n' > scoutforge/__init__.py \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install .

# Now the real source, reinstalled without touching dependency resolution.
COPY scoutforge ./scoutforge
COPY alembic.ini ./
RUN /opt/venv/bin/pip install --no-deps --force-reinstall .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="ScoutForge" \
      org.opencontainers.image.description="Roblox game scouting intelligence for Discord" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 scoutforge

COPY --from=builder /opt/venv /opt/venv

# alembic.ini is read at runtime by `scoutforge migrate`.
WORKDIR /app
COPY --chown=scoutforge:scoutforge alembic.ini ./

USER scoutforge

# The bot process is PID 1 (via tini), so a signal probe is the honest liveness
# check here; there is no HTTP port to query.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD kill -0 1

ENTRYPOINT ["/usr/bin/tini", "--", "scoutforge"]
CMD ["run"]