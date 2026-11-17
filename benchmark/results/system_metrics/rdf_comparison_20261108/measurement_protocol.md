# RDF Engine System Metrics Protocol

## Scope

- Dataset: YAGO2Geo UK subset already loaded with each engine's spatial support enabled.
- Query set: the same 222 selected GeoQuestions1089 queries used for AGE-Spatial.
- Execution: one client, one full warm-up pass, followed by one measured pass.
- Timeout: 60 seconds per query.
- Isolation: only the engine under test was running among the four experimental systems.
- The current values are single-run pilot measurements. Paper results should use at least three measured repetitions and report the median and dispersion.

## Query latency and throughput

`run-geoquestions-full-benchmark.py` sends each SPARQL request with `urllib.request.urlopen()`. A `time.perf_counter()` interval starts immediately before the request and ends after the response body is read. Strabon uses the same interval and additionally normalizes its SPARQL XML response. Percentiles are calculated only from `OK` requests.

- Attempted QPS = 222 / measured workload wall time.
- Successful QPS = number of `OK` requests / measured workload wall time.
- The workload wall time includes failed requests, timeouts, and Strabon timeout recovery.

## Errors and answers

- Execution error: `ERROR` or `ERROR_*`, including Strabon's `ERROR_RESPONSE`.
- Timeout: request reached the 60-second limit.
- HTTP error: `HTTP_*` returned by the endpoint.
- Answer mismatch rate: mismatched answers / `OK` requests.

## CPU, memory, and I/O

`run-rdf-system-query-metrics.py` repeatedly calls `docker stats --no-stream` during the measured pass and waits one second after each snapshot. The effective interval also includes command time: approximately 2.5 seconds for one container and 4.5 seconds when the two Strabon containers are queried sequentially. CPU percentage follows Docker semantics, where 100% is one fully used CPU core. Incremental peak memory is the maximum measured container memory minus memory immediately before the measured pass.

- Jena: `yago2geo-jena-fuseki` container.
- Virtuoso: `yago2geo-virtuoso` container.
- Strabon: sum of `yago2geo-strabon` and `yago2geo-strabon-postgis`, because query processing spans the endpoint and PostgreSQL/PostGIS.

Block read/write values are deltas between the first and last Docker samples. A zero block-read delta can indicate that the dataset was served from the host page cache; it is not an engine-level buffer hit ratio.

For Strabon, later repetitions also capture `pg_stat_database` before and after the measured pass. Its PostgreSQL shared-buffer hit ratio is calculated as `delta(blks_hit) / (delta(blks_hit) + delta(blks_read))`, using the same definition as AGE-Spatial. Jena and Virtuoso do not expose an equivalent counter through their documented monitoring interfaces, so no value with the same name should be reported for those systems.

## Storage

- AGE-Spatial: graph tables, spatial metadata, GiST indexes, and URI indexes for the measured graph.
- Jena: the target TDB2 dataset directory, including its spatial index file.
- Strabon: `pg_database_size('endpoint')` in PostgreSQL.
- Virtuoso: the current directory contains multiple graphs, so its measured directory size is not a dataset-specific value and should not be used in a direct storage comparison.

## Native loading comparison

AGE-Spatial loading measures only pre-generated LPG CSV files and excludes RDF-to-LPG conversion. RDF engines load the original RDF files, so loading should be reported as a separate experiment rather than mixed with AGE vertex/edge loading.

- Jena: time `tdb2.tdbloader` into a new TDB2 directory; count loaded triples and raw RDF bytes.
- Virtuoso: time `ld_dir`, `rdf_loader_run()`, and `checkpoint` into a new graph; report spatial materialization/index construction separately.
- Strabon: time the Strabon `store` operation into a new PostgreSQL database until commit and index construction complete.

Common RDF loading measures should be triples/s and input MB/s. AGE-Spatial should additionally report RDF-to-LPG conversion time and CSV loading time as separate phases. Vertex/s and edge/s are LPG-specific and should not be compared directly with RDF triples/s.

## Reproduction

```bash
python3 benchmark/scripts/run-rdf-system-query-metrics.py \
  --engine jena \
  --output-dir benchmark/results/system_metrics/rdf_comparison_20261108/jena \
  --stop-after

python3 benchmark/scripts/run-rdf-system-query-metrics.py \
  --engine virtuoso \
  --output-dir benchmark/results/system_metrics/rdf_comparison_20261108/virtuoso \
  --stop-after

python3 benchmark/scripts/run-rdf-system-query-metrics.py \
  --engine strabon \
  --output-dir benchmark/results/system_metrics/rdf_comparison_20261108/strabon \
  --stop-after
```
