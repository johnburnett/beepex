#!/bin/bash
set -o errexit -o nounset
trap 'echo "Exit status $? at line $LINENO from: $BASH_COMMAND"' ERR

if [[ "${1:-}" == "clean" ]]; then
  echo "Cleaning..."
  rm -rf ".ruff_cache"
  rm -rf ".venv"
  rm -rf "build"
  rm -rf "dist"
  rm __version__.py
else
  echo "__version__ = \"$(git describe --tags --always | cut -c 2-)\"" > __version__.py
  uv sync
  uv run ruff format
  uv run ty check
  uv run ruff check
  uv run python beepex.py --token STUB_TOKEN --create_example ./example
  find ./example/ -name \*.html -exec uv run python test/validatehtml.py {} +
  uv run pyinstaller --noconfirm beepex.spec
fi
