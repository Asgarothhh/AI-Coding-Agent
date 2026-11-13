# openJiuwen Challenge 2026 harness image.
#
# Runtime contract (spec 001-token-estimator v5.0, sections 10 and 11):
#   docker run --network hack-net --read-only --tmpfs /tmp:rw,size=2g \
#       -e HOME=/tmp --cpus 4 --memory 16g --cap-drop ALL \
#       --security-opt no-new-privileges -v "$TASKS:/data/tasks:rw" \
#       hack:latest python /app/run_agent.py --tasks-dir /data/tasks
#
# Base image is pinned by tag AND digest (multi-arch OCI index). The tag stays
# readable; the digest makes the build reproducible and immune to re-tags.
#   python:3.11-slim-bookworm
#   index digest: sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
#   amd64 manifest: sha256:e8c69d86735f6df3efa06b324694fe51f0422785f83a3bd463052636c2421944
#   python 3.11.18, debian bookworm-slim
FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39

LABEL org.opencontainers.image.title="openjiuwen-challenge-harness" \
      org.opencontainers.image.description="Batch token-budget harness for openJiuwen Challenge 2026" \
      org.opencontainers.image.source="local" \
      org.opencontainers.image.licenses="proprietary" \
      org.openjiuwen.runtime.contract="python /app/run_agent.py --tasks-dir /data/tasks" \
      org.openjiuwen.runtime.cpu="4" \
      org.openjiuwen.runtime.memory="16g"

# Deterministic, non-interactive Python. Byte-compilation is disabled so the
# process never tries to write .pyc files into the read-only rootfs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ca-certificates keeps TLS validation working for the organizer LLM proxy.
# No compilers: every dependency is installed from a prebuilt wheel.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /var/cache/debconf/*

# Dependencies come only from the hashed lock file. --require-hashes rejects any
# artifact that does not match the recorded digest; --no-deps guarantees the
# closure is exactly what the lock describes and nothing is resolved ad hoc.
# The single RUN also drops the lock file and every pip/build cache, so none of
# them survive into the image layer.
COPY requirements.lock /tmp/requirements.lock
RUN set -eux; \
    pip install \
        --no-cache-dir \
        --no-deps \
        --require-hashes \
        --root-user-action=ignore \
        --no-warn-script-location \
        -r /tmp/requirements.lock; \
    rm -f /tmp/requirements.lock; \
    rm -rf /root/.cache /root/.local /tmp/* /var/tmp/*; \
    python -c "import openjiuwen, sys; print('openjiuwen import OK', sys.version.split()[0])"

# Application code only: the harness is stdlib-first and imports the SDK lazily.
COPY run_agent.py /app/run_agent.py
COPY harness/ /app/harness/
COPY entrypoint.sh /entrypoint.sh

RUN set -eux; \
    chmod 0755 /entrypoint.sh; \
    python -m compileall -q /app/harness /app/run_agent.py; \
    chmod -R a+rX /app

# After the build nothing may be downloaded or installed: the runtime image has
# no package index configured, and the process runs with a read-only rootfs.
ENV PIP_NO_INDEX=1 \
    HOME=/tmp

# Run as root on purpose. The judge bind-mounts /data/tasks with unknown host
# ownership, and the harness must create answer.txt inside each task directory.
# --cap-drop ALL + --security-opt no-new-privileges remove the capabilities that
# would make root dangerous (no CAP_DAC_OVERRIDE, no setuid escalation).
# Do not "fix" this by adding USER without verifying the mount in the final
# command; see spec section 11.2.

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "/app/run_agent.py", "--tasks-dir", "/data/tasks", "--llm-backend", "gateway", "--model-policy", "fixed"]
