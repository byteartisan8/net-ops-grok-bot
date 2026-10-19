#!/usr/bin/env bash
# Build the Zabbix 8.0 images used by zabbix/compose.yaml (owner: 04-zabbix).
#
# Why a build: Zabbix 8.0 is not GA yet (newest release 8.0.0rc1, 2026-10-01) and Zabbix publishes no image tags for
# pre-releases on Docker Hub (only the nightly `trunk-*` images, currently an unreleased 8.0.0rc2). So the images are
# built from Zabbix's OWN Dockerfiles (github.com/zabbix/zabbix-docker, docker-bake.hcl) at a pinned commit, with the
# Zabbix sources at the release tag. Planned bump: once 8.0.0 GA is on Docker Hub, point zabbix/compose.yaml at
# zabbix/zabbix-{server-pgsql,web-nginx-pgsql,snmptraps}:alpine-8.0.0 and drop this script.
#
# Usage: zabbix/images/build-images.sh [--force]   (skips the build when every image already exists)
# Env:   ZBX_SOURCE_TAG, ZBX_DOCKER_COMMIT, ZBX_IMAGE_TAG override the pins below (keep zabbix/compose.yaml in sync).
set -euo pipefail

# Zabbix release tag (git.zabbix.com/scm/zbx/zabbix.git; the agent 2 plugin repos carry the same tag).
ZBX_SOURCE_TAG="${ZBX_SOURCE_TAG:-8.0.0rc1}"
# zabbix-docker `trunk` on 2026-09-28, three days before 8.0.0rc1: the Dockerfiles the rc1-era images were built
# with (later commits add build options for features that are not in rc1, e.g. APM proxies, the MCP server).
ZBX_DOCKER_COMMIT="${ZBX_DOCKER_COMMIT:-d60c37c45705a5e9492c8fca005776302081c13d}"
ZBX_DOCKER_REPO="https://github.com/zabbix/zabbix-docker.git"
ZBX_IMAGE_TAG="${ZBX_IMAGE_TAG:-${ZBX_SOURCE_TAG}-alpine}"
NAMESPACE="netsim/"
TARGETS=(server-pgsql web-nginx-pgsql snmptraps)

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
workdir="${repo_root}/build/zabbix-docker-${ZBX_DOCKER_COMMIT:0:12}"

images=()
for t in "${TARGETS[@]}"; do images+=("${NAMESPACE}zabbix-${t}:${ZBX_IMAGE_TAG}"); done

if [[ "${1:-}" != "--force" ]]; then
  missing=0
  for img in "${images[@]}"; do docker image inspect "$img" >/dev/null 2>&1 || missing=1; done
  if [[ $missing -eq 0 ]]; then
    echo "zabbix images: ${images[*]} already built (--force to rebuild)"
    exit 0
  fi
fi

if [[ ! -f "${workdir}/docker-bake.hcl" ]]; then
  echo "zabbix images: fetching zabbix-docker @ ${ZBX_DOCKER_COMMIT:0:12} into ${workdir#"${repo_root}"/}"
  rm -rf "${workdir}.tmp"
  git init -q "${workdir}.tmp"
  git -C "${workdir}.tmp" fetch -q --depth 1 "${ZBX_DOCKER_REPO}" "${ZBX_DOCKER_COMMIT}"
  git -C "${workdir}.tmp" -c advice.detachedHead=false checkout -q FETCH_HEAD
  rm -rf "${workdir}"
  mv "${workdir}.tmp" "${workdir}"
fi
[[ "$(git -C "${workdir}" rev-parse HEAD)" == "${ZBX_DOCKER_COMMIT}" ]] || {
  echo "zabbix images: ${workdir} is not at ${ZBX_DOCKER_COMMIT}; delete it and re-run" >&2
  exit 1
}

echo "zabbix images: building Zabbix ${ZBX_SOURCE_TAG} -> ${images[*]} (first build ~10-20 min)"
start=$(date +%s)
# Bake variables come from the environment; relative contexts in the bake file resolve from its directory.
# Three phases, like Zabbix's own CI: the builders start FROM the build-base image by tag, which bake would
# otherwise try to pull before building it.
bake() {
  (
    cd "${workdir}"
    GIT_BRANCH="${ZBX_SOURCE_TAG}" ZBX_IMAGE_NAMESPACE="${NAMESPACE}" ZBX_IMAGE_TAG="${ZBX_IMAGE_TAG}" \
      docker buildx bake --progress "${BUILDKIT_PROGRESS:-plain}" --load -f docker-bake.hcl "$@"
  )
}
bake build-base
bake build-pgsql build-common
bake "${TARGETS[@]}"
echo "zabbix images: built in $(($(date +%s) - start)) s"
for img in "${images[@]}"; do
  docker image inspect -f "  {{index .RepoTags 0}}  {{.Id}}  {{.Size}} bytes" "$img"
done
docker run --rm --entrypoint /usr/sbin/zabbix_server "${NAMESPACE}zabbix-server-pgsql:${ZBX_IMAGE_TAG}" -V | head -2
