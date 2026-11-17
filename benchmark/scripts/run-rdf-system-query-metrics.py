#!/usr/bin/env python3
"""Measure comparable query/resource metrics for RDF geospatial engines."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


ROOT = Path(__file__).resolve().parents[2]
RDF_COMPOSE = ROOT / "docker" / "rdf-benchmark" / "docker-compose.yml"
AGE_COMPOSE = ROOT / "docker" / "benchmark" / "docker-compose.yml"
QUERY_RUNNER = ROOT / "benchmark" / "scripts" / "run-geoquestions-full-benchmark.py"
QUERY_INPUT = (
    ROOT / "benchmark" / "results" / "geoquestions" / "age_spatial_full"
    / "geoquestions_age_spatial_full_uk_only_questions.csv"
)
VIRTUOSO_QUERY_FILE = (
    ROOT / "benchmark" / "results" / "geoquestions" / "virtuoso_bif_uk"
    / "geoquestions_uk_virtuoso_mixed_queries.csv"
)


def load_age_metrics_module():
    path = ROOT / "benchmark" / "scripts" / "run-age-spatial-system-metrics.py"
    spec = importlib.util.spec_from_file_location("age_system_metrics", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import AGE system metrics helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AGE_METRICS = load_age_metrics_module()


ENGINE_CONFIG: Dict[str, Dict[str, Any]] = {
    "jena": {
        "containers": ["yago2geo-jena-fuseki"],
        "endpoint": "http://127.0.0.1:13030/yago2geo_uk_subset/sparql",
    },
    "virtuoso": {
        "containers": ["yago2geo-virtuoso"],
        "endpoint": "http://127.0.0.1:18890/sparql",
    },
    "strabon": {
        "containers": ["yago2geo-strabon", "yago2geo-strabon-postgis"],
        "endpoint": "http://127.0.0.1:18080/Strabon/Query",
    },
}


def run(command: Sequence[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command), cwd=str(ROOT), text=True, check=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def compose(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "-f", str(RDF_COMPOSE), *args], capture=capture)


def stop_other_engines(engine: str) -> None:
    run(["docker", "compose", "-f", str(AGE_COMPOSE), "stop", "age-spatial-pg"])
    services = {
        "jena": ["virtuoso", "strabon", "strabon-postgis"],
        "virtuoso": ["jena", "strabon", "strabon-postgis"],
        "strabon": ["jena", "virtuoso"],
    }[engine]
    compose("stop", *services)


def post_ready(url: str, *, strabon: bool = False, timeout: int = 360) -> None:
    query = "ASK WHERE { }" if strabon else "ASK { ?s ?p ?o }"
    headers = {
        "Accept": "application/sparql-results+xml" if strabon else "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = urllib.parse.urlencode({"query": query}).encode()
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read(1)
                if 200 <= response.status < 300:
                    return
        except Exception:
            time.sleep(3 if not strabon else 5)
    raise TimeoutError("endpoint did not become ready: %s" % url)


def start_engine(engine: str) -> None:
    if engine == "jena":
        run([str(ROOT / "benchmark" / "scripts" / "start-yago2geo-uk-subset-jena-indexed.sh")], capture=False)
    elif engine == "virtuoso":
        compose("up", "-d", "virtuoso", capture=False)
    else:
        compose("up", "-d", "strabon-postgis", "strabon", capture=False)
    post_ready(ENGINE_CONFIG[engine]["endpoint"], strabon=engine == "strabon")


class MultiContainerSampler:
    def __init__(self, containers: Sequence[str], output: Path) -> None:
        self.containers = list(containers)
        self.output = output
        self.samples: List[Dict[str, float]] = []
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def snapshot(self) -> Dict[str, float]:
        rows = [AGE_METRICS.docker_snapshot(name) for name in self.containers]
        return {
            "timestamp_epoch": time.time(),
            "cpu_percent": sum(row.get("cpu_percent", 0.0) for row in rows),
            "memory_used_bytes": sum(row.get("memory_used_bytes", 0.0) for row in rows),
            "block_read_bytes": sum(row.get("block_read_bytes", 0.0) for row in rows),
            "block_write_bytes": sum(row.get("block_write_bytes", 0.0) for row in rows),
        }

    def start(self) -> None:
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()

    def _sample(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.samples.append(self.snapshot())
            except Exception:
                pass
            self.stop_event.wait(1.0)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        fields = ["timestamp_epoch", "cpu_percent", "memory_used_bytes", "block_read_bytes", "block_write_bytes"]
        with self.output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.samples)


def resource_summary(idle: Dict[str, float], samples: Sequence[Dict[str, float]]) -> Dict[str, float]:
    cpu = [row["cpu_percent"] for row in samples]
    memory = [row["memory_used_bytes"] for row in samples]
    peak = max(memory) if memory else idle["memory_used_bytes"]
    first = samples[0] if samples else idle
    last = samples[-1] if samples else idle
    return {
        "sample_count": len(samples),
        "average_cpu_percent": sum(cpu) / len(cpu) if cpu else 0.0,
        "peak_cpu_percent": max(cpu) if cpu else 0.0,
        "idle_memory_bytes": idle["memory_used_bytes"],
        "average_memory_bytes": sum(memory) / len(memory) if memory else idle["memory_used_bytes"],
        "peak_memory_bytes": peak,
        "incremental_peak_memory_bytes": max(0.0, peak - idle["memory_used_bytes"]),
        "block_read_bytes": max(0.0, last["block_read_bytes"] - first["block_read_bytes"]),
        "block_write_bytes": max(0.0, last["block_write_bytes"] - first["block_write_bytes"]),
    }


def add_rdf_error_metrics(query: Dict[str, Any]) -> Dict[str, Any]:
    """Split RDF endpoint failures into execution, timeout, and HTTP classes."""
    counts = query.get("execution_status_counts", {})
    attempted = int(query.get("attempted_queries", 0))
    execution_errors = sum(
        count for status, count in counts.items()
        if status == "ERROR" or status.startswith("ERROR_")
    )
    http_errors = sum(
        count for status, count in counts.items()
        if status.startswith("HTTP_")
    )
    query["execution_error_count"] = execution_errors
    query["http_error_count"] = http_errors
    query["timeout_count"] = int(counts.get("TIMEOUT", 0))
    query["execution_error_rate"] = execution_errors / attempted if attempted else 0.0
    query["http_error_rate"] = http_errors / attempted if attempted else 0.0
    return query


def query_command(engine: str, output: Path, label: str, timeout: float) -> List[str]:
    command = [
        sys.executable, str(QUERY_RUNNER),
        "--input", str(QUERY_INPUT), "--engines", engine,
        "--timeout", str(timeout), "--flush-every", "10",
        "--run-label", label, "--output", str(output),
        "--jena-endpoint", ENGINE_CONFIG["jena"]["endpoint"],
        "--virtuoso-endpoint", ENGINE_CONFIG["virtuoso"]["endpoint"],
        "--virtuoso-graph", "http://yago-knowledge.org/graph/yago2geo_uk_subset",
        "--virtuoso-query-file", str(VIRTUOSO_QUERY_FILE),
        "--strabon-endpoint", ENGINE_CONFIG["strabon"]["endpoint"],
        "--strabon-mode", "endpoint", "--strabon-db", "yago2geo_uk_subset",
        "--strabon-container", "yago2geo-strabon",
        "--strabon-cleanup-on-timeout", "--strabon-restart-on-timeout",
        "--strabon-postgis-container", "yago2geo-strabon-postgis",
        "--strabon-cleanup-dbs", "endpoint,yago2geo_uk_subset",
    ]
    return command


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def storage_size(engine: str) -> Dict[str, Any]:
    if engine == "jena":
        path = ROOT / "benchmark" / "results" / "rdfdb" / "jena_data" / "databases" / "yago2geo_uk_subset"
        return {"scope": "target TDB2 dataset directory", "bytes": directory_size(path)}
    if engine == "virtuoso":
        path = ROOT / "benchmark" / "results" / "rdfdb" / "virtuoso_data"
        return {"scope": "entire Virtuoso database directory (contains multiple graphs)", "bytes": directory_size(path)}
    proc = run([
        "docker", "exec", "yago2geo-strabon-postgis", "psql", "-U", "postgres", "-d", "postgres",
        "-At", "-c", "SELECT pg_database_size('endpoint');",
    ])
    return {"scope": "Strabon endpoint PostgreSQL database", "bytes": int(proc.stdout.strip())}


def strabon_database_io_snapshot() -> Dict[str, int]:
    proc = run([
        "docker", "exec", "yago2geo-strabon-postgis",
        "psql", "-U", "postgres", "-d", "endpoint", "-At", "-F", ",", "-c",
        "SELECT blks_read, blks_hit, temp_files, temp_bytes "
        "FROM pg_stat_database WHERE datname = current_database();",
    ])
    values = [int(value) for value in proc.stdout.strip().split(",")]
    return dict(zip(("blks_read", "blks_hit", "temp_files", "temp_bytes"), values))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=sorted(ENGINE_CONFIG), required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--skip-warmup", action="store_true")
    parser.add_argument("--stop-after", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    engine = args.engine
    containers = ENGINE_CONFIG[engine]["containers"]

    stop_other_engines(engine)
    start_engine(engine)
    if not args.skip_warmup:
        print("Warming %s..." % engine, flush=True)
        run(query_command(engine, output / "query_warmup_results.csv", "system_metrics_warmup", args.timeout), capture=False)

    sampler = MultiContainerSampler(containers, output / "docker_query_workload.csv")
    idle = sampler.snapshot()
    before_pg_io = strabon_database_io_snapshot() if engine == "strabon" else None
    sampler.start()
    started = time.perf_counter_ns()
    print("Measuring %s..." % engine, flush=True)
    run(query_command(engine, output / "query_measured_results.csv", "system_metrics_measured", args.timeout), capture=False)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    sampler.stop()

    query = add_rdf_error_metrics(
        AGE_METRICS.query_summary(output / "query_measured_results.csv", elapsed)
    )
    if before_pg_io is not None:
        after_pg_io = strabon_database_io_snapshot()
        hit_delta = after_pg_io["blks_hit"] - before_pg_io["blks_hit"]
        read_delta = after_pg_io["blks_read"] - before_pg_io["blks_read"]
        query.update({
            "shared_blks_hit": hit_delta,
            "shared_blks_read": read_delta,
            "shared_buffer_hit_ratio": (
                hit_delta / (hit_delta + read_delta)
                if hit_delta + read_delta else 0.0
            ),
            "temp_files": after_pg_io["temp_files"] - before_pg_io["temp_files"],
            "temp_bytes": after_pg_io["temp_bytes"] - before_pg_io["temp_bytes"],
        })
    resources = resource_summary(idle, sampler.samples)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine": engine,
        "containers": containers,
        "warm_cache": not args.skip_warmup,
        "query": query,
        "resources": resources,
        "storage": storage_size(engine),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)

    if args.stop_after:
        compose("stop", *({"jena": ["jena"], "virtuoso": ["virtuoso"], "strabon": ["strabon", "strabon-postgis"]}[engine]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
