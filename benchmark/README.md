# AGE Spatial Benchmark

This directory is mounted into the benchmark container at `/benchmark`.

## Files

- `queries/`: SQL files for smoke tests and benchmark workloads.
- `scripts/`: host-side helper scripts that call `docker compose`.
- `results/`: captured query outputs.

## Typical Workflow

From the repository root:

```bash
docker compose -f docker/benchmark/docker-compose.yml up -d --build
benchmark/scripts/run-smoke.sh
benchmark/scripts/psql.sh
```

Every query file should include the AGE session setup:

```sql
LOAD 'age';
SET search_path = ag_catalog, public;
```

For benchmark queries, prefer:

```sql
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON)
...
```

JSON plans are easier to parse and summarize for paper tables.
