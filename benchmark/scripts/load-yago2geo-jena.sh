#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker/rdf-benchmark/docker-compose.yml}"
JENA_RDF_ROOT="${JENA_RDF_ROOT:-/rdf/yago2geo}"
JENA_DB_LOC="${JENA_DB_LOC:-/fuseki/databases/yago2geo}"
JENA_DATASET="${JENA_DATASET:-yago2geo}"

docker compose -f "${COMPOSE_FILE}" build jena
"${ROOT_DIR}/benchmark/scripts/build-jena-noindex-launcher.sh"

docker compose -f "${COMPOSE_FILE}" stop jena >/dev/null 2>&1 || true
docker compose -f "${COMPOSE_FILE}" run --rm \
    -e RDF_ROOT="${JENA_RDF_ROOT}" \
    -e JENA_DB_LOC="${JENA_DB_LOC}" \
    jena-loader
JENA_DATASET="${JENA_DATASET}" JENA_DB_LOC="${JENA_DB_LOC}" \
    docker compose -f "${COMPOSE_FILE}" up -d jena

echo "Jena Fuseki endpoint: http://127.0.0.1:${JENA_PORT:-13030}/${JENA_DATASET}/sparql"
