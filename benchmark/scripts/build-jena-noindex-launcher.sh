#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JENA_VERSION="${JENA_VERSION:-5.1.0}"
JENA_IMAGE="${JENA_IMAGE:-age-spatial-jena-geosparql:${JENA_VERSION}}"
SRC="${ROOT_DIR}/docker/rdf-benchmark/jena-geosparql/NoSpatialIndexFuseki.java"
OUT_DIR="${ROOT_DIR}/benchmark/results/rdfdb/jena_noindex"
JAR="${JENA_GEOSPARQL_JAR:-${OUT_DIR}/jena-fuseki-geosparql.jar}"

if ! command -v javac >/dev/null 2>&1; then
    echo "javac is required to build NoSpatialIndexFuseki" >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"

if [[ ! -f "${JAR}" ]]; then
    tmp_container="$(docker create "${JENA_IMAGE}")"
    trap 'docker rm -f "${tmp_container}" >/dev/null 2>&1 || true' EXIT
    docker cp "${tmp_container}:/opt/jena/jena-fuseki-geosparql.jar" "${JAR}"
    docker rm "${tmp_container}" >/dev/null
    trap - EXIT
fi

javac -cp "${JAR}" -d "${OUT_DIR}" "${SRC}"
echo "Built NoSpatialIndexFuseki in ${OUT_DIR}"
