#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  if command -v python3.12 >/dev/null; then
    launcher_python=python3.12
  else
    launcher_python=python3
  fi
  "$launcher_python" -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi

umask 077
export HK_RUN_MODE=local
.venv/bin/python ops/local_runtime.py start
