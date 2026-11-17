#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker/rdf-benchmark/docker-compose.yml}"
STRABON_RDF_ROOT="${STRABON_RDF_ROOT:-/rdf/yago2geo-jena-input}"
STRABON_DB="${STRABON_DB:-endpoint}"
STRABON_USER="${STRABON_USER:-postgres}"
STRABON_PASSWORD="${STRABON_PASSWORD:-postgres}"

docker compose -f "${COMPOSE_FILE}" build strabon
docker compose -f "${COMPOSE_FILE}" up -d strabon-postgis
docker compose -f "${COMPOSE_FILE}" exec -T strabon-postgis \
    bash -lc "for i in \$(seq 1 120); do pg_isready -U '${STRABON_USER}' -d '${STRABON_DB}' >/dev/null 2>&1 || pg_isready -U '${STRABON_USER}' -d postgres >/dev/null 2>&1; if [[ \$? -eq 0 ]]; then exit 0; fi; sleep 2; done; exit 1"
if [[ "${STRABON_RESET_DB:-0}" == "1" ]]; then
    docker compose -f "${COMPOSE_FILE}" exec -T strabon-postgis \
        bash -lc "dropdb -U '${STRABON_USER}' --if-exists '${STRABON_DB}'"
fi
docker compose -f "${COMPOSE_FILE}" exec -T strabon-postgis \
    bash -lc "createdb -U '${STRABON_USER}' '${STRABON_DB}' 2>/dev/null || true; psql -U '${STRABON_USER}' -d '${STRABON_DB}' -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS postgis;' -c 'GRANT ALL ON geometry_columns TO PUBLIC;' -c 'GRANT ALL ON geography_columns TO PUBLIC;' -c 'GRANT ALL ON spatial_ref_sys TO PUBLIC;'"
docker compose -f "${COMPOSE_FILE}" run --rm \
    -e RDF_ROOT="${STRABON_RDF_ROOT}" \
    -e STRABON_DB="${STRABON_DB}" \
    -e STRABON_START_FROM="${STRABON_START_FROM:-}" \
    -e STRABON_START_AFTER="${STRABON_START_AFTER:-}" \
    strabon-loader
docker compose -f "${COMPOSE_FILE}" up -d strabon

echo "Strabon endpoint: http://127.0.0.1:${STRABON_PORT:-18080}/Strabon"
echo "Strabon loaded PostGIS DB: ${STRABON_DB}"
if [[ "${STRABON_DB}" != "endpoint" ]]; then
    echo "Note: the packaged Strabon web endpoint is built to read DB 'endpoint'; use strabon-cmd or rebuild endpoint config for DB '${STRABON_DB}'."
fi
