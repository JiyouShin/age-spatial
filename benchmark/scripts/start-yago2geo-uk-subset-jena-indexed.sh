#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker/rdf-benchmark/docker-compose.yml}"
JENA_DATASET="${JENA_DATASET:-yago2geo_uk_subset}"
JENA_DB_LOC="${JENA_DB_LOC:-/fuseki/databases/yago2geo_uk_subset}"
JENA_SPATIAL_INDEX_FILE="${JENA_SPATIAL_INDEX_FILE:-/fuseki/databases/yago2geo_uk_subset/spatial.index}"

docker compose -f "${COMPOSE_FILE}" stop jena >/dev/null 2>&1 || true

JENA_DATASET="${JENA_DATASET}" \
JENA_DB_LOC="${JENA_DB_LOC}" \
JENA_SPATIAL_INDEX=true \
JENA_INDEX=true \
JENA_SPATIAL_INDEX_FILE="${JENA_SPATIAL_INDEX_FILE}" \
    docker compose -f "${COMPOSE_FILE}" up -d jena

echo "Jena indexed Fuseki endpoint: http://127.0.0.1:${JENA_PORT:-13030}/${JENA_DATASET}/sparql"
