# ─────────────────────────────────────────────────────────────────────────────
# AI QA Engineer — control plane image.
#
# Includes Node + Playwright browsers because the Execution and Exploration
# agents drive a real browser against the application under test. Running as a
# non-root user matters here: this container executes commands on behalf of an
# agent, and the command allowlist should not be the only thing standing between
# a bug and the host.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_MAJOR=20 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ---- system deps + Node ------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git tini \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update && apt-get install -y --no-install-recommends nodejs \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# ---- python deps (own layer: changes far less often than source) -------------
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "psycopg[binary]>=3.1"

# ---- Playwright browsers -----------------------------------------------------
RUN npx --yes playwright@1.47.0 install --with-deps chromium \
 && rm -rf /root/.npm

# ---- application -------------------------------------------------------------
COPY configs/ ./configs/
COPY packages/ ./packages/
COPY services/ ./services/
COPY agents/ ./agents/
COPY tools/ ./tools/
COPY scripts/ ./scripts/
COPY knowledge/ ./knowledge/
COPY apps/control-plane/ ./apps/control-plane/
COPY pyproject.toml README.md ./

# ---- non-root ----------------------------------------------------------------
RUN useradd --create-home --uid 10001 aiqa \
 && mkdir -p /app/artifacts /workspaces \
 && chown -R aiqa:aiqa /app /workspaces /ms-playwright
USER aiqa

ENV AIQA_HOST=0.0.0.0 \
    AIQA_PORT=8080 \
    AIQA_ARTIFACTS_DIR=/app/artifacts \
    PYTHONPATH=/app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "services.api_gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
