#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGE_COMPOSE="${ROOT_DIR}/docker/benchmark/docker-compose.yml"
RDF_COMPOSE="${ROOT_DIR}/docker/rdf-benchmark/docker-compose.yml"
RUNNER="${ROOT_DIR}/benchmark/scripts/run-geoquestions-full-benchmark.py"

usage() {
    cat >&2 <<EOF
Usage:
  $0 {age|jena|virtuoso|strabon} [options]
  $0 --engine {age|jena|virtuoso|strabon} [options]

Runs the UK GeoQuestions indexed benchmark for exactly one selected DB.
The other benchmark DB containers are stopped before the selected DB is run.
Result and statistics CSV files are checkpointed every --flush-every questions,
and progress is printed to the terminal by the Python runner.

Options:
  --input PATH                Input UK question CSV
  --output-dir DIR            Result directory
  --run-label LABEL           Label used in output filenames [uk_indexed]
  --timeout SEC               Per-query timeout [60]
  --flush-every N             Rewrite result CSV every N queries [10]
  --limit N                   Run only first N questions [0 = all]
  --ids LIST                  Comma-separated question ids, e.g. 138,145
  --warmup                    Run warm-up before measured run [default]
  --no-warmup                 Skip warm-up
  --warmup-limit N            Limit warm-up questions [0 = same as measured]
  --resume                    Skip rows already present in output CSV
  --stop-after                Stop the selected DB after measured run
  --age-session-mode MODE     AGE execution mode: persistent|per-query [persistent]
  --ensure-age-uri-indexes    Create AGE uri/yago_uri property indexes before AGE run
  --strabon-mode MODE         Strabon execution mode: endpoint|cmd [endpoint]
  --strabon-endpoint URL      Strabon HTTP endpoint [http://127.0.0.1:18080/Strabon/Query]
  --strabon-cleanup-on-timeout
                              Terminate Strabon PostGIS backend queries after timeout
  --no-strabon-restart-on-timeout
                              Do not restart Strabon endpoint after timeout cleanup
  --strabon-server-timeout SEC
                              Set PostGIS statement_timeout before starting Strabon [0 = skip]
  --virtuoso-query-file PATH  Virtuoso mixed query CSV
  -h, --help                  Show this help

Examples:
  $0 age --no-warmup --timeout 60
  $0 jena --warmup-limit 10 --flush-every 10 --resume
  $0 virtuoso --ids 138,145 --no-warmup
EOF
}

ENGINE=""
RUN_LABEL="${RUN_LABEL:-uk_indexed}"
TIMEOUT="${TIMEOUT:-60}"
FLUSH_EVERY="${FLUSH_EVERY:-10}"
LIMIT="${LIMIT:-0}"
IDS="${IDS:-}"
WARMUP="${WARMUP:-1}"
WARMUP_LIMIT="${WARMUP_LIMIT:-0}"
STOP_AFTER="${STOP_AFTER:-0}"
RESUME="${RESUME:-0}"
AGE_SESSION_MODE="${AGE_SESSION_MODE:-persistent}"
ENSURE_AGE_URI_INDEXES="${ENSURE_AGE_URI_INDEXES:-0}"
STRABON_MODE="${STRABON_MODE:-endpoint}"
STRABON_ENDPOINT="${STRABON_ENDPOINT:-http://127.0.0.1:${STRABON_PORT:-18080}/Strabon/Query}"
STRABON_CLEANUP_ON_TIMEOUT="${STRABON_CLEANUP_ON_TIMEOUT:-0}"
STRABON_RESTART_ON_TIMEOUT="${STRABON_RESTART_ON_TIMEOUT:-1}"
STRABON_SERVER_TIMEOUT="${STRABON_SERVER_TIMEOUT:-0}"
INPUT="${INPUT:-${ROOT_DIR}/benchmark/results/geoquestions/age_spatial_full/geoquestions_age_spatial_full_uk_only_questions.csv}"
OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/benchmark/results/geoquestions/uk_indexed_benchmark}"
VIRTUOSO_QUERY_FILE="${VIRTUOSO_QUERY_FILE:-${ROOT_DIR}/benchmark/results/geoquestions/virtuoso_bif_uk/geoquestions_uk_virtuoso_mixed_queries.csv}"

