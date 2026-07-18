#!/usr/bin/env bash
set -euo pipefail

git pull --ff-only
docker compose build
docker compose up -d --no-deps luck-agent
