#!/usr/bin/env python3
"""Measure single-dataset AGE-Spatial system metrics.

The runner uses the existing UK GeoQuestions subset vertices together with the
full UK YAGO2Geo edges.  It separates database loading, GiST construction, URI
index construction, and query execution so their costs are not conflated.

Generated results include phase wall time, Docker CPU/memory/block-I/O samples,
PostgreSQL storage snapshots, query latency percentiles, sequential QPS, error
counts, and PostgreSQL shared-buffer hit ratio.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


# GeoQuestions rows contain complete query texts and can exceed Python's
# conservative 128 KiB CSV field default.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
PSQL = ROOT / "benchmark" / "scripts" / "psql.sh"
QUERY_RUNNER = ROOT / "benchmark" / "scripts" / "run-geoquestions-full-benchmark.py"
VERTEX_SOURCE = ROOT / "benchmark" / "results" / "load" / "yago2geo_uk_subset"
EDGE_SOURCE = ROOT / "benchmark" / "results" / "load" / "yago2geo" / "uk"
QUERY_INPUT = (
    ROOT
    / "benchmark"
    / "results"
    / "geoquestions"
    / "age_spatial_full"
    / "geoquestions_age_spatial_full_uk_only_questions.csv"
)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def run_command(
    command: Sequence[str],
    *,
    input_text: Optional[str] = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        cwd=str(ROOT),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=True,
    )


def run_psql(sql: str) -> str:
    proc = run_command(
        [str(PSQL), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"],
        input_text=sql,
    )
    return (proc.stdout or "").strip()


def parse_size(value: str) -> float:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", value)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    return number * factors.get(unit, 1)


def parse_pair(value: str) -> Tuple[float, float]:
    parts = [part.strip() for part in value.split("/")]
    if len(parts) != 2:
        return 0.0, 0.0
    return parse_size(parts[0]), parse_size(parts[1])


def parse_docker_stats(payload: str) -> Dict[str, float]:
    row = json.loads(payload)
    memory_used, memory_limit = parse_pair(row.get("MemUsage", "0B / 0B"))
    block_read, block_write = parse_pair(row.get("BlockIO", "0B / 0B"))
    cpu = float(row.get("CPUPerc", "0").strip().rstrip("%") or 0)
    return {
        "timestamp_epoch": time.time(),
        "cpu_percent": cpu,
        "memory_used_bytes": memory_used,
        "memory_limit_bytes": memory_limit,
        "block_read_bytes": block_read,
        "block_write_bytes": block_write,
    }


def docker_snapshot(container: str) -> Dict[str, float]:
    proc = run_command(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            container,
        ]
    )
    line = (proc.stdout or "").strip().splitlines()
    return parse_docker_stats(line[-1]) if line else {}


class DockerStatsSampler:
    def __init__(self, container: str, output: Path) -> None:
        self.container = container
        self.output = output
        self.samples: List[Dict[str, float]] = []
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()

    def _sample(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.samples.append(docker_snapshot(self.container))
            except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                pass
            self.stop_event.wait(1.0)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._write()

    def _write(self) -> None:
        fields = [
            "timestamp_epoch",
            "cpu_percent",
            "memory_used_bytes",
            "memory_limit_bytes",
            "block_read_bytes",
            "block_write_bytes",
        ]
        with self.output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.samples)


def timed_phase(
    name: str,
    container: str,
    output_dir: Path,
    action: Callable[[], Any],
) -> Tuple[Any, Dict[str, Any]]:
    idle = docker_snapshot(container)
    sampler = DockerStatsSampler(container, output_dir / ("docker_%s.csv" % name))
    sampler.start()
    started_ns = time.perf_counter_ns()
    status = "ok"
    detail = ""
    result: Any = None
    try:
        result = action()
    except Exception as exc:
        status = "error"
        detail = str(exc)
        raise
    finally:
        elapsed_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
        sampler.stop()

    samples = sampler.samples
    cpu_values = [row["cpu_percent"] for row in samples]
    memory_values = [row["memory_used_bytes"] for row in samples]
    first = samples[0] if samples else idle
    last = samples[-1] if samples else idle
    peak_memory = max(memory_values) if memory_values else idle.get("memory_used_bytes", 0)
    idle_memory = idle.get("memory_used_bytes", 0)
    metrics: Dict[str, Any] = {
        "phase": name,
        "status": status,
        "detail": detail,
        "elapsed_seconds": elapsed_seconds,
        "sample_count": len(samples),
        "idle_memory_bytes": idle_memory,
        "average_memory_bytes": statistics.fmean(memory_values) if memory_values else idle_memory,
        "peak_memory_bytes": peak_memory,
        "incremental_peak_memory_bytes": max(0.0, peak_memory - idle_memory),
        "average_cpu_percent": statistics.fmean(cpu_values) if cpu_values else 0.0,
        "peak_cpu_percent": max(cpu_values) if cpu_values else 0.0,
        "block_read_bytes": max(0.0, last.get("block_read_bytes", 0) - first.get("block_read_bytes", 0)),
        "block_write_bytes": max(0.0, last.get("block_write_bytes", 0) - first.get("block_write_bytes", 0)),
    }
    return result, metrics


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_load_statements(path: Path, function_prefix: str) -> List[str]:
    statements = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("SELECT ag_catalog.%s" % function_prefix):
            statements.append(stripped)
    return statements


def build_sql(graph: str, stage_dir: str, output_dir: Path) -> Dict[str, Path]:
    vertex_manifest = read_json(VERTEX_SOURCE / "manifest.json")
    edge_manifest = read_json(EDGE_SOURCE / "manifest.json")
    vertex_labels = sorted(vertex_manifest["vertex_counts"])
    edge_labels = sorted(edge_manifest["edge_counts"])

    setup_lines = [
        "LOAD 'age';",
        "SET search_path TO ag_catalog, public;",
        "SELECT ag_catalog.drop_graph(%s, true) WHERE EXISTS "
        "(SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s);"
        % (sql_literal(graph), sql_literal(graph)),
        "DELETE FROM ag_catalog.ag_spatial_columns WHERE graph_name = %s;"
        % sql_literal(graph),
        "SELECT ag_catalog.create_graph(%s);" % sql_literal(graph),
    ]
    setup_lines.extend(
        "SELECT ag_catalog.create_vlabel(%s, %s);"
        % (sql_literal(graph), sql_literal(label))
        for label in vertex_labels
    )
    setup_lines.extend(
        "SELECT ag_catalog.create_elabel(%s, %s);"
        % (sql_literal(graph), sql_literal(label))
        for label in edge_labels
    )

    vertex_statements = extract_load_statements(
        VERTEX_SOURCE / "load_yago2geo_uk_subset.sql", "load_labels"
    )
    vertex_statements = [
        statement.replace("'yago2geo_uk_subset'", sql_literal(graph))
        .replace("yago2geo_uk_subset/vertices", "%s/vertices" % stage_dir)
        .replace('"create_index": true', '"create_index": false')
        for statement in vertex_statements
    ]

    edge_statements = extract_load_statements(EDGE_SOURCE / "load_uk.sql", "load_edges")
    edge_statements = [
        statement.replace("'yago2geo_uk'", sql_literal(graph)).replace(
            "yago2geo/uk/edges", "%s/edges" % stage_dir
        )
        for statement in edge_statements
    ]

    gist_sql = """\
