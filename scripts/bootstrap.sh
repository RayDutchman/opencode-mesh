#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
printf 'Copy config/*.example.json to *.local.json and fill in your configuration\n'
