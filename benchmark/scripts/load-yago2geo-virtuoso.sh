#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker/rdf-benchmark/docker-compose.yml}"
VIRTUOSO_RDF_ROOT="${VIRTUOSO_RDF_ROOT:-/database/rdf/yago2geo}"
YAGO2GEO_GRAPH_IRI="${YAGO2GEO_GRAPH_IRI:-http://yago-knowledge.org/graph/yago2geo}"

docker compose -f "${COMPOSE_FILE}" up -d virtuoso
docker compose -f "${COMPOSE_FILE}" exec -T \
    -e RDF_ROOT="${VIRTUOSO_RDF_ROOT}" \
    -e YAGO2GEO_GRAPH_IRI="${YAGO2GEO_GRAPH_IRI}" \
    virtuoso /scripts/load-virtuoso-yago2geo.sh

echo "Virtuoso SPARQL endpoint: http://127.0.0.1:${VIRTUOSO_HTTP_PORT:-18890}/sparql"
echo "Virtuoso graph IRI: ${YAGO2GEO_GRAPH_IRI}"
