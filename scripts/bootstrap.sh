#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
printf '请复制 config/*.example.json 为 *.local.json 并填写配置\n'
