#!/bin/bash
set -o errexit -o nounset
trap 'echo "Exit status $? at line $LINENO from: $BASH_COMMAND"' ERR

if [[ "${1:-}" == "clean" ]]; then
  echo "Cleaning..."
  rm -rf ".mypy_cache"
  rm -rf ".ruff_cache"
  rm -rf ".venv"
  rm -rf "build"
  rm -rf "dist"
else
  uv sync
  uv run ruff format
  uv run mypy .
  uv run ty check
  uv run ruff check
  uv run python beepex.py --token STUB_TOKEN --create_example ./example
  uv run pyinstaller --noconfirm beepex.spec
fi
