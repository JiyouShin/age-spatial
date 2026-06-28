#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker/benchmark/docker-compose.yml}"
SERVICE="${SERVICE:-age-spatial-pg}"
COUNTRY="${1:-all}"
SOURCE_DIR="${ROOT_DIR}/benchmark/results/load/yago2geo"

if [[ ! -d "${SOURCE_DIR}" ]]; then
    echo "Missing ${SOURCE_DIR}. Run benchmark/scripts/prepare-yago2geo-age-load.py first." >&2
    exit 1
fi

if [[ "${COUNTRY}" != "all" && ! -f "${SOURCE_DIR}/${COUNTRY}/load_${COUNTRY}.sql" ]]; then
    echo "Missing load SQL for country '${COUNTRY}'." >&2
    echo "Expected: ${SOURCE_DIR}/${COUNTRY}/load_${COUNTRY}.sql" >&2
    exit 1
fi

docker compose -f "${COMPOSE_FILE}" exec -T "${SERVICE}" \
    bash -lc "rm -rf /tmp/age/yago2geo && mkdir -p /tmp/age && cp -r /benchmark/results/load/yago2geo /tmp/age/"

if [[ "${COUNTRY}" == "all" ]]; then
    "${ROOT_DIR}/benchmark/scripts/psql.sh" -v ON_ERROR_STOP=1 \
        -f /tmp/age/yago2geo/load_all.sql
else
    "${ROOT_DIR}/benchmark/scripts/psql.sh" -v ON_ERROR_STOP=1 \
        -f "/tmp/age/yago2geo/${COUNTRY}/load_${COUNTRY}.sql"
fi
