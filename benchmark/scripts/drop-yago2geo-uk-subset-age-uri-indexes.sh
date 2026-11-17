#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GRAPH_SCHEMA="${AGE_GRAPH_SCHEMA:-yago2geo_uk_subset}"
PSQL="${ROOT_DIR}/benchmark/scripts/psql.sh"

echo "== Dropping AGE URI property indexes for ${GRAPH_SCHEMA} =="

"${PSQL}" -X -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT schemaname, indexname
        FROM pg_indexes
        WHERE schemaname = '${GRAPH_SCHEMA}'
          AND (indexname LIKE 'age_uri_%' OR indexname LIKE 'age_yago_uri_%')
        ORDER BY indexname
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %I.%I', r.schemaname, r.indexname);
    END LOOP;
END
\$\$;
SQL

echo "== Remaining AGE URI indexes =="
"${PSQL}" -X -q -A -F $'\t' -P footer=off <<SQL
SELECT count(*) AS remaining_uri_indexes
FROM pg_indexes
WHERE schemaname = '${GRAPH_SCHEMA}'
  AND (indexname LIKE 'age_uri_%' OR indexname LIKE 'age_yago_uri_%');
SQL
