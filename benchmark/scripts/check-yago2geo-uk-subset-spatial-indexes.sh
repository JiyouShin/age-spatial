#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "== Jena spatial index files =="
find "${ROOT_DIR}/benchmark/results/rdfdb/jena_data/databases/yago2geo_uk_subset" \
    -maxdepth 2 -type f \( -name 'spatial.index*' -o -name '*spatial*' \) -printf '%p\t%s bytes\n' \
    2>/dev/null || true

echo
echo "== Virtuoso RDF_GEO =="
docker exec yago2geo-virtuoso bash -lc 'cat >/tmp/virt_geo_check.sql <<'"'"'SQL'"'"'
SELECT COUNT(*) AS rdf_geo_rows FROM DB.DBA.RDF_GEO;
SELECT TOP 5 X, Y, X2, Y2, ID FROM DB.DBA.RDF_GEO;
SQL
ISQL=$(command -v isql-v || command -v isql)
"${ISQL}" 1111 dba "${DBA_PASSWORD:-dba}" /tmp/virt_geo_check.sql'

echo
echo "== Strabon PostGIS spatial indexes =="
docker exec yago2geo-strabon-postgis psql -U postgres -d yago2geo_uk_subset -At -c \
    "SELECT tablename || E'\t' || indexname || E'\t' || indexdef FROM pg_indexes WHERE schemaname='public' AND indexdef ILIKE '%gist%' ORDER BY tablename, indexname;"
