#!/bin/bash
set -o errexit -o nounset
trap 'echo "Exit status $? at line $LINENO from: $BASH_COMMAND"' ERR

uv sync
uv run ruff format
uv run ty check
uv run ruff check
