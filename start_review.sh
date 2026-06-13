#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

".venv/bin/python" -m pip install -r requirements.txt
".venv/bin/python" cli.py serve --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
