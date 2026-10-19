#!/usr/bin/env bash
# Load contract/yang/netsim-device.yang into a real Clixon backend and validate the golden configs and the
# YANG examples through NETCONF (edit-config + validate). Builds the pinned image on first use (a few minutes).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/.." && pwd)"
dockerfile="${here}/docker/clixon-check.Dockerfile"
version="$(sed -n 's/^ARG CLIXON_VERSION=//p' "${dockerfile}")"
image="${NETSIM_CLIXON_IMAGE:-netsim/clixon-check:${version}}"

if ! docker image inspect "${image}" >/dev/null 2>&1; then
  echo "tools/clixon-check.sh: building ${image} (one-time, a few minutes)..." >&2
  docker build --quiet -t "${image}" -f "${dockerfile}" "${here}/docker" >&2
fi

exec docker run --rm --network none -v "${repo}:/repo:ro" -w /repo "${image}" python3 tools/clixon_check.py "$@"
