#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="${ROOT_DIR}/benchmark/results/tmp"
mkdir -p "${TMP_DIR}"

TOPOLOGY_QUERY="${TMP_DIR}/rdf-geosparql-topology-smoke.rq"
DISTANCE_QUERY="${TMP_DIR}/rdf-geosparql-distance-smoke.rq"
STRDF_QUERY="${TMP_DIR}/rdf-strdf-smoke.rq"

cat > "${TOPOLOGY_QUERY}" <<'RQ'
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX geof: <http://www.opengis.net/def/function/geosparql/>
SELECT
  (geof:sfWithin("POINT(0.5 0.5)"^^geo:wktLiteral, "POLYGON((0 0,0 1,1 1,1 0,0 0))"^^geo:wktLiteral) AS ?within)
  (geof:sfTouches("POLYGON((0 0,0 1,1 1,1 0,0 0))"^^geo:wktLiteral, "POLYGON((1 0,1 1,2 1,2 0,1 0))"^^geo:wktLiteral) AS ?touches)
  (geof:sfContains("POLYGON((0 0,0 1,1 1,1 0,0 0))"^^geo:wktLiteral, "POINT(0.5 0.5)"^^geo:wktLiteral) AS ?contains)
  (geof:sfIntersects("POINT(0.5 0.5)"^^geo:wktLiteral, "POLYGON((0 0,0 1,1 1,1 0,0 0))"^^geo:wktLiteral) AS ?intersects)
WHERE {}
RQ

cat > "${DISTANCE_QUERY}" <<'RQ'
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX geof: <http://www.opengis.net/def/function/geosparql/>
PREFIX uom: <http://www.opengis.net/def/uom/OGC/1.0/>
SELECT (geof:distance("POINT(0 0)"^^geo:wktLiteral, "POINT(0 1)"^^geo:wktLiteral, uom:metre) AS ?dist) WHERE {}
RQ

cat > "${STRDF_QUERY}" <<'RQ'
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX strdf: <http://strdf.di.uoa.gr/ontology#>
SELECT (strdf:area("POLYGON((0 0,0 1,1 1,1 0,0 0))"^^geo:wktLiteral) AS ?area) WHERE {}
RQ

echo "== Jena GeoSPARQL topology =="
"${ROOT_DIR}/benchmark/scripts/rdf-sparql-jena.sh" "${TOPOLOGY_QUERY}" || true
echo

echo "== Jena GeoSPARQL distance =="
"${ROOT_DIR}/benchmark/scripts/rdf-sparql-jena.sh" "${DISTANCE_QUERY}" || true
echo

echo "== Jena stSPARQL strdf:area =="
"${ROOT_DIR}/benchmark/scripts/rdf-sparql-jena.sh" "${STRDF_QUERY}" || true
echo

echo "== Virtuoso GeoSPARQL topology =="
"${ROOT_DIR}/benchmark/scripts/rdf-sparql-virtuoso.sh" "${TOPOLOGY_QUERY}" || true
echo

echo "== Virtuoso GeoSPARQL distance =="
"${ROOT_DIR}/benchmark/scripts/rdf-sparql-virtuoso.sh" "${DISTANCE_QUERY}" || true
echo

echo "== Virtuoso stSPARQL strdf:area =="
"${ROOT_DIR}/benchmark/scripts/rdf-sparql-virtuoso.sh" "${STRDF_QUERY}" || true
