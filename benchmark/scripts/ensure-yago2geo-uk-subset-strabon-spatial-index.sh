#!/usr/bin/env bash
set -euo pipefail

docker exec yago2geo-strabon-postgis psql -U postgres -d yago2geo_uk_subset -v ON_ERROR_STOP=1 -c \
    "CREATE INDEX IF NOT EXISTS geoindex ON public.geo_values USING gist (strdfgeo); ANALYZE public.geo_values;"
