#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker/benchmark/docker-compose.yml}"
SERVICE="${SERVICE:-age-spatial-pg}"
SOURCE_DIR="${ROOT_DIR}/benchmark/results/load/yago2geo_uk_subset"

if [[ ! -f "${SOURCE_DIR}/load_yago2geo_uk_subset.sql" ]]; then
    echo "Missing UK subset AGE load SQL. Run benchmark/scripts/prepare-yago2geo-uk-subset.py first." >&2
    exit 1
fi

docker compose -f "${COMPOSE_FILE}" exec -T "${SERVICE}" \
    bash -lc "rm -rf /tmp/age/yago2geo_uk_subset && mkdir -p /tmp/age && cp -r /benchmark/results/load/yago2geo_uk_subset /tmp/age/"

"${ROOT_DIR}/benchmark/scripts/psql.sh" -v ON_ERROR_STOP=1 \
    -f /tmp/age/yago2geo_uk_subset/load_yago2geo_uk_subset.sql

