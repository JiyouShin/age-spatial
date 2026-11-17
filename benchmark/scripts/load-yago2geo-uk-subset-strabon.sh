#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

STRABON_DB="${STRABON_DB:-yago2geo_uk_subset}" \
STRABON_RESET_DB="${STRABON_RESET_DB:-1}" \
STRABON_RDF_ROOT="${STRABON_RDF_ROOT:-/rdf/rdfdb/uk_subset_input}" \
    "${ROOT_DIR}/benchmark/scripts/load-yago2geo-strabon.sh"

