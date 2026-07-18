#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2

sudo mkdir -p /home/agent/data /home/agent/soul /home/agent/workspace
sudo chown -R "$USER":"$USER" /home/agent/data /home/agent/soul /home/agent/workspace
mkdir -p data soul workspace

docker compose pull
docker compose up -d

curl -fsS http://localhost:8080/health
