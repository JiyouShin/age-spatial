#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GRAPH_SCHEMA="${AGE_GRAPH_SCHEMA:-yago2geo_uk_subset}"
PSQL="${ROOT_DIR}/benchmark/scripts/psql.sh"

echo "== Creating AGE URI property indexes for ${GRAPH_SCHEMA} =="

"${PSQL}" -X -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$
DECLARE
    r record;
    uri_idx text;
    yago_uri_idx text;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = '${GRAPH_SCHEMA}'
          AND c.relkind = 'r'
          AND c.relname NOT LIKE '\_ag\_label\_%' ESCAPE '\'
          AND a.attname = 'properties'
          AND NOT a.attisdropped
        ORDER BY c.relname
    LOOP
        uri_idx := 'age_uri_' || substr(md5('${GRAPH_SCHEMA}.' || r.relname || '.uri'), 1, 24);
        yago_uri_idx := 'age_yago_uri_' || substr(md5('${GRAPH_SCHEMA}.' || r.relname || '.yago_uri'), 1, 19);

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I.%I (ag_catalog.agtype_access_operator(properties, %L::agtype))',
            uri_idx,
            '${GRAPH_SCHEMA}',
            r.relname,
            '"uri"'
        );

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I.%I (ag_catalog.agtype_access_operator(properties, %L::agtype))',
            yago_uri_idx,
            '${GRAPH_SCHEMA}',
            r.relname,
            '"yago_uri"'
        );

        EXECUTE format('ANALYZE %I.%I', '${GRAPH_SCHEMA}', r.relname);
    END LOOP;
END
\$\$;
SQL

echo "== AGE URI indexes =="
"${PSQL}" -X -q -A -F $'\t' -P footer=off <<SQL
SELECT tablename, indexname
FROM pg_indexes
WHERE schemaname = '${GRAPH_SCHEMA}'
  AND (indexname LIKE 'age_uri_%' OR indexname LIKE 'age_yago_uri_%')
ORDER BY tablename, indexname;
SQL
