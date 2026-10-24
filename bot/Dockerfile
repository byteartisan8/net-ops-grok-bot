# grok-bot image (owner: 07-grok-bot): the NOC office and its web page (FastAPI on :8095), the approvals page for
# the operator's phone (:8097); also the routine sink (grok-bot-sink) of the tests. Build context: the repository
# root; bot/Dockerfile.dockerignore keeps it to what the image needs.
#
# Same Python base and uv as the device and lab-api images. Only netsim-bot is installed (it talks HTTP to lab-api
# and the roles' routines and imports no other workspace package), but uv needs every workspace member's
# pyproject.toml to resolve the lock. bot/compose.yaml mounts the repository read-only so the bot reads the live .env
# (a routine URL or key added later is used without a restart).
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie

FROM ${PYTHON_IMAGE} AS python
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY lib/netsim_common/pyproject.toml lib/netsim_common/
COPY devices/simd/pyproject.toml devices/simd/
COPY netbox/tools/pyproject.toml netbox/tools/
COPY zabbix/tools/pyproject.toml zabbix/tools/
COPY lab/pyproject.toml lab/
COPY bot/pyproject.toml bot/README.md bot/
RUN uv sync --frozen --no-dev --no-install-workspace --package netsim-bot
COPY bot/src bot/src
RUN uv sync --frozen --no-dev --no-editable --package netsim-bot

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="netsim-grok-bot" \
      org.opencontainers.image.description="NetSim NOC office: Zabbix customer alerts, three NOC roles on MCP, approved changes, NOC room" \
      org.opencontainers.image.version="0.2.0"
COPY --from=python /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:${PATH} \
    BOT_DATA_DIR=/var/lib/grok-bot \
    PYTHONUNBUFFERED=1
EXPOSE 8095/tcp 8097/tcp
VOLUME ["/var/lib/grok-bot"]
HEALTHCHECK --interval=5s --timeout=4s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8095/api/health', headers={'Host': '127.0.0.1'}), timeout=3)"]
ENTRYPOINT ["grok-bot"]
