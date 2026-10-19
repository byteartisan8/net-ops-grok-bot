#!/usr/bin/env bash
# Run yanglint (libyang) with the lab's pinned version.
#
# Default: the container image built from tools/docker/yanglint.Dockerfile (built on first use).
# Set YANGLINT=/path/to/yanglint (or YANGLINT=yanglint) to use a native binary instead.
# Paths given as arguments must be inside the repository (it is mounted read-only at the same path).
set -euo pipefail

if [[ -n "${YANGLINT:-}" ]]; then
  exec "${YANGLINT}" "$@"
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/.." && pwd)"
dockerfile="${here}/docker/yanglint.Dockerfile"
version="$(sed -n 's/^ARG LIBYANG_VERSION=v//p' "${dockerfile}")"
image="${NETSIM_YANGLINT_IMAGE:-netsim/yanglint:${version}}"

if ! docker image inspect "${image}" >/dev/null 2>&1; then
  echo "tools/yanglint.sh: building ${image} (one-time)..." >&2
  docker build --quiet -t "${image}" -f "${dockerfile}" "${here}/docker" >&2
fi

workdir="${PWD}"
case "${workdir}" in
  "${repo}"|"${repo}"/*) ;;
  *) workdir="${repo}" ;;
esac

exec docker run --rm -i --network none \
  -v "${repo}:${repo}:ro" -w "${workdir}" \
  "${image}" "$@"
