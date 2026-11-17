#!/usr/bin/env bash
set -euo pipefail

RDF_ROOT="${RDF_ROOT:-/rdf/yago2geo-jena-input}"
FALLBACK_RDF_ROOT="${FALLBACK_RDF_ROOT:-/rdf/yago2geo}"
STRABON_HOST="${STRABON_HOST:-strabon-postgis}"
STRABON_PORT="${STRABON_PORT:-5432}"
STRABON_DB="${STRABON_DB:-endpoint}"
STRABON_USER="${STRABON_USER:-postgres}"
STRABON_PASSWORD="${STRABON_PASSWORD:-postgres}"
STRABON_MEMORY_MULT="${STRABON_MEMORY_MULT:-16}"
STRABON_CMD="${STRABON_CMD:-/opt/strabon/scripts/strabon-cmd}"

if [[ ! -x "${STRABON_CMD}" ]]; then
    echo "Strabon command not found or not executable: ${STRABON_CMD}" >&2
    exit 1
fi

STRABON_CMD_DIR="$(cd "$(dirname "${STRABON_CMD}")" && pwd)"

if [[ ! -d "${RDF_ROOT}" ]] || ! find "${RDF_ROOT}" -type f \( -name '*.nt' -o -name '*.ttl' -o -name '*.rdf' \) -print -quit | grep -q .; then
    echo "Preferred RDF root is missing or empty: ${RDF_ROOT}" >&2
    echo "Falling back to: ${FALLBACK_RDF_ROOT}" >&2
    RDF_ROOT="${FALLBACK_RDF_ROOT}"
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

if [[ -n "${STRABON_START_FROM:-}" || -n "${STRABON_START_AFTER:-}" ]]; then
    resume_marker="${STRABON_START_FROM:-${STRABON_START_AFTER}}"
    resume_after=0
    if [[ -n "${STRABON_START_AFTER:-}" ]]; then
        resume_after=1
    fi

    found_resume_marker=0
    resume_files=()

    for file in "${rdf_files[@]}"; do
        if [[ "${found_resume_marker}" -eq 0 ]]; then
            if [[ "${file}" == "${resume_marker}" || "${file}" == */"${resume_marker}" ]]; then
                found_resume_marker=1
                if [[ "${resume_after}" -eq 1 ]]; then
                    continue
                fi
            else
                continue
            fi
        fi

        resume_files+=("${file}")
    done

    if [[ "${found_resume_marker}" -eq 0 ]]; then
        echo "Resume marker was not found under ${RDF_ROOT}: ${resume_marker}" >&2
        exit 1
    fi

    rdf_files=("${resume_files[@]}")
    echo "Resuming Strabon load from ${#rdf_files[@]} file(s)"
    if [[ "${resume_after}" -eq 1 ]]; then
        echo "Resume mode: after ${resume_marker}"
    else
        echo "Resume mode: from ${resume_marker}"
    fi
fi

echo "RDF root: ${RDF_ROOT}"
echo "RDF files: ${#rdf_files[@]}"
du -ch "${rdf_files[@]}" | tail -n 1

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    printf '%s\n' "${rdf_files[@]}"
    exit 0
fi

for file in "${rdf_files[@]}"; do
    case "${file}" in
        *.nt) format="ntriples" ;;
        *.ttl) format="turtle" ;;
        *.rdf) format="rdfxml" ;;
        *)
            echo "Skipping unsupported RDF file: ${file}" >&2
            continue
            ;;
    esac

    echo "Loading ${file} as ${format}..."
    log_file="$(mktemp)"
    (
        cd "${STRABON_CMD_DIR}"
        "${STRABON_CMD}" \
        -MM "${STRABON_MEMORY_MULT}" \
        -h "${STRABON_HOST}" \
        -p "${STRABON_PORT}" \
        -db "${STRABON_DB}" \
        -u "${STRABON_USER}" \
        -pass "${STRABON_PASSWORD}" \
        store -f "${format}" "${file}"
    ) 2>&1 | tee "${log_file}"

    if grep -Eq '\[Strabon\.StoreOp\] Error during store|java\.lang\.(Exception|NullPointerException)|Cannot connect to database' "${log_file}"; then
        echo "Strabon failed while loading ${file}. See log above." >&2
        rm -f "${log_file}"
        exit 1
    fi
    rm -f "${log_file}"
done

echo "Strabon RDF load finished"
