#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker build -t openjiuwen-hack:latest .

# The final run contract refers to the image as hack:latest; keep both names.
docker tag openjiuwen-hack:latest hack:latest