LOAD 'age';
SET search_path TO ag_catalog, public;
DO $$
DECLARE
    r record;
    v_index_name name;
BEGIN
    FOR r IN
        SELECT graph_name, label_name, relation_oid::regclass AS relation_name,
               column_name
        FROM ag_catalog.ag_spatial_columns
        WHERE graph_name = %s
        ORDER BY label_name, column_name
    LOOP
        v_index_name := left(lower(r.label_name::text || '_' ||
                                   r.column_name::text || '_gix'), 63)::name;
        EXECUTE format('CREATE INDEX %%I ON %%s USING gist (%%I)',
                       v_index_name, r.relation_name, r.column_name);
        UPDATE ag_catalog.ag_spatial_columns
        SET index_name = v_index_name
        WHERE graph_name = r.graph_name
          AND label_name = r.label_name
          AND column_name = r.column_name;
    END LOOP;
END
$$;
""" % sql_literal(graph)

    uri_sql = """\
LOAD 'age';
SET search_path TO ag_catalog, public;
DO $$
DECLARE
    r record;
    uri_idx text;
    yago_uri_idx text;
BEGIN
    FOR r IN
        SELECT l.name AS relname
        FROM ag_catalog.ag_label l
        JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
        WHERE g.name = %s AND l.kind = 'v'
        ORDER BY l.name
    LOOP
        uri_idx := 'age_uri_' || substr(md5(%s || '.' || r.relname || '.uri'), 1, 24);
        yago_uri_idx := 'age_yago_uri_' || substr(md5(%s || '.' || r.relname || '.yago_uri'), 1, 19);
        EXECUTE format(
            'CREATE INDEX %%I ON %%I.%%I (ag_catalog.agtype_access_operator(properties, %%L::agtype))',
            uri_idx, %s, r.relname, '"uri"'
        );
        EXECUTE format(
            'CREATE INDEX %%I ON %%I.%%I (ag_catalog.agtype_access_operator(properties, %%L::agtype))',
            yago_uri_idx, %s, r.relname, '"yago_uri"'
        );
    END LOOP;
