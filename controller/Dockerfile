# syntax=docker/dockerfile:1
# controller: the Clixon controller in front of the ten PEs (owner: 10-clixon-controller). Build context = repository
# root:
#
#   docker build -f controller/Dockerfile -t netsim/clixon-controller:1.9.0 .     (or: make controller-build)
#
# Stages: CLIgen, Clixon and the Clixon controller built from pinned release tags (commits checked, as in
# devices/Dockerfile), then a runtime on the same Python base image as the other lab images (Python only runs
# supervisord). Southbound: NETCONF over SSH to the PEs (port 830). Northbound: only the controller CLI, which
# lab-api reaches over SSH on labnet (port 22) as the role users and as itself. See controller/README.md.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie

# ---------------------------------------------------------------------------------------- build
FROM ${PYTHON_IMAGE} AS build
# Controller 1.9.0 (2026-10-15) is the newest release. It builds (-Werror) and runs on the same Clixon/CLIgen 7.9.0
# commits as the PEs (devices/Dockerfile), so the whole lab runs one Clixon release.
ARG CLIGEN_VERSION=7.9.0
ARG CLIGEN_COMMIT=9fd27a8e85eae688f2eaf1df3fb6500e05f72583
ARG CLIXON_VERSION=7.9.0
ARG CLIXON_COMMIT=98178407d162e63542ce563aa284c6193c1fead5
ARG CONTROLLER_VERSION=1.9.0
ARG CONTROLLER_COMMIT=53631aae42605f7f49aa3df7e1281775656ec3ad
# openssh-client: the controller's configure records the ssh binary it runs to reach the devices (SSH_BIN).
RUN apt-get update \
 && apt-get install -y --no-install-recommends bison build-essential ca-certificates flex git libssl-dev \
      openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system clicon \
 && useradd --system --gid clicon --no-create-home --shell /usr/sbin/nologin clicon
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
# .version: the controller's version string (CLI "show version") names the release commit, not just the tag.
RUN git clone --depth 1 --branch "${CONTROLLER_VERSION}" https://github.com/clicon/clixon-controller.git \
      /src/controller \
 && test "$(git -C /src/controller rev-parse HEAD)" = "${CONTROLLER_COMMIT}" \
 && cd /src/controller \
 && git describe --long > .version \
 && ./configure --prefix=/opt/clixon --sysconfdir=/etc --localstatedir=/var/lib --runstatedir=/run \
      --with-cligen=/opt/clixon --with-clixon=/opt/clixon --with-clicon-user=clicon --with-clicon-group=clicon \
 && make -j"$(nproc)" && make install

# ---------------------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
ARG CONTROLLER_VERSION=1.9.0
LABEL org.opencontainers.image.title="netsim-clixon-controller" \
      org.opencontainers.image.description="NetSim lab: Clixon controller managing the PEs over NETCONF/SSH" \
      org.opencontainers.image.version="${CONTROLLER_VERSION}"
# supervisor: the PEs' version (uv.lock), the same pure-Python wheel, checked against uv.lock's hash.
# Users: the three NOC roles (read only), lab-api (pulls) and admin (people, `make controller-shell`); their login
# shell is the controller CLI. "*" = no password but not locked (sshd refuses locked accounts even for keys).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libssl3t64 openssh-client openssh-server \
 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
 && printf '%s\n' \
      'supervisor==4.3.0 --hash=sha256:0bcb763fddafba410f35cbde226aa7f8514b9fb82eb05a0c85f6588d1c13f8db' \
      > /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt \
 && groupadd --system clicon \
 && useradd --system --gid clicon --no-create-home --shell /usr/sbin/nologin clicon \
 && echo /usr/local/bin/controller-cli >> /etc/shells \
 && for user in noc-engineer network-engineer noc-manager lab-api admin; do \
      useradd --create-home --user-group --groups clicon --shell /usr/local/bin/controller-cli --password '*' "$user"; \
    done \
 && install -d -m 0755 /etc/netsim/keys
COPY --from=build /opt/clixon /opt/clixon
COPY --from=build /etc/clixon /etc/clixon
RUN echo /opt/clixon/lib > /etc/ld.so.conf.d/clixon.conf && ldconfig
COPY controller/clixon/netsim.xml /etc/clixon/controller/netsim.xml
COPY controller/startup_db /opt/netsim/controller/startup_db
COPY controller/sshd_config /etc/ssh/sshd_config
COPY controller/ssh_config /etc/ssh/ssh_config.d/netsim.conf
COPY controller/supervisord.conf /etc/supervisor/supervisord.conf
COPY --chmod=0755 controller/bin/ /usr/local/bin/
COPY --chmod=0755 controller/agent.py /usr/local/bin/controller-agent
COPY --chmod=0755 controller/healthcheck.py /usr/local/bin/controller-healthcheck
ENV PATH=/opt/clixon/bin:/opt/clixon/sbin:${PATH} \
    CONTROLLER_VERSION=${CONTROLLER_VERSION} \
    PYTHONUNBUFFERED=1
EXPOSE 22/tcp
VOLUME ["/var/lib/controller"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=5s --timeout=4s --start-period=60s --retries=3 CMD ["controller-healthcheck"]
ENTRYPOINT ["controller-entrypoint"]
