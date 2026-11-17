#!/usr/bin/env bash
set -euo pipefail

QUERY_FILE="${1:?Usage: rdf-sparql-virtuoso.sh QUERY_FILE}"
VIRTUOSO_HTTP_PORT="${VIRTUOSO_HTTP_PORT:-18890}"
YAGO2GEO_GRAPH_IRI="${YAGO2GEO_GRAPH_IRI:-http://yago-knowledge.org/graph/yago2geo}"

curl -sS \
    -H 'Accept: text/csv' \
    --data-urlencode "default-graph-uri=${YAGO2GEO_GRAPH_IRI}" \
    --data-urlencode "query@${QUERY_FILE}" \
    "http://127.0.0.1:${VIRTUOSO_HTTP_PORT}/sparql"