END
$$;
""" % (
        sql_literal(graph),
        sql_literal(graph),
        sql_literal(graph),
        sql_literal(graph),
        sql_literal(graph),
    )

    files = {
        "setup": output_dir / "01_setup.sql",
        "vertices": output_dir / "02_load_vertices.sql",
        "edges": output_dir / "03_load_edges.sql",
        "gist": output_dir / "04_create_gist.sql",
        "uri": output_dir / "05_create_uri_indexes.sql",
        "analyze": output_dir / "06_analyze.sql",
    }
    files["setup"].write_text("\n".join(setup_lines) + "\n", encoding="utf-8")
    files["vertices"].write_text(
        "LOAD 'age';\nSET search_path TO ag_catalog, public;\n"
        + "\n".join(vertex_statements)
        + "\n",
        encoding="utf-8",
    )
    files["edges"].write_text(
        "LOAD 'age';\nSET search_path TO ag_catalog, public;\n"
        + "\n".join(edge_statements)
        + "\n",
        encoding="utf-8",
    )
    files["gist"].write_text(gist_sql, encoding="utf-8")
    files["uri"].write_text(uri_sql, encoding="utf-8")
    files["analyze"].write_text("ANALYZE;\n", encoding="utf-8")
    return files


def execute_sql_file(path: Path) -> str:
    return run_psql(path.read_text(encoding="utf-8"))


def count_rows(graph: str, labels: Iterable[str]) -> int:
    parts = [
        "SELECT count(*)::bigint AS n FROM %s.%s"
        % (sql_identifier(graph), sql_identifier(label))
        for label in labels
    ]
    sql = "SELECT COALESCE(sum(n), 0) FROM (%s) AS counts;" % " UNION ALL ".join(parts)
    return int(run_psql(sql).splitlines()[-1])


def storage_snapshot(graph: str) -> Dict[str, Any]:
    sql = """\