if [[ $# -eq 0 ]]; then
    usage
    exit 2
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        age|jena|virtuoso|strabon)
            ENGINE="$1"
            shift
            ;;
        --engine)
            ENGINE="${2:?--engine requires a value}"
            shift 2
            ;;
        --input)
            INPUT="${2:?--input requires a path}"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="${2:?--output-dir requires a path}"
            shift 2
            ;;
        --run-label)
            RUN_LABEL="${2:?--run-label requires a value}"
            shift 2
            ;;
        --timeout)
            TIMEOUT="${2:?--timeout requires seconds}"
            shift 2
            ;;
        --flush-every)
            FLUSH_EVERY="${2:?--flush-every requires a number}"
            shift 2
            ;;
        --limit)
            LIMIT="${2:?--limit requires a number}"
            shift 2
            ;;
        --ids)
            IDS="${2:?--ids requires a comma-separated list}"
            shift 2
            ;;
        --warmup)
            WARMUP=1
            shift
            ;;
        --no-warmup)
            WARMUP=0
            shift
            ;;
        --warmup-limit)
            WARMUP_LIMIT="${2:?--warmup-limit requires a number}"
            shift 2
            ;;
        --resume)
            RESUME=1
            shift
            ;;
        --stop-after)
            STOP_AFTER=1
            shift
            ;;
        --age-session-mode)
            AGE_SESSION_MODE="${2:?--age-session-mode requires persistent or per-query}"
            shift 2
            ;;
        --ensure-age-uri-indexes)
            ENSURE_AGE_URI_INDEXES=1
            shift
            ;;
        --strabon-mode)
            STRABON_MODE="${2:?--strabon-mode requires endpoint or cmd}"
            shift 2
            ;;
        --strabon-endpoint)
            STRABON_ENDPOINT="${2:?--strabon-endpoint requires a URL}"
            shift 2
            ;;
        --strabon-cleanup-on-timeout)
            STRABON_CLEANUP_ON_TIMEOUT=1
            shift
            ;;
        --no-strabon-restart-on-timeout)
            STRABON_RESTART_ON_TIMEOUT=0
            shift
            ;;
        --strabon-server-timeout)
            STRABON_SERVER_TIMEOUT="${2:?--strabon-server-timeout requires seconds}"
            shift 2
            ;;
        --virtuoso-query-file)
            VIRTUOSO_QUERY_FILE="${2:?--virtuoso-query-file requires a path}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option or engine: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "${ENGINE}" ]]; then
    echo "Missing engine." >&2
    usage
    exit 2
fi

case "${ENGINE}" in
    age|jena|virtuoso|strabon) ;;
    *)
        echo "Unsupported engine: ${ENGINE}" >&2
        echo "Use one of: age, jena, virtuoso, strabon" >&2
        exit 2
        ;;
esac

build_runner_args() {
    runner_args=(
        "--input" "${INPUT}"
        "--engines" "${ENGINE}"
        "--timeout" "${TIMEOUT}"
        "--flush-every" "${FLUSH_EVERY}"
        "--age-graph" "yago2geo_uk_subset"
        "--jena-endpoint" "http://127.0.0.1:${JENA_PORT:-13030}/yago2geo_uk_subset/sparql"
        "--virtuoso-endpoint" "http://127.0.0.1:${VIRTUOSO_HTTP_PORT:-18890}/sparql"
        "--virtuoso-graph" "http://yago-knowledge.org/graph/yago2geo_uk_subset"
        "--virtuoso-query-file" "${VIRTUOSO_QUERY_FILE}"
        "--strabon-endpoint" "${STRABON_ENDPOINT}"
        "--strabon-mode" "${STRABON_MODE}"
        "--strabon-db" "yago2geo_uk_subset"
        "--strabon-container" "yago2geo-strabon"
        "--age-session-mode" "${AGE_SESSION_MODE}"
    )
    if [[ "${STRABON_CLEANUP_ON_TIMEOUT}" == "1" ]]; then
        runner_args+=(
            "--strabon-cleanup-on-timeout"
            "--strabon-postgis-container" "yago2geo-strabon-postgis"
            "--strabon-cleanup-dbs" "endpoint,yago2geo_uk_subset"
        )
        if [[ "${STRABON_RESTART_ON_TIMEOUT}" == "1" ]]; then
            runner_args+=("--strabon-restart-on-timeout")
        fi
    fi
    if [[ "${LIMIT}" != "0" ]]; then
        runner_args+=("--limit" "${LIMIT}")
    fi
    if [[ -n "${IDS}" ]]; then
        runner_args+=("--ids" "${IDS}")
    fi
    if [[ "${RESUME}" == "1" ]]; then
        runner_args+=("--resume")
    fi
}

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

