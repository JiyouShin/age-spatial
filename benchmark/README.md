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

## RDF Schema Profiling

To inspect YAGO2Geo-style RDF before choosing LPG labels and properties, run:

```bash
benchmark/scripts/extract-rdf-schema.py benchmark/data/extracted/yago2geo/usa/*.nt \
  -o benchmark/results/schema/yago2geo/usa

benchmark/scripts/extract-rdf-schema.py \
  benchmark/data/extracted/yago2geo/ireland/OSI_extended.ttl \
  benchmark/data/extracted/yago2geo/ireland/OSI_new.ttl \
  benchmark/data/extracted/yago2geo/ireland/OSI_matches.nt \
  benchmark/data/extracted/yago2geo/ireland/OSI_topological.nt \
  -o benchmark/results/schema/yago2geo/ireland

benchmark/scripts/extract-rdf-schema.py \
  benchmark/data/extracted/yago2geo/uk/os/OS_extended.ttl \
  benchmark/data/extracted/yago2geo/uk/os/OS_new.ttl \
  benchmark/data/extracted/yago2geo/uk/osni/OSNI_extended.ttl \
  benchmark/data/extracted/yago2geo/uk/osni/OSNI_new.ttl \
  benchmark/data/extracted/yago2geo/uk/os/OS_matches.nt \
  benchmark/data/extracted/yago2geo/uk/osni/OSNI_matches.nt \
  benchmark/data/extracted/yago2geo/uk/os/OS_topological.nt \
  benchmark/data/extracted/yago2geo/uk/osni/OSNI_topological.nt \
  -o benchmark/results/schema/yago2geo/uk

benchmark/scripts/extract-rdf-schema.py \
  benchmark/data/extracted/yago2geo/greece/GAG_extended.ttl \
  benchmark/data/extracted/yago2geo/greece/GAG_new.ttl \
  benchmark/data/extracted/yago2geo/greece/GAG_matches.nt \
  benchmark/data/extracted/yago2geo/greece/GAG_topological.nt \
  -o benchmark/results/schema/yago2geo/greece
```

The profiler writes:

- `schema_report.md`: human-readable candidate groups, predicates, shapes, and
  URI edge predicates.
- `groups.csv`: RDF-type, file, or URI-pattern-derived candidate labels and
  subject counts.
- `predicates_by_group.csv`: predicate coverage and literal type guesses per
  candidate label.
- `shapes.csv`: distinct predicate sets per candidate label.
- `uri_edges.csv`: URI-object predicates that may become LPG edges.
