#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

YAGO2GEO_GRAPH_IRI="${YAGO2GEO_GRAPH_IRI:-http://yago-knowledge.org/graph/yago2geo_uk_subset}" \
VIRTUOSO_RDF_ROOT="${VIRTUOSO_RDF_ROOT:-/database/rdfdb/uk_subset_input}" \
    "${ROOT_DIR}/benchmark/scripts/load-yago2geo-virtuoso.sh"
