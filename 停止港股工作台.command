#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo '本机工作台尚未安装，无需停止。'
  exit 0
fi
umask 077
.venv/bin/python ops/local_runtime.py stop
