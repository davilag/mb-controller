#!/bin/sh
# Stop the detached macOS supervisor and its keep-awake assertion.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$project_root/.venv/bin/python" "$project_root/scripts/overnight.py" stop
