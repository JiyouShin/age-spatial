#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GEOM_GRAPH_IRI="${YAGO2GEO_VIRT_GEOM_GRAPH_IRI:-http://yago-knowledge.org/graph/yago2geo_uk_subset_virtgeom}"
VIRTUOSO_RDF_ROOT="${VIRTUOSO_RDF_ROOT:-/database/rdfdb/uk_subset_virtgeom_input}"

"${ROOT_DIR}/benchmark/scripts/prepare-yago2geo-uk-virtuoso-geom-input.py"

YAGO2GEO_GRAPH_IRI="${GEOM_GRAPH_IRI}" \
VIRTUOSO_RDF_ROOT="${VIRTUOSO_RDF_ROOT}" \
    "${ROOT_DIR}/benchmark/scripts/load-yago2geo-virtuoso.sh"

docker exec "${VIRTUOSO_CONTAINER:-yago2geo-virtuoso}" bash -lc "cat >/tmp/check_uk_virtgeom.sql <<'SQL'
SELECT COUNT(*) AS original_triples
FROM DB.DBA.RDF_QUAD
WHERE g = iri_to_id('http://yago-knowledge.org/graph/yago2geo_uk_subset');

SELECT COUNT(*) AS virtgeom_triples
FROM DB.DBA.RDF_QUAD
WHERE g = iri_to_id('${GEOM_GRAPH_IRI}');

SPARQL
DEFINE input:default-graph-uri <${GEOM_GRAPH_IRI}>
PREFIX wgs: <http://www.w3.org/2003/01/geo/wgs84_pos#>
SELECT ?feature (DATATYPE(?geom) AS ?datatype)
WHERE { ?feature wgs:geometry ?geom }
LIMIT 5;
SQL
ISQL=\$(command -v isql-v || command -v isql)
\"\${ISQL}\" 1111 dba \"\${DBA_PASSWORD:-dba}\" /tmp/check_uk_virtgeom.sql"
