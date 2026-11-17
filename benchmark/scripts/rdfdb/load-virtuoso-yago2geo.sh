#!/usr/bin/env bash
set -euo pipefail

RDF_ROOT="${RDF_ROOT:-/database/rdf/yago2geo}"
GRAPH_IRI="${YAGO2GEO_GRAPH_IRI:-http://yago-knowledge.org/graph/yago2geo}"
DBA_PASSWORD="${DBA_PASSWORD:-dba}"
SQL_FILE="/tmp/load-yago2geo-virtuoso.sql"

if [[ ! -d "${RDF_ROOT}" ]]; then
    echo "RDF root does not exist: ${RDF_ROOT}" >&2
    exit 1
fi

mapfile -t rdf_dirs < <(
    find "${RDF_ROOT}" -type f \( -name '*.nt' -o -name '*.ttl' -o -name '*.rdf' \) \
        -printf '%h\n' | sort -u
)

if [[ "${#rdf_dirs[@]}" -eq 0 ]]; then
    echo "No RDF files found under ${RDF_ROOT}" >&2
    exit 1
fi

{
    echo "SET ECHO ON;"
    echo "log_enable(2);"
    echo "SPARQL CLEAR GRAPH <${GRAPH_IRI}>;"
    echo "DELETE FROM DB.DBA.load_list;"
    for dir in "${rdf_dirs[@]}"; do
        echo "ld_dir('${dir}', '*.nt', '${GRAPH_IRI}');"
        echo "ld_dir('${dir}', '*.ttl', '${GRAPH_IRI}');"
        echo "ld_dir('${dir}', '*.rdf', '${GRAPH_IRI}');"
    done
    echo "SELECT ll_file, ll_graph, ll_state FROM DB.DBA.load_list ORDER BY ll_file;"
    echo "rdf_loader_run();"
    echo "checkpoint;"
    echo "SELECT COUNT(*) AS triples FROM DB.DBA.RDF_QUAD WHERE g = iri_to_id('${GRAPH_IRI}');"
} > "${SQL_FILE}"

echo "Registered RDF directories: ${#rdf_dirs[@]}"
echo "Graph IRI: ${GRAPH_IRI}"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    cat "${SQL_FILE}"
    exit 0
fi

ISQL_BIN="$(command -v isql-v || command -v isql || true)"
if [[ -z "${ISQL_BIN}" ]]; then
    echo "isql-v/isql not found in this image" >&2
    exit 1
fi

echo "Waiting for Virtuoso..."
for _ in $(seq 1 120); do
    if "${ISQL_BIN}" 1111 dba "${DBA_PASSWORD}" "EXEC=SELECT 1;" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

if ! "${ISQL_BIN}" 1111 dba "${DBA_PASSWORD}" "EXEC=SELECT 1;" >/dev/null 2>&1; then
    echo "Virtuoso did not become ready" >&2
    exit 1
fi

echo "Loading YAGO2Geo into Virtuoso..."
"${ISQL_BIN}" 1111 dba "${DBA_PASSWORD}" "${SQL_FILE}"

echo "Virtuoso RDF load finished"
