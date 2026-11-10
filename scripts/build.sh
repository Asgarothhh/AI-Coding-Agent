#!/usr/bin/env bash
# Build the openJiuwen Challenge runtime image.
#
#   bash scripts/build.sh
#
# The build is fully offline-reproducible from requirements.lock and the
# digest-pinned base image. Build time and image size are printed at the end and
# must stay below the limits in spec section 11.1 (< 1000 s, < 4 GB).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

IMAGE="${IMAGE:-openjiuwen-hack:latest}"
FINAL_TAG="hack:latest"
# Rough peak requirement: base image (~60 MB) + dependency wheels (~1.2 GB
# unpacked) + BuildKit cache for the install layer (~1.5 GB) with headroom.
NEED_MB="${NEED_MB:-4096}"

command -v docker >/dev/null 2>&1 || { echo "FATAL: docker is not installed" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "FATAL: docker daemon is not reachable" >&2; exit 1; }

DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
FREE_MB="$(df -Pm "${DOCKER_ROOT}" 2>/dev/null | awk 'NR==2 {print $4}')"
FREE_MB="${FREE_MB:-0}"

echo "== preflight =="
echo "docker root : ${DOCKER_ROOT}"
echo "free space  : ${FREE_MB} MB (required: ${NEED_MB} MB)"
if [ "${FREE_MB}" -lt "${NEED_MB}" ]; then
    cat >&2 <<EOF
FATAL: insufficient disk space for the image build.
  docker root : ${DOCKER_ROOT}
  free        : ${FREE_MB} MB
  required    : ${NEED_MB} MB (base ~60 MB + deps ~1.2 GB + build cache ~1.5 GB)
Free space first, or reclaim BuildKit cache with an explicitly approved
\`docker builder prune\` / \`docker system prune\`. No packages are removed by
this script.
EOF
    exit 77
fi

echo "== build ${IMAGE} =="
START_NS="$(date +%s%N)"
docker build \
    --progress=plain \
    --tag "${IMAGE}" \
    --tag "${FINAL_TAG}" \
    .
END_NS="$(date +%s%N)"
ELAPSED_MS=$(( (END_NS - START_NS) / 1000000 ))
ELAPSED_S="$(awk -v ms="${ELAPSED_MS}" 'BEGIN {printf "%.1f", ms / 1000}')"

SIZE_BYTES="$(docker image inspect --format '{{.Size}}' "${IMAGE}")"
SIZE_MIB="$(awk -v b="${SIZE_BYTES}" 'BEGIN {printf "%.1f", b / 1048576}')"
CONTENT_BYTES="$(docker image inspect --format '{{.Size}}' "${FINAL_TAG}")"

echo
echo "== result =="
echo "build_seconds : ${ELAPSED_S}"
echo "size_bytes    : ${SIZE_BYTES}"
echo "size_mib      : ${SIZE_MIB}"
echo "tags          : ${IMAGE}, ${FINAL_TAG}"
echo
echo "-- docker images (organizer command) --"
docker images "${IMAGE}"
docker images "${FINAL_TAG}"

python3 - "${ELAPSED_S}" "${SIZE_MIB}" <<'PY'
import sys
seconds, mib = float(sys.argv[1]), float(sys.argv[2])
fail = False
if seconds >= 1000:
    print(f"FAIL: build time {seconds:.1f}s exceeds the 1000 s limit", file=sys.stderr)
    fail = True
if mib >= 4096:
    print(f"FAIL: image size {mib:.1f} MiB exceeds the 4 GiB limit", file=sys.stderr)
    fail = True
if not fail:
    print(f"OK: {seconds:.1f}s < 1000 s and {mib:.1f} MiB < 4096 MiB")
sys.exit(1 if fail else 0)
PY
