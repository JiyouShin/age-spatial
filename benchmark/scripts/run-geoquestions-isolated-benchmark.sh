#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGE_COMPOSE="${ROOT_DIR}/docker/benchmark/docker-compose.yml"
RDF_COMPOSE="${ROOT_DIR}/docker/rdf-benchmark/docker-compose.yml"
RUNNER="${ROOT_DIR}/benchmark/scripts/run-geoquestions-full-benchmark.py"

ENGINE="${1:-}"
if [[ -z "${ENGINE}" ]]; then
    echo "Usage: $0 {age|jena|virtuoso|strabon}" >&2
    echo "" >&2
    echo "Environment options:" >&2
    echo "  RUN_LABEL=noindex TIMEOUT=60 FLUSH_EVERY=10 LIMIT=0 IDS=''" >&2
    echo "  WARMUP=1 WARMUP_LIMIT=0 STOP_AFTER=0" >&2
    exit 2
fi

RUN_LABEL="${RUN_LABEL:-noindex}"
TIMEOUT="${TIMEOUT:-60}"
FLUSH_EVERY="${FLUSH_EVERY:-10}"
LIMIT="${LIMIT:-0}"
IDS="${IDS:-}"
WARMUP="${WARMUP:-1}"
WARMUP_LIMIT="${WARMUP_LIMIT:-0}"
STOP_AFTER="${STOP_AFTER:-0}"
OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/benchmark/results/geoquestions/full_benchmark}"

case "${ENGINE}" in
    age|jena|virtuoso|strabon) ;;
    *)
        echo "Unsupported engine: ${ENGINE}" >&2
        echo "Use one of: age, jena, virtuoso, strabon" >&2
        exit 2
        ;;
esac

runner_args=(
    "--engines" "${ENGINE}"
    "--timeout" "${TIMEOUT}"
    "--flush-every" "${FLUSH_EVERY}"
)
if [[ "${LIMIT}" != "0" ]]; then
    runner_args+=("--limit" "${LIMIT}")
fi
if [[ -n "${IDS}" ]]; then
    runner_args+=("--ids" "${IDS}")
fi

wait_container_running() {
    local container="$1"
    local timeout="${2:-120}"
    local elapsed=0
    while (( elapsed < timeout )); do
        if [[ "$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || true)" == "true" ]]; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo "Container did not become running: ${container}" >&2
    return 1
}

wait_container_healthy_or_running() {
    local container="$1"
    local timeout="${2:-180}"
    local elapsed=0
    while (( elapsed < timeout )); do
        local running
        running="$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || true)"
        local health
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container}" 2>/dev/null || true)"
        if [[ "${health}" == "healthy" || ( "${health}" == "none" && "${running}" == "true" ) ]]; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo "Container did not become healthy/running: ${container}" >&2
    return 1
}

wait_http() {
    local url="$1"
    local timeout="${2:-180}"
    local elapsed=0
    while (( elapsed < timeout )); do
        if python3 -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=3).read(1)" "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "HTTP endpoint did not become ready: ${url}" >&2
    return 1
}

stop_experiment_dbs() {
    echo "== Stopping experiment DB containers except ${ENGINE} =="
    case "${ENGINE}" in
        age)
            docker compose -f "${RDF_COMPOSE}" stop jena virtuoso strabon strabon-postgis >/dev/null || true
            ;;
        jena)
            docker compose -f "${AGE_COMPOSE}" stop age-spatial-pg >/dev/null || true
            docker compose -f "${RDF_COMPOSE}" stop virtuoso strabon strabon-postgis >/dev/null || true
            ;;
        virtuoso)
            docker compose -f "${AGE_COMPOSE}" stop age-spatial-pg >/dev/null || true
            docker compose -f "${RDF_COMPOSE}" stop jena strabon strabon-postgis >/dev/null || true
            ;;
        strabon)
            docker compose -f "${AGE_COMPOSE}" stop age-spatial-pg >/dev/null || true
            docker compose -f "${RDF_COMPOSE}" stop jena virtuoso >/dev/null || true
            ;;
    esac
}

start_engine() {
    echo "== Starting ${ENGINE} only =="
    case "${ENGINE}" in
        age)
            docker compose -f "${AGE_COMPOSE}" up -d age-spatial-pg
            wait_container_healthy_or_running "age-spatial-pg16" 180
            ;;
        jena)
            docker compose -f "${RDF_COMPOSE}" up -d jena
            wait_container_running "yago2geo-jena-fuseki" 180
            wait_http "http://127.0.0.1:${JENA_PORT:-13030}/yago2geo/sparql" 180
            ;;
        virtuoso)
            docker compose -f "${RDF_COMPOSE}" up -d virtuoso
            wait_container_healthy_or_running "yago2geo-virtuoso" 240
            wait_http "http://127.0.0.1:${VIRTUOSO_HTTP_PORT:-18890}/sparql" 180
            ;;
        strabon)
            docker compose -f "${RDF_COMPOSE}" up -d strabon-postgis strabon
            wait_container_healthy_or_running "yago2geo-strabon-postgis" 240
            wait_container_running "yago2geo-strabon" 240
            wait_http "http://127.0.0.1:${STRABON_PORT:-18080}/Strabon" 240
            ;;
    esac
}

run_phase() {
    local phase="$1"
    local label="${RUN_LABEL}_${phase}"
    local output="${OUTPUT_DIR}/geoquestions_full_${ENGINE}_${label}_results.csv"
    local phase_args=("${runner_args[@]}" "--run-label" "${label}" "--output" "${output}")

    if [[ "${phase}" == "warmup" && "${WARMUP_LIMIT}" != "0" ]]; then
        phase_args+=("--limit" "${WARMUP_LIMIT}")
    fi

    echo "== ${ENGINE} ${phase} run =="
    echo "output=${output}"
    python3 "${RUNNER}" "${phase_args[@]}"
}

stop_experiment_dbs
start_engine

if [[ "${WARMUP}" == "1" ]]; then
    run_phase "warmup"
else
    echo "== Skipping warm-up run =="
fi

run_phase "measured"

if [[ "${STOP_AFTER}" == "1" ]]; then
    echo "== Stopping ${ENGINE} after measured run =="
    case "${ENGINE}" in
        age) docker compose -f "${AGE_COMPOSE}" stop age-spatial-pg ;;
        jena) docker compose -f "${RDF_COMPOSE}" stop jena ;;
        virtuoso) docker compose -f "${RDF_COMPOSE}" stop virtuoso ;;
        strabon) docker compose -f "${RDF_COMPOSE}" stop strabon strabon-postgis ;;
    esac
fi

echo "== Done: ${ENGINE} ${RUN_LABEL} isolated benchmark =="
