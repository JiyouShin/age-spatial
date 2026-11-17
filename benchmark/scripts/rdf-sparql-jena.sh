#!/usr/bin/env bash
set -euo pipefail

QUERY_FILE="${1:?Usage: rdf-sparql-jena.sh QUERY_FILE}"
JENA_PORT="${JENA_PORT:-13030}"
JENA_DATASET="${JENA_DATASET:-yago2geo}"

curl -sS \
    -H 'Accept: text/csv' \
    --data-urlencode "query@${QUERY_FILE}" \
    "http://127.0.0.1:${JENA_PORT}/${JENA_DATASET}/sparql"