wait_sparql_post() {
    local url="$1"
    local timeout="${2:-180}"
    local elapsed=0
    while (( elapsed < timeout )); do
        if python3 -c '
import sys
import urllib.parse
import urllib.request

data = urllib.parse.urlencode({"query": "ASK { ?s ?p ?o }"}).encode()
request = urllib.request.Request(
    sys.argv[1],
    data=data,
    headers={"Accept": "application/sparql-results+json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=5) as response:
    if 200 <= response.status < 300:
        response.read(1)
' "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "SPARQL endpoint did not become ready: ${url}" >&2
    return 1
}

wait_strabon_post() {
    local url="$1"
    local timeout="${2:-360}"
    local elapsed=0
    while (( elapsed < timeout )); do
        if python3 -c '
import sys
import urllib.parse
import urllib.request

data = urllib.parse.urlencode({"query": "ASK WHERE { }"}).encode()
request = urllib.request.Request(
    sys.argv[1],
    data=data,
    headers={
        "Accept": "application/sparql-results+xml",
        "Content-Type": "application/x-www-form-urlencoded",
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=10) as response:
    if 200 <= response.status < 300:
        response.read(1)
' "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    echo "Strabon SPARQL endpoint did not become ready: ${url}" >&2
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
    echo "== Starting ${ENGINE} indexed UK setup =="
    case "${ENGINE}" in
        age)
            docker compose -f "${AGE_COMPOSE}" up -d age-spatial-pg
            wait_container_healthy_or_running "age-spatial-pg16" 180
            if [[ "${ENSURE_AGE_URI_INDEXES}" == "1" ]]; then
                "${ROOT_DIR}/benchmark/scripts/ensure-yago2geo-uk-subset-age-uri-indexes.sh"
            fi
            ;;
        jena)
            "${ROOT_DIR}/benchmark/scripts/start-yago2geo-uk-subset-jena-indexed.sh"
            wait_container_running "yago2geo-jena-fuseki" 180
            wait_sparql_post "http://127.0.0.1:${JENA_PORT:-13030}/yago2geo_uk_subset/sparql" 240
            ;;
        virtuoso)
            docker compose -f "${RDF_COMPOSE}" up -d virtuoso
            wait_container_healthy_or_running "yago2geo-virtuoso" 240
            wait_http "http://127.0.0.1:${VIRTUOSO_HTTP_PORT:-18890}/sparql" 180
            ;;
        strabon)
            docker compose -f "${RDF_COMPOSE}" up -d strabon-postgis
            wait_container_healthy_or_running "yago2geo-strabon-postgis" 240
            if [[ "${STRABON_SERVER_TIMEOUT}" != "0" ]]; then
                STRABON_STATEMENT_TIMEOUT_SECONDS="${STRABON_SERVER_TIMEOUT}" \
                    "${ROOT_DIR}/benchmark/scripts/ensure-yago2geo-uk-subset-strabon-timeout.sh"
            fi
            docker compose -f "${RDF_COMPOSE}" stop strabon >/dev/null || true
            docker compose -f "${RDF_COMPOSE}" up -d strabon
            wait_container_running "yago2geo-strabon" 240
            if [[ "${STRABON_MODE}" == "endpoint" ]]; then
                wait_strabon_post "${STRABON_ENDPOINT}" 360
            fi
            ;;
    esac
}

run_phase() {
    local phase="$1"
    local label="${RUN_LABEL}_${phase}"
    local output="${OUTPUT_DIR}/geoquestions_uk_${ENGINE}_${label}_results.csv"
    local phase_args=("${runner_args[@]}" "--run-label" "${label}" "--output" "${output}")

    if [[ "${phase}" == "warmup" && "${WARMUP_LIMIT}" != "0" ]]; then
        phase_args+=("--limit" "${WARMUP_LIMIT}")
    fi

    echo "== ${ENGINE} ${phase} run =="
    echo "output=${output}"
    python3 "${RUNNER}" "${phase_args[@]}"
}

run_selected_engine() {
    build_runner_args
    stop_experiment_dbs
    start_engine

    echo "== Benchmark configuration =="
    echo "engine=${ENGINE}"
    echo "input=${INPUT}"
    echo "output_dir=${OUTPUT_DIR}"
    echo "run_label=${RUN_LABEL}"
    echo "timeout=${TIMEOUT}"
    echo "flush_every=${FLUSH_EVERY}"
    echo "limit=${LIMIT}"
    echo "ids=${IDS}"
    echo "warmup=${WARMUP}"
    echo "warmup_limit=${WARMUP_LIMIT}"
    echo "resume=${RESUME}"
    echo "age_session_mode=${AGE_SESSION_MODE}"
    echo "ensure_age_uri_indexes=${ENSURE_AGE_URI_INDEXES}"
    echo "strabon_mode=${STRABON_MODE}"
    echo "strabon_endpoint=${STRABON_ENDPOINT}"
    echo "strabon_cleanup_on_timeout=${STRABON_CLEANUP_ON_TIMEOUT}"
    echo "strabon_restart_on_timeout=${STRABON_RESTART_ON_TIMEOUT}"
    echo "strabon_server_timeout=${STRABON_SERVER_TIMEOUT}"

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

    echo "== Done: ${ENGINE} ${RUN_LABEL} UK indexed isolated benchmark =="
}

run_selected_engine
