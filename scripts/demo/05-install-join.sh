#!/usr/bin/env bash
# MeshForge v1.0 demo 05-install-join (roadmap 1.0.7, D420) - see scripts/demo/README.md.
# Options: --dry-run, --reset, ... (python scripts/demo/demo.py --help).
# MESHFORGE_DEMO_PYTHON: the Python to use instead of `uv run python`.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${MESHFORGE_DEMO_PYTHON:-}" ]]; then
  exec "$MESHFORGE_DEMO_PYTHON" "$here/demo.py" 05-install-join "$@"
fi
exec uv run python "$here/demo.py" 05-install-join "$@"
