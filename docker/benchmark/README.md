# AGE Spatial Benchmark Docker Environment

This directory contains a fixed PostgreSQL/PostGIS/AGE Spatial environment for
server-side benchmark experiments.

## Versions

- PostgreSQL: `postgres:16.4-bookworm`
- PostGIS: built from source, default `3.6.4`
- AGE Spatial: built from the current repository checkout

## Start

Run from the repository root:

```bash
docker compose -f docker/benchmark/docker-compose.yml up -d --build
```

The database is exposed on localhost port `55432`.

```text
host: 127.0.0.1
port: 55432
database: age_spatial_bench
user: postgres
password: postgres
```

## Smoke Test

```bash
benchmark/scripts/run-smoke.sh
```

## Open psql

```bash
benchmark/scripts/psql.sh
```

## Run a Query File

```bash
benchmark/scripts/run-query.sh benchmark/queries/age_spatial_smoke.sql
```

## Capture Query Output

```bash
benchmark/scripts/capture-query.sh benchmark/queries/age_spatial_smoke.sql
```

Outputs are written under `benchmark/results/`.

## Stop

```bash
docker compose -f docker/benchmark/docker-compose.yml down
```

To delete the database volume as well:

```bash
docker compose -f docker/benchmark/docker-compose.yml down -v
```
