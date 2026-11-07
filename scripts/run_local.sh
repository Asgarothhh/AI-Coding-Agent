#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKS_DIR="${PROJECT_ROOT}/examples/task-dir"

# Offline smoke run: same hardening flags as the final contract, no network.
#
# --user: the examples are owned by the host user. With --cap-drop ALL the
# container root loses CAP_DAC_OVERRIDE, so it cannot write a 0755 bind mount
# owned by another uid. The final contract runs as root and therefore requires
# /data/tasks to be writable by uid 0 (e.g. 0777 or owned by root).
exec docker run --rm \
    --user "$(id -u):$(id -g)" \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,size=2g \
    -e HOME=/tmp \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --workdir /app \
    -v "${TASKS_DIR}:/data/tasks" \
    --entrypoint python \
    openjiuwen-hack:latest \
    /app/run_agent.py --tasks-dir /data/tasks --offline --max-workers 1 "$@"
