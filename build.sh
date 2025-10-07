#!/usr/bin/env bash
set -o errexit -o nounset
uv run ruff format
uv run ty check
uv run ruff check
