#!/usr/bin/env bash
set -euo pipefail

RDF_ROOT="${RDF_ROOT:-/rdf/yago2geo}"
DB_LOC="${JENA_DB_LOC:-/fuseki/databases/yago2geo}"

if ! command -v tdb2.tdbloader >/dev/null 2>&1; then
    export PATH="/opt/java/openjdk/bin:/jena/bin:${PATH}"
fi

if ! command -v tdb2.tdbloader >/dev/null 2>&1; then
    echo "tdb2.tdbloader not found in this image" >&2
    exit 1
fi

if [[ ! -d "${RDF_ROOT}" ]]; then
    echo "RDF root does not exist: ${RDF_ROOT}" >&2
    exit 1
fi

mapfile -t rdf_files < <(
    find "${RDF_ROOT}" -type f \( -name '*.nt' -o -name '*.ttl' -o -name '*.rdf' \) | sort
)

if [[ "${#rdf_files[@]}" -eq 0 ]]; then
    echo "No RDF files found under ${RDF_ROOT}" >&2
    exit 1
fi

echo "RDF files: ${#rdf_files[@]}"
du -ch "${rdf_files[@]}" | tail -n 1

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    printf '%s\n' "${rdf_files[@]}"
    exit 0
fi

echo "Resetting Jena TDB2 location: ${DB_LOC}"
rm -rf "${DB_LOC}"
mkdir -p "${DB_LOC}"

echo "Loading YAGO2Geo into Jena TDB2..."
tdb2.tdbloader --loc="${DB_LOC}" "${rdf_files[@]}"

echo "Jena TDB2 load finished: ${DB_LOC}"
