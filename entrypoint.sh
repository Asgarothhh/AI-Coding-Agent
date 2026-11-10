#!/bin/sh
# Entry point for the openJiuwen Challenge harness image.
#
# Contract (spec 001-token-estimator v5.0, sections 10 and 11):
#   * exec the given command as PID 1, so signals and exit codes reach the judge;
#   * no network downloads, no package installs, no configuration rewriting;
#   * no writes outside the mounted tasks tree, $OUT and /tmp (writable tmpfs).
#
# This script therefore only normalises HOME and execs. It must stay free of
# side effects: the root filesystem is mounted read-only at runtime.
set -eu

# The judge passes HOME=/tmp explicitly; keep the same default for local runs.
: "${HOME:=/tmp}"
export HOME

exec "$@"
