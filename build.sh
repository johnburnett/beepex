#!/usr/bin/env bash
set -o errexit -o nounset
uv run ruff format
uv run mypy .
uv run ty check
uv run ruff check
uv run python beepex.py --create-example
uv run pyinstaller --noconfirm beepex.spec
