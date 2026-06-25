#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: benchmark/scripts/run-query.sh <query.sql> [psql args...]" >&2
    exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
QUERY_FILE="$1"
shift

if [[ ! -f "${QUERY_FILE}" ]]; then
    echo "query file not found: ${QUERY_FILE}" >&2
    exit 1
fi

QUERY_ABS="$(cd "$(dirname "${QUERY_FILE}")" && pwd)/$(basename "${QUERY_FILE}")"
BENCHMARK_DIR="${ROOT_DIR}/benchmark"

case "${QUERY_ABS}" in
    "${BENCHMARK_DIR}"/*)
        CONTAINER_QUERY="/benchmark/${QUERY_ABS#"${BENCHMARK_DIR}/"}"
        ;;
    *)
        echo "query file must be under ${BENCHMARK_DIR}: ${QUERY_ABS}" >&2
        exit 1
        ;;
esac

"${ROOT_DIR}/benchmark/scripts/psql.sh" \
    -v ON_ERROR_STOP=1 \
    -f "${CONTAINER_QUERY}" \
    "$@"