WITH graph_relations AS (
    SELECT c.oid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
), graph_sizes AS (
    SELECT
        COALESCE(sum(pg_table_size(oid)), 0)::bigint AS table_bytes,
        COALESCE(sum(pg_indexes_size(oid)), 0)::bigint AS index_bytes,
        COALESCE(sum(pg_total_relation_size(oid)), 0)::bigint AS total_bytes
    FROM graph_relations
), classified_indexes AS (
    SELECT
        COALESCE(sum(pg_relation_size(i.indexrelid))
            FILTER (WHERE am.amname = 'gist'), 0)::bigint AS gist_index_bytes,
        COALESCE(sum(pg_relation_size(i.indexrelid))
            FILTER (WHERE ic.relname LIKE 'age_uri_%%'
                     OR ic.relname LIKE 'age_yago_uri_%%'), 0)::bigint AS uri_index_bytes
    FROM pg_index i
    JOIN pg_class tc ON tc.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = tc.relnamespace
    JOIN pg_class ic ON ic.oid = i.indexrelid
    JOIN pg_am am ON am.oid = ic.relam
    WHERE n.nspname = %s
)
SELECT json_build_object(
    'physical_table_bytes', graph_sizes.table_bytes,
    'all_index_bytes', graph_sizes.index_bytes,
    'gist_index_bytes', classified_indexes.gist_index_bytes,
    'uri_index_bytes', classified_indexes.uri_index_bytes,
    'total_storage_bytes', graph_sizes.total_bytes,
    'metadata_catalog_bytes', pg_total_relation_size('ag_catalog.ag_spatial_columns'),
    'metadata_rows', (SELECT count(*) FROM ag_catalog.ag_spatial_columns
                      WHERE graph_name = %s)
)::text
FROM graph_sizes CROSS JOIN classified_indexes;
""" % (sql_literal(graph), sql_literal(graph), sql_literal(graph))
    return json.loads(run_psql(sql).splitlines()[-1])


def database_io_snapshot() -> Dict[str, int]:
    sql = """\
