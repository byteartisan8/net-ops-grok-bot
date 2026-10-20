# lab-api image (owner: 05-integration): the lab's tool surface for the bot (FastAPI on :8090).
# Build context: the repository root; lab/Dockerfile.dockerignore keeps it to what the image needs.
#
# Same Python base and uv as the device image (02). The workspace packages are installed as wheels into /opt/venv:
# netsim-common, netsim-netbox-tools, netsim-zabbix-tools and netsim-lab. The contract files and scenarios are
# copied too, so the image runs on its own; lab/compose.yaml mounts the repository read-only instead, so that
# lab-api reads the live .env (tokens written later by the bootstraps) and the current scenario files.
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
COPY lib/netsim_common/pyproject.toml lib/netsim_common/README.md lib/netsim_common/
COPY devices/simd/pyproject.toml devices/simd/README.md devices/simd/
COPY netbox/tools/pyproject.toml netbox/tools/README.md netbox/tools/
COPY zabbix/tools/pyproject.toml zabbix/tools/README.md zabbix/tools/
COPY lab/pyproject.toml lab/README.md lab/
RUN uv sync --frozen --no-dev --no-install-workspace --package netsim-lab
COPY lib/netsim_common/src lib/netsim_common/src
COPY netbox/tools/src netbox/tools/src
COPY zabbix/tools/src zabbix/tools/src
COPY lab/src lab/src
RUN uv sync --frozen --no-dev --no-editable --package netsim-lab

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="netsim-lab-api" \
      org.opencontainers.image.description="NetSim lab-api: intent, live state, Zabbix, drift, faults, scenarios, verified changes" \
      org.opencontainers.image.version="0.1.0"
COPY --from=python /opt/venv /opt/venv
COPY contract/topology.yaml contract/bsm-spec.schema.json /opt/netsim/contract/
COPY contract/golden /opt/netsim/contract/golden
COPY lab/scenarios /opt/netsim/lab/scenarios
ENV PATH=/opt/venv/bin:${PATH} \
    NETSIM_CONTRACT_DIR=/opt/netsim/contract \
    NETSIM_SCENARIOS_DIR=/opt/netsim/lab/scenarios \
    NETSIM_LAB_NETWORK=labnet \
    LAB_DATA_DIR=/var/lib/lab-api \
    PYTHONUNBUFFERED=1
EXPOSE 8090/tcp
VOLUME ["/var/lib/lab-api"]
HEALTHCHECK --interval=5s --timeout=4s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)"]
ENTRYPOINT ["lab-api"]
