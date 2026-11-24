#!/bin/sh
# macOS: keep all existing data in this installation.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.10以降をインストールしてから再度起動してください。'
  exit 1
fi
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python server.py
