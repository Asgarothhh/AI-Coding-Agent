# syntax=docker/dockerfile:1
#
# openJiuwen Challenge 2026 harness image.
#
# The final run contract mounts the tasks directory read-write at /data/tasks and
# uses: --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp --cap-drop ALL
#       --security-opt no-new-privileges
#
# We intentionally run as root: the permissions/ownership of the host-provided
# /data/tasks mount are unknown, and the harness must be able to create
# answer.txt / solutions.json / trace.jsonl there. The runtime restrictions
# (--cap-drop ALL, --security-opt no-new-privileges, read-only rootfs) already
# remove the capabilities that would make root dangerous.

FROM python:3.11.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ca-certificates keeps TLS validation working for the optional LLM proxy.
# bash/coreutils/grep are already present in the slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY run_agent.py ./
COPY harness/ harness/

ENTRYPOINT ["python"]
CMD ["/app/run_agent.py", "--tasks-dir", "/data/tasks"]
