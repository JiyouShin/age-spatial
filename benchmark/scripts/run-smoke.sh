#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

"${ROOT_DIR}/benchmark/scripts/run-query.sh" \
    "${ROOT_DIR}/benchmark/queries/age_spatial_smoke.sql"
