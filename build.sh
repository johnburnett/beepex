#!/usr/bin/env bash
set -o errexit -o nounset
if [[ "${1:-}" == "clean" ]]; then
  echo "Cleaning..."
  rm -rf ".mypy_cache"
  rm -rf ".ruff_cache"
  rm -rf ".venv"
  rm -rf "build"
  rm -rf "dist"
  uv sync
fi
uv run ruff format
uv run mypy .
uv run ty check
uv run ruff check
uv run python beepex.py --create_example ./example
uv run pyinstaller --noconfirm beepex.spec