SELECT json_build_object(
    'blks_read', blks_read,
    'blks_hit', blks_hit,
    'temp_files', temp_files,
    'temp_bytes', temp_bytes
)::text
FROM pg_stat_database
WHERE datname = current_database();
"""
    return {key: int(value) for key, value in json.loads(run_psql(sql)).items()}


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def query_summary(path: Path, wall_seconds: float) -> Dict[str, Any]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    ok_rows = [row for row in rows if row.get("execution_status") == "OK"]
    latencies = [float(row["executiontime_ms"]) for row in ok_rows]
    status_counts: Dict[str, int] = {}
    match_counts: Dict[str, int] = {}
    for row in rows:
        status = row.get("execution_status", "")
        match = row.get("match_status", "")
        status_counts[status] = status_counts.get(status, 0) + 1
        match_counts[match] = match_counts.get(match, 0) + 1
    attempted = len(rows)
    successful = len(ok_rows)
    execution_errors = status_counts.get("ERROR", 0)
    timeouts = status_counts.get("TIMEOUT", 0)
    mismatches = match_counts.get("mismatch", 0)
    return {
        "attempted_queries": attempted,
        "successful_queries": successful,
        "query_wall_seconds": wall_seconds,
        "attempted_query_throughput_qps": attempted / wall_seconds if wall_seconds else 0.0,
        "successful_query_throughput_qps": successful / wall_seconds if wall_seconds else 0.0,
        "query_throughput_qps": successful / wall_seconds if wall_seconds else 0.0,
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "latency_p98_ms": percentile(latencies, 0.98),
        "latency_p99_ms": percentile(latencies, 0.99),
        "mean_latency_ms": statistics.fmean(latencies) if latencies else 0.0,
        "execution_error_rate": execution_errors / attempted if attempted else 0.0,
        "timeout_rate": timeouts / attempted if attempted else 0.0,
        "answer_mismatch_rate_among_executed": (
            mismatches / successful if successful else 0.0
        ),
        "execution_status_counts": status_counts,
        "match_status_counts": match_counts,
    }


def write_phase_metrics(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def checkpoint_progress(
    output_dir: Path,
    phases: Sequence[Dict[str, Any]],
    storage: Dict[str, Dict[str, Any]],
) -> None:
    """Persist completed phases so a later aggregation error loses no timings."""
    write_phase_metrics(output_dir / "phase_metrics.partial.csv", phases)
    (output_dir / "storage_snapshots.partial.json").write_text(
        json.dumps(storage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default="age_spatial_system_metrics")
    parser.add_argument("--container", default="age-spatial-pg16")
    parser.add_argument("--query-input", type=Path, default=QUERY_INPUT)
    parser.add_argument("--query-limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--skip-warmup", action="store_true")
    parser.add_argument("--skip-query", action="store_true")
    parser.add_argument("--skip-stage", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or (
        ROOT / "benchmark" / "results" / "system_metrics" / timestamp
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = args.graph

    vertex_manifest = read_json(VERTEX_SOURCE / "manifest.json")
    edge_manifest = read_json(EDGE_SOURCE / "manifest.json")
    vertex_labels = sorted(vertex_manifest["vertex_counts"])
    edge_labels = sorted(edge_manifest["edge_counts"])
    expected_vertices = sum(vertex_manifest["vertex_counts"].values())
    expected_edges = sum(edge_manifest["edge_counts"].values())
    vertex_bytes = sum(path.stat().st_size for path in (VERTEX_SOURCE / "vertices").glob("*.csv"))
    edge_bytes = sum(path.stat().st_size for path in (EDGE_SOURCE / "edges").glob("*.csv"))

    sql_files = build_sql(args.graph, stage_dir, output_dir)
    if not args.skip_stage:
        stage_command = (
            "rm -rf /tmp/age/{stage} && "
            "mkdir -p /tmp/age/{stage}/vertices /tmp/age/{stage}/edges && "
            "cp /benchmark/results/load/yago2geo_uk_subset/vertices/*.csv "
            "/tmp/age/{stage}/vertices/ && "
            "cp /benchmark/results/load/yago2geo/uk/edges/*.csv "
            "/tmp/age/{stage}/edges/"
        ).format(stage=stage_dir)
        print("Staging CSV files outside measured phases...", flush=True)
        run_command(["docker", "exec", args.container, "bash", "-lc", stage_command])

    phases: List[Dict[str, Any]] = []
    storage: Dict[str, Dict[str, Any]] = {}

    print("Setting up benchmark graph...", flush=True)
    execute_sql_file(sql_files["setup"])

    print("Loading vertices...", flush=True)
    _, vertex_phase = timed_phase(
        "load_vertices", args.container, output_dir,
        lambda: execute_sql_file(sql_files["vertices"]),
    )
    actual_vertices = count_rows(args.graph, vertex_labels)
    vertex_phase.update(
        {
            "expected_rows": expected_vertices,
            "actual_rows": actual_vertices,
            "input_bytes": vertex_bytes,
            "rows_per_second": actual_vertices / vertex_phase["elapsed_seconds"],
            "input_mb_per_second": (vertex_bytes / 1_000_000) / vertex_phase["elapsed_seconds"],
            "failure_rate": abs(expected_vertices - actual_vertices) / expected_vertices,
        }
    )
    phases.append(vertex_phase)
    checkpoint_progress(output_dir, phases, storage)

    print("Loading edges...", flush=True)
    _, edge_phase = timed_phase(
        "load_edges", args.container, output_dir,
        lambda: execute_sql_file(sql_files["edges"]),
    )
    actual_edges = count_rows(args.graph, edge_labels)
    edge_phase.update(
        {
            "expected_rows": expected_edges,
            "actual_rows": actual_edges,
            "input_bytes": edge_bytes,
            "rows_per_second": actual_edges / edge_phase["elapsed_seconds"],
            "input_mb_per_second": (edge_bytes / 1_000_000) / edge_phase["elapsed_seconds"],
            "failure_rate": abs(expected_edges - actual_edges) / expected_edges,
        }
    )
    phases.append(edge_phase)
    storage["physical_columns_no_optional_indexes"] = storage_snapshot(args.graph)
    checkpoint_progress(output_dir, phases, storage)

    print("Creating GiST indexes...", flush=True)
    _, gist_phase = timed_phase(
        "create_gist", args.container, output_dir,
        lambda: execute_sql_file(sql_files["gist"]),
    )
    gist_phase.update(
        {"expected_rows": "", "actual_rows": "", "input_bytes": "",
         "rows_per_second": "", "input_mb_per_second": "", "failure_rate": ""}
    )
    phases.append(gist_phase)
    storage["with_gist"] = storage_snapshot(args.graph)
    checkpoint_progress(output_dir, phases, storage)

    print("Creating URI property indexes...", flush=True)
    _, uri_phase = timed_phase(
        "create_uri_indexes", args.container, output_dir,
        lambda: execute_sql_file(sql_files["uri"]),
    )
    uri_phase.update(
        {"expected_rows": "", "actual_rows": "", "input_bytes": "",
         "rows_per_second": "", "input_mb_per_second": "", "failure_rate": ""}
    )
    phases.append(uri_phase)
    execute_sql_file(sql_files["analyze"])
    storage["with_gist_and_uri"] = storage_snapshot(args.graph)
    checkpoint_progress(output_dir, phases, storage)

    query_metrics: Dict[str, Any] = {}
    if not args.skip_query:
        warmup_output = output_dir / "query_warmup_results.csv"
        measured_output = output_dir / "query_measured_results.csv"
        common = [
            sys.executable,
            str(QUERY_RUNNER),
            "--engines", "age",
            "--input", str(args.query_input),
            "--age-graph", args.graph,
            "--age-session-mode", "persistent",
            "--timeout", str(args.timeout),
        ]
        if args.query_limit:
            common.extend(["--limit", str(args.query_limit)])
        if not args.skip_warmup:
            print("Warming query workload...", flush=True)
            run_command(
                common
                + ["--run-label", "system_metrics_warmup", "--output", str(warmup_output)],
                capture=False,
            )

        before_io = database_io_snapshot()
        print("Measuring query workload...", flush=True)
        _, query_phase = timed_phase(
            "query_workload",
            args.container,
            output_dir,
            lambda: run_command(
                common
                + ["--run-label", "system_metrics_measured", "--output", str(measured_output)],
                capture=False,
            ),
        )
        after_io = database_io_snapshot()
        query_phase.update(
            {"expected_rows": "", "actual_rows": "", "input_bytes": "",
             "rows_per_second": "", "input_mb_per_second": "", "failure_rate": ""}
        )
        phases.append(query_phase)
        checkpoint_progress(output_dir, phases, storage)
        query_metrics = query_summary(measured_output, query_phase["elapsed_seconds"])
        hit_delta = after_io["blks_hit"] - before_io["blks_hit"]
        read_delta = after_io["blks_read"] - before_io["blks_read"]
        query_metrics.update(
            {
                "shared_blks_hit": hit_delta,
                "shared_blks_read": read_delta,
                "shared_buffer_hit_ratio": (
                    hit_delta / (hit_delta + read_delta)
                    if hit_delta + read_delta
                    else 0.0
                ),
                "temp_files": after_io["temp_files"] - before_io["temp_files"],
                "temp_bytes": after_io["temp_bytes"] - before_io["temp_bytes"],
            }
        )

    write_phase_metrics(output_dir / "phase_metrics.csv", phases)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "graph": args.graph,
        "container": args.container,
        "dataset": {
            "vertex_source": str(VERTEX_SOURCE.relative_to(ROOT)),
            "edge_source": str((EDGE_SOURCE / "edges").relative_to(ROOT)),
            "expected_vertices": expected_vertices,
            "expected_edges": expected_edges,
            "vertex_csv_bytes": vertex_bytes,
            "edge_csv_bytes": edge_bytes,
        },
        "phases": phases,
        "storage": storage,
        "query": query_metrics,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("Results: %s" % output_dir, flush=True)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
