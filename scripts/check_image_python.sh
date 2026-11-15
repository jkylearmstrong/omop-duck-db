#!/usr/bin/env bash
# Fails unless the image's Python is exactly the version pinned by `ARG PYTHON_VERSION=` in the Dockerfile.
# Usage: scripts/check_image_python.sh <image> [Dockerfile]
set -euo pipefail
image="$1"
dockerfile="${2:-Dockerfile}"
pin="$(sed -n 's/^ARG PYTHON_VERSION=\([0-9.]*\)$/\1/p' "$dockerfile" | head -n 1)"
if [ -z "$pin" ]; then echo "No 'ARG PYTHON_VERSION=x.y.z' line in $dockerfile" >&2; exit 1; fi
got="$(docker run --rm "$image" python -c 'import platform; print(platform.python_version())')"
got3="$(docker run --rm "$image" python3 -c 'import platform; print(platform.python_version())')"
echo "pinned: $pin  image python: $got  image python3: $got3"
if [ "$got" != "$pin" ] || [ "$got3" != "$pin" ]; then
  echo "::error::The image runs Python $got (python3: $got3), but $dockerfile pins $pin" >&2
  exit 1
fi
