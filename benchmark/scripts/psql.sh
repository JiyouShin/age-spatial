#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker/benchmark/docker-compose.yml}"
SERVICE="${SERVICE:-age-spatial-pg}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-age_spatial_bench}"

docker compose -f "${COMPOSE_FILE}" exec -T "${SERVICE}" \
    psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" "$@"
