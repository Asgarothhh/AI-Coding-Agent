#!/usr/bin/env bash
# Reproduce the judge's container contract locally, offline.
#
#   bash scripts/run_local.sh
#
# This is deliberately the *same shape* as the final command in the regulation
# (spec section 11.2), minus the LLM network: read-only rootfs, 2 GiB tmpfs /tmp,
# HOME=/tmp, no capabilities, no privilege escalation, 4 CPUs, 16 GiB RAM, and
# the tasks directory bind-mounted read-write.
#
# The examples are copied to a temporary directory first: the harness writes
# answer.txt (and may patch workspace files) inside the tasks tree, and the
# repository copies must stay pristine.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-openjiuwen-hack:latest}"
SRC_TASKS="${SRC_TASKS:-${PROJECT_ROOT}/examples/task-dir}"
EXPECT_TASKS="${EXPECT_TASKS:-8}"
EXPECT_SCORE="${EXPECT_SCORE:-20}"
EXPECT_TOKENS="${EXPECT_TOKENS:-0}"

[ -d "${SRC_TASKS}" ] || { echo "FATAL: tasks source not found: ${SRC_TASKS}" >&2; exit 1; }
docker image inspect "${IMAGE}" >/dev/null 2>&1 \
    || { echo "FATAL: image ${IMAGE} not found; run bash scripts/build.sh first" >&2; exit 1; }

WORK_TASKS="${WORK_TASKS:-$(mktemp -d "${TMPDIR:-/tmp}/oj-tasks.XXXXXX")}"
LOG_FILE="${LOG_FILE:-${WORK_TASKS}.json}"
LOG_ERR="${LOG_ERR:-${WORK_TASKS}.stderr.log}"

# Independent copy: the judge's mount is a separate filesystem, not the repo.
cp -a "${SRC_TASKS}/." "${WORK_TASKS}/"
# The organizer archive contains no Python caches; drop local leftovers so the
# smoke run starts from the same state as the judge's /data/tasks.
find "${WORK_TASKS}" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "${WORK_TASKS}" -type f -name '*.py[cod]' -delete 2>/dev/null || true
# The contract runs as root with --cap-drop ALL, i.e. without CAP_DAC_OVERRIDE.
# The host copy is owned by the local user, so make it writable by uid 0 as the
# judge's /data/tasks mount must be. A 0755 mount owned by a foreign uid cannot
# be written by the container at all; the harness then writes no answer.txt and
# reports every task as failed (see eval/results/docker.md).
chmod -R a+rwX "${WORK_TASKS}"

echo "== offline smoke =="
echo "image      : ${IMAGE}"
echo "tasks copy : ${WORK_TASKS}"
echo "json log   : ${LOG_FILE}"
echo "stderr log : ${LOG_ERR}"
echo

set +e
docker run --rm \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,size=2g \
    -e HOME=/tmp \
    --cpus 4 \
    --memory 16g \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --workdir /app \
    -v "${WORK_TASKS}:/data/tasks:rw" \
    "${IMAGE}" \
    python /app/run_agent.py --tasks-dir /data/tasks \
        --offline --mutation-repair --max-workers 1 "$@" \
    >"${LOG_FILE}" 2>"${LOG_ERR}"
RUN_STATUS=$?
set -e

echo "-- summary tail --"
tail -n 12 "${LOG_FILE}"
echo
echo "-- exit status: ${RUN_STATUS} --"

STATUS=0
if [ "${RUN_STATUS}" -ne 0 ]; then
    echo "FAIL: container exited with ${RUN_STATUS}; see ${LOG_ERR}" >&2
    STATUS=1
fi

python3 - "${LOG_FILE}" "${EXPECT_TASKS}" "${EXPECT_SCORE}" "${EXPECT_TOKENS}" <<'PY' || STATUS=1
import json
import sys

path, tasks, score, tokens = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()
# The harness prints a short human-readable preamble before the JSON summary.
start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("{")), None)
if start is None:
    print("FAIL: no JSON summary on container stdout", file=sys.stderr)
    sys.exit(1)
data = json.loads("\n".join(lines[start:]))
fields = {
    "tasks": data.get("tasks"),
    "solved": data.get("solved"),
    "failed": data.get("failed"),
    "errors": data.get("errors"),
    "score": data.get("score"),
    "tokens_spent": data.get("tokens_spent"),
    "offline": data.get("offline"),
}
print("summary:", json.dumps(fields, ensure_ascii=False))
problems = []
if data.get("offline") is not True:
    problems.append("offline flag is not set")
if data.get("tasks") != tasks or data.get("solved") != tasks:
    problems.append(f"expected {tasks}/{tasks} solved, got {data.get('solved')}/{data.get('tasks')}")
if float(data.get("score") or 0) != score:
    problems.append(f"expected score {score:g}, got {data.get('score')}")
if float(data.get("tokens_spent") or 0) != tokens:
    problems.append(f"expected {tokens:g} tokens spent, got {data.get('tokens_spent')}")
if data.get("failed") or data.get("errors"):
    problems.append(f"failed={data.get('failed')} errors={data.get('errors')}")
if problems:
    for p in problems:
        print("FAIL:", p, file=sys.stderr)
    sys.exit(1)
print(f"OK: {data['solved']}/{data['tasks']} solved, score {float(data['score']):g}, tokens {float(data['tokens_spent']):g}")
PY

echo
echo "-- answer.txt placement --"
COPY_ANSWERS="$(find "${WORK_TASKS}" -name answer.txt | wc -l)"
REPO_ANSWERS="$(find "${PROJECT_ROOT}/examples" -name answer.txt 2>/dev/null | wc -l)"
echo "answer.txt in tasks copy : ${COPY_ANSWERS}"
echo "answer.txt in repo       : ${REPO_ANSWERS}"
if [ "${REPO_ANSWERS}" -ne 0 ]; then
    echo "FAIL: the run wrote into the repository examples directory" >&2
    STATUS=1
fi
if [ "${COPY_ANSWERS}" -eq 0 ]; then
    echo "FAIL: no answer.txt was produced in the tasks copy" >&2
    STATUS=1
fi

if [ "${STATUS}" -eq 0 ]; then
    echo "RESULT: PASS (offline smoke, 4 CPU / 16 GiB / read-only rootfs, no network)"
else
    echo "RESULT: FAIL"
fi
echo "tasks copy kept for inspection: ${WORK_TASKS}"
exit "${STATUS}"
