#!/usr/bin/env bash
set -euo pipefail

TIMEOUT_SECONDS="${STRABON_STATEMENT_TIMEOUT_SECONDS:-60}"
CONTAINER="${STRABON_POSTGIS_CONTAINER:-yago2geo-strabon-postgis}"
USER_NAME="${STRABON_USER:-postgres}"
DBS_CSV="${STRABON_TIMEOUT_DBS:-endpoint,yago2geo_uk_subset}"

IFS=',' read -r -a DBS <<< "${DBS_CSV}"

echo "== Setting Strabon PostGIS statement_timeout=${TIMEOUT_SECONDS}s =="

for db in "${DBS[@]}"; do
    db="${db//[[:space:]]/}"
    if [[ -z "${db}" ]]; then
        continue
    fi

    exists="$(
        docker exec "${CONTAINER}" psql -U "${USER_NAME}" -d postgres -At \
            -c "SELECT 1 FROM pg_database WHERE datname = '${db//\'/\'\'}';" 2>/dev/null || true
    )"
    if [[ "${exists}" != "1" ]]; then
        echo "skip missing database: ${db}"
        continue
    fi

    docker exec "${CONTAINER}" psql -U "${USER_NAME}" -d postgres -v ON_ERROR_STOP=1 \
        -c "ALTER DATABASE \"${db//\"/\"\"}\" SET statement_timeout = '${TIMEOUT_SECONDS}s';"
    echo "statement_timeout set for database: ${db}"
done

echo "Note: ALTER DATABASE affects new DB sessions. Start or restart the Strabon endpoint after this step."
