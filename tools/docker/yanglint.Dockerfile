# yanglint from a pinned libyang release (used by tools/yanglint.sh when no native yanglint is wanted).
#   docker build -t netsim/yanglint:6.4.13 -f tools/docker/yanglint.Dockerfile tools/docker
ARG ALPINE_VERSION=3.24.2

FROM alpine:${ALPINE_VERSION} AS build
ARG LIBYANG_VERSION=v6.4.13
ARG LIBYANG_COMMIT=f7e0cc1bc99ec9280af1010a94ba8dd8ef448f6d
RUN apk add --no-cache build-base cmake git pcre2-dev
RUN git clone --depth 1 --branch "${LIBYANG_VERSION}" https://github.com/CESNET/libyang.git /src \
 && test "$(git -C /src rev-parse HEAD)" = "${LIBYANG_COMMIT}"
RUN cmake -S /src -B /build \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DENABLE_TESTS=OFF \
      -DENABLE_VALGRIND_TESTS=OFF \
 && cmake --build /build -j"$(nproc)" \
 && cmake --install /build

FROM alpine:${ALPINE_VERSION}
RUN apk add --no-cache pcre2
COPY --from=build /usr/local/ /usr/local/
RUN yanglint --version
USER 65534:65534
ENTRYPOINT ["yanglint"]
