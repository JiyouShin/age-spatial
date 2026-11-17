#!/usr/bin/env python3
"""Build a comparable AGE-Spatial/RDF-engine system metrics table."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def failure_counts(status_counts: Dict[str, int]) -> Dict[str, int]:
    execution_errors = sum(
        count for status, count in status_counts.items()
        if status == "ERROR" or status.startswith("ERROR_")
    )
    http_errors = sum(
        count for status, count in status_counts.items()
        if status.startswith("HTTP_")
    )
    return {
        "execution_errors": execution_errors,
        "timeouts": int(status_counts.get("TIMEOUT", 0)),
        "http_errors": http_errors,
    }


def age_resource(summary: Dict[str, Any]) -> Dict[str, float]:
    for phase in summary["phases"]:
        if phase["phase"] == "query_workload":
            return phase
    raise ValueError("AGE-Spatial query_workload phase not found")


def age_storage(summary: Dict[str, Any]) -> Dict[str, Any]:
    storage = summary["storage"]["with_gist_and_uri"]
    return {
        "bytes": storage["total_storage_bytes"],
        "scope": "AGE graph tables, metadata, GiST, and URI indexes",
    }


def build_row(engine: str, summary: Dict[str, Any], is_age: bool) -> Dict[str, Any]:
    query = summary["query"]
    resources = age_resource(summary) if is_age else summary["resources"]
    storage = age_storage(summary) if is_age else summary["storage"]
    status_counts = query["execution_status_counts"]
    match_counts = query["match_status_counts"]
    failures = failure_counts(status_counts)
    return {
        "engine": engine,
        "attempted": query["attempted_queries"],
        "executed": query["successful_queries"],
        "answer_matched": match_counts.get("match", 0),
        "answer_mismatched": match_counts.get("mismatch", 0),
        **failures,
        "latency_p50_ms": query["latency_p50_ms"],
        "latency_p95_ms": query["latency_p95_ms"],
        "latency_p98_ms": query["latency_p98_ms"],
        "latency_p99_ms": query["latency_p99_ms"],
        "attempted_qps": query["attempted_query_throughput_qps"],
        "successful_qps": query["successful_query_throughput_qps"],
        "mismatch_rate_executed": query["answer_mismatch_rate_among_executed"],
        "average_cpu_percent": resources["average_cpu_percent"],
        "incremental_peak_memory_mib": resources["incremental_peak_memory_bytes"] / 1024**2,
        "block_read_mib": resources["block_read_bytes"] / 1024**2,
        "block_write_mib": resources["block_write_bytes"] / 1024**2,
        "storage_mib": storage["bytes"] / 1024**2,
        "storage_scope": storage["scope"],
    }


def write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: List[Dict[str, Any]]) -> None:
    lines = [
        "| Engine | Executed | Matched | Exec. error | Timeout | HTTP error | p50 (ms) | p95 (ms) | Attempted QPS | Avg CPU (%) | Peak memory increase (MiB) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {engine} | {executed} | {answer_matched} | {execution_errors} | "
            "{timeouts} | {http_errors} | {latency_p50_ms:.2f} | "
            "{latency_p95_ms:.2f} | {attempted_qps:.3f} | "
            "{average_cpu_percent:.2f} | {incremental_peak_memory_mib:.2f} |".format(**row)
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--age-summary", type=Path, required=True)
    parser.add_argument("--rdf-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    inputs = [
        ("AGE-Spatial", args.age_summary, True),
        ("Apache Jena", args.rdf_root / "jena" / "summary.json", False),
        ("Virtuoso", args.rdf_root / "virtuoso" / "summary.json", False),
        ("Strabon", args.rdf_root / "strabon" / "summary.json", False),
    ]
    rows = [build_row(name, read_json(path), is_age) for name, path, is_age in inputs]
    write_csv(args.output_dir / "comparison.csv", rows)
    write_markdown(args.output_dir / "comparison.md", rows)
    print((args.output_dir / "comparison.md").read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
