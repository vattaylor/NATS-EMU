#!/usr/bin/env bash
# Start NATS EMU. Uses the system Python when PySide6 is already installed.
set -euo pipefail
cd "$(dirname "$0")"

if python3 -c "import PySide6" >/dev/null 2>&1; then
  exec python3 -m nats_emu "$@"
fi

echo "Installing PySide6 into .venv…" >&2
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -r requirements.txt
exec .venv/bin/python -m nats_emu "$@"
