#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: benchmark/scripts/capture-query.sh <query.sql> [output-file]" >&2
    exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
QUERY_FILE="$1"
OUTPUT_FILE="${2:-}"

if [[ -z "${OUTPUT_FILE}" ]]; then
    mkdir -p "${ROOT_DIR}/benchmark/results"
    base_name="$(basename "${QUERY_FILE}" .sql)"
    timestamp="$(date +%Y%m%d-%H%M%S)"
    OUTPUT_FILE="${ROOT_DIR}/benchmark/results/${base_name}-${timestamp}.out"
fi

"${ROOT_DIR}/benchmark/scripts/run-query.sh" "${QUERY_FILE}" | tee "${OUTPUT_FILE}"

echo "saved: ${OUTPUT_FILE}"
