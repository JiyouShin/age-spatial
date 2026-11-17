#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

JENA_DATASET="${JENA_DATASET:-yago2geo_uk_subset}" \
JENA_DB_LOC="${JENA_DB_LOC:-/fuseki/databases/yago2geo_uk_subset}" \
JENA_RDF_ROOT="${JENA_RDF_ROOT:-/rdf/rdfdb/uk_subset_input}" \
    "${ROOT_DIR}/benchmark/scripts/load-yago2geo-jena.sh"

