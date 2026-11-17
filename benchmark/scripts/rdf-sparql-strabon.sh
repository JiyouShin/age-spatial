#!/usr/bin/env bash
set -euo pipefail

QUERY_FILE="${1:?Usage: rdf-sparql-strabon.sh QUERY_FILE}"
STRABON_PORT="${STRABON_PORT:-18080}"

curl -sS \
    -H 'Accept: text/csv' \
    --data-urlencode "query@${QUERY_FILE}" \
    "http://127.0.0.1:${STRABON_PORT}/Strabon/Query"
