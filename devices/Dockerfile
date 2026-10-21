# syntax=docker/dockerfile:1
# netsim-pe: one simulated PE router (owner: 02-device-simulator). Build context = repository root:
#
#   docker build -f devices/Dockerfile -t netsim/netsim-pe:0.1.0 .     (or: make devices-build)
#
# Stages: Clixon/CLIgen built from pinned release tags (+ our two Clixon plugins), the Python venv from
# uv.lock (simd, snmpsim, pysnmp, supervisor), then a lean runtime on the same Python base image.
# Configured only by env: HOSTNAME, MGMT_IP, CORE_URL, TRAP_TARGETS, SNMP_COMMUNITY (+ SITE); see devices/README.md.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie

# ---------------------------------------------------------------------------------------- Clixon build
FROM ${PYTHON_IMAGE} AS clixon
ARG CLIGEN_VERSION=7.9.0
ARG CLIGEN_COMMIT=9fd27a8e85eae688f2eaf1df3fb6500e05f72583
ARG CLIXON_VERSION=7.9.0
ARG CLIXON_COMMIT=98178407d162e63542ce563aa284c6193c1fead5
RUN apt-get update \
 && apt-get install -y --no-install-recommends bison build-essential ca-certificates flex git libssl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${CLIGEN_VERSION}" https://github.com/clicon/cligen.git /src/cligen \
 && test "$(git -C /src/cligen rev-parse HEAD)" = "${CLIGEN_COMMIT}" \
 && cd /src/cligen && ./configure --prefix=/opt/clixon \
 && make -j"$(nproc)" && make install
RUN git clone --depth 1 --branch "${CLIXON_VERSION}" https://github.com/clicon/clixon.git /src/clixon \
 && test "$(git -C /src/clixon rev-parse HEAD)" = "${CLIXON_COMMIT}" \
 && cd /src/clixon \
 && ./configure --prefix=/opt/clixon --sysconfdir=/etc --with-cligen=/opt/clixon \
      --with-restconf=native --disable-nghttp2 \
 && make -j"$(nproc)" && make install && make install-include
COPY devices/clixon/netsim_backend.c devices/clixon/netsim_cli.c /src/netsim/
RUN cd /src/netsim \
 && gcc -O2 -Wall -Werror -fPIC -shared -I/opt/clixon/include -o netsim_backend.so netsim_backend.c \
      -L/opt/clixon/lib -lclixon -lclixon_backend -lcligen \
 && gcc -O2 -Wall -Werror -fPIC -shared -I/opt/clixon/include -o netsim_cli.so netsim_cli.c \
      -L/opt/clixon/lib -lclixon -lclixon_cli -lcligen

# ---------------------------------------------------------------------------------------- Python venv
FROM ${PYTHON_IMAGE} AS python
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Workspace metadata first (every member's pyproject.toml), so the dependency layer is cached.
COPY pyproject.toml uv.lock ./
COPY lib/netsim_common/pyproject.toml lib/netsim_common/README.md lib/netsim_common/
COPY devices/simd/pyproject.toml devices/simd/README.md devices/simd/
COPY netbox/tools/pyproject.toml netbox/tools/
COPY zabbix/tools/pyproject.toml zabbix/tools/
COPY lab/pyproject.toml lab/
COPY bot/pyproject.toml bot/
RUN uv sync --frozen --no-dev --no-install-workspace --package netsim-simd --extra image
COPY lib/netsim_common/src lib/netsim_common/src
COPY devices/simd/src devices/simd/src
RUN uv sync --frozen --no-dev --no-editable --package netsim-simd --extra image

# ---------------------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
ARG CLIXON_VERSION=7.9.0
LABEL org.opencontainers.image.title="netsim-pe" \
      org.opencontainers.image.description="NetSim simulated PE router: Clixon RESTCONF/CLI, simd, snmpsim" \
      org.opencontainers.image.version="0.1.0"
# libssl: Clixon; snmp: net-snmp client tools (snmpwalk/snmpget) for engineers inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libssl3t64 snmp \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system clicon \
 && useradd --system --user-group --no-create-home --shell /usr/sbin/nologin snmpsim \
 && usermod --append --groups clicon www-data \
 && mkdir -p /var/lib/snmp/cert_indexes
COPY --from=clixon /opt/clixon /opt/clixon
RUN echo /opt/clixon/lib > /etc/ld.so.conf.d/clixon.conf && ldconfig
COPY --from=python /opt/venv /opt/venv
COPY contract/topology.yaml contract/oids.yaml /opt/netsim/contract/
COPY contract/golden /opt/netsim/contract/golden
COPY contract/yang/netsim-device.yang devices/clixon/yang/ietf-netconf-notifications@2012-02-06.yang /opt/netsim/yang/main/
COPY devices/clixon/clixon.xml /etc/clixon.xml
COPY devices/clixon/netsim.cli /opt/netsim/clixon/clispec/netsim.cli
COPY --from=clixon /src/netsim/netsim_backend.so /opt/netsim/clixon/backend/
COPY --from=clixon /src/netsim/netsim_cli.so /opt/netsim/clixon/cli/
COPY devices/snmpsim/variation/netsim.py /opt/netsim/snmpsim/variation/netsim.py
COPY devices/supervisord.conf /etc/supervisor/supervisord.conf
COPY --chmod=0755 devices/entrypoint.sh /usr/local/bin/netsim-entrypoint
ENV PATH=/opt/venv/bin:/opt/clixon/bin:/opt/clixon/sbin:${PATH} \
    NETSIM_CONTRACT_DIR=/opt/netsim/contract \
    CLIXON_VERSION=${CLIXON_VERSION} \
    PYTHONUNBUFFERED=1
EXPOSE 8080/tcp 9000/tcp 161/udp
VOLUME ["/var/lib/netsim"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=5s --timeout=4s --start-period=60s --retries=3 CMD ["netsim-healthcheck"]
ENTRYPOINT ["netsim-entrypoint"]
