# Minimal Clixon (backend + NETCONF, no RESTCONF) to check that contract/yang loads and validates in Clixon.
# Used by tools/clixon-check.sh (`make yang-clixon`). The lab's real device image is owned by 02-device-simulator.
ARG ALPINE_VERSION=3.24.2

FROM alpine:${ALPINE_VERSION}
ARG CLIGEN_VERSION=7.9.0
ARG CLIGEN_COMMIT=9fd27a8e85eae688f2eaf1df3fb6500e05f72583
ARG CLIXON_VERSION=7.9.0
ARG CLIXON_COMMIT=98178407d162e63542ce563aa284c6193c1fead5
RUN apk add --no-cache build-base flex bison git openssl-dev python3 py3-yaml
RUN git clone --depth 1 --branch "${CLIGEN_VERSION}" https://github.com/clicon/cligen.git /src/cligen \
 && test "$(git -C /src/cligen rev-parse HEAD)" = "${CLIGEN_COMMIT}" \
 && cd /src/cligen && ./configure --prefix=/usr/local && make -j"$(nproc)" && make install
RUN git clone --depth 1 --branch "${CLIXON_VERSION}" https://github.com/clicon/clixon.git /src/clixon \
 && test "$(git -C /src/clixon rev-parse HEAD)" = "${CLIXON_COMMIT}" \
 && cd /src/clixon && ./configure --prefix=/usr/local --sysconfdir=/etc --without-restconf \
 && make -j"$(nproc)" && make install
RUN addgroup -S clicon && (clixon_backend -V 2>&1 || true) | grep "Clixon version: ${CLIXON_VERSION}"
