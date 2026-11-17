#!/usr/bin/env python3
"""Benchmark fixed-depth relationship matching with a spatial predicate.

The available workloads use materialized YAGO2Geo relationships and native
PostGIS geometry columns.  They cover administrative WITHIN paths and exact-hop
TOUCHES traversal with a metric distance filter.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import subprocess
from pathlib import Path


WITHIN_QUERIES = {
    1: """MATCH (p:OS_CivilParishorCommunity)
              -[:WITHIN]->(w:OS_DistrictWard),
              (t:OSM_town)
          WHERE ST_Intersects(t.geom, p.geom)
          RETURN count(*) AS result_count""",
    2: """MATCH (p:OS_CivilParishorCommunity)
              -[:WITHIN]->(w:OS_DistrictWard)
              -[:WITHIN]->(d:OS_District),
              (t:OSM_town)
          WHERE ST_Intersects(t.geom, p.geom)
          RETURN count(*) AS result_count""",
    3: """MATCH (p:OS_CivilParishorCommunity)
              -[:WITHIN]->(w:OS_DistrictWard)
              -[:WITHIN]->(d:OS_District)
              -[:WITHIN]->(c:OS_County),
              (t:OSM_town)
          WHERE ST_Intersects(t.geom, p.geom)
          RETURN count(*) AS result_count""",
}

ACLE_URI = "http://yago-knowledge.org/resource/Acle"

TOUCHES_DISTANCE_QUERIES = {
    1: f"""MATCH (s:OS_DistrictWard)
              -[:TOUCHES]->(w1:OS_DistrictWard),
              (a:OSM_town)
          WHERE s.uri = '{ACLE_URI}'
            AND a.uri = '{ACLE_URI}'
            AND id(w1) <> id(s)
            AND ST_DistanceSphere(
                  ST_Centroid(w1.geom), ST_Centroid(a.geom)
                ) <= 10000
          RETURN count(DISTINCT w1.uri) AS result_count""",
    2: f"""MATCH (s:OS_DistrictWard)
              -[:TOUCHES]->(w1:OS_DistrictWard)
              -[:TOUCHES]->(w2:OS_DistrictWard),
              (a:OSM_town)
          WHERE s.uri = '{ACLE_URI}'
            AND a.uri = '{ACLE_URI}'
            AND id(w2) <> id(s)
            AND ST_DistanceSphere(
                  ST_Centroid(w2.geom), ST_Centroid(a.geom)
                ) <= 10000
          RETURN count(DISTINCT w2.uri) AS result_count""",
    3: f"""MATCH (s:OS_DistrictWard)
              -[:TOUCHES]->(w1:OS_DistrictWard)
              -[:TOUCHES]->(w2:OS_DistrictWard)
              -[:TOUCHES]->(w3:OS_DistrictWard),
              (a:OSM_town)
          WHERE s.uri = '{ACLE_URI}'
            AND a.uri = '{ACLE_URI}'
            AND id(w3) <> id(s)
            AND ST_DistanceSphere(
                  ST_Centroid(w3.geom), ST_Centroid(a.geom)
                ) <= 10000
          RETURN count(DISTINCT w3.uri) AS result_count""",
}

TOUCHES_CANDIDATE_QUERIES = {
    1: f"""MATCH (s:OS_DistrictWard)
              -[:TOUCHES]->(w1:OS_DistrictWard)
          WHERE s.uri = '{ACLE_URI}' AND id(w1) <> id(s)
          RETURN count(DISTINCT w1.uri) AS result_count""",
    2: f"""MATCH (s:OS_DistrictWard)
              -[:TOUCHES]->(w1:OS_DistrictWard)
              -[:TOUCHES]->(w2:OS_DistrictWard)
          WHERE s.uri = '{ACLE_URI}' AND id(w2) <> id(s)
          RETURN count(DISTINCT w2.uri) AS result_count""",
    3: f"""MATCH (s:OS_DistrictWard)
              -[:TOUCHES]->(w1:OS_DistrictWard)
              -[:TOUCHES]->(w2:OS_DistrictWard)
              -[:TOUCHES]->(w3:OS_DistrictWard)
          WHERE s.uri = '{ACLE_URI}' AND id(w3) <> id(s)
          RETURN count(DISTINCT w3.uri) AS result_count""",
}

WORKLOADS = {
    "within_hierarchy": {
        "queries": WITHIN_QUERIES,
        "candidate_queries": {},
        "patterns": {
            1: "WITHIN",
            2: "WITHIN -> WITHIN",
            3: "WITHIN -> WITHIN -> WITHIN",
        },
        "spatial_predicate": "ST_Intersects(t.geom, p.geom)",
        "description": "Administrative containment depth with a town-parish intersection",
    },
    "touches_distance": {
        "queries": TOUCHES_DISTANCE_QUERIES,
        "candidate_queries": TOUCHES_CANDIDATE_QUERIES,
        "patterns": {
            1: "TOUCHES",
            2: "TOUCHES -> TOUCHES",
            3: "TOUCHES -> TOUCHES -> TOUCHES",
        },
        "spatial_predicate": "Endpoint centroid distance from Acle town <= 10 km",
        "description": "Exact-hop ward adjacency from Acle with a metric spatial filter",
    },
}

TIME_RE = re.compile(r"^Time:\s+([0-9.]+)\s+ms$")
MARKER_RE = re.compile(r"^__MEASURE__\s+depth=(\d+)\s+run=(\d+)$")


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile of an empty list")
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def psql_command(container: str, database: str) -> list[str]:
    return [
        "docker", "exec", "-i", container,
        "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
        "-d", database,
    ]


def run_psql(container: str, database: str, sql: str) -> str:
    result = subprocess.run(
        psql_command(container, database),
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "psql failed with exit code %d\nstdout:\n%s\nstderr:\n%s"
            % (result.returncode, result.stdout, result.stderr)
        )
    return result.stdout


def cypher_select(graph: str, query: str) -> str:
    compact = " ".join(line.strip() for line in query.splitlines())
    return (
        "SELECT result_count FROM cypher('%s', $cypher$ %s $cypher$) "
        "AS (result_count agtype);" % (graph, compact)
    )


def get_result_count(container: str, database: str, graph: str, query: str) -> int:
    sql = "\n".join([
        "LOAD 'age';",
        "SET search_path TO ag_catalog, public;",
        "SET client_min_messages TO warning;",
        "\\pset tuples_only on",
        "\\pset format unaligned",
        cypher_select(graph, query),
    ])
    output = run_psql(container, database, sql)
    numeric = [line.strip() for line in output.splitlines() if line.strip().isdigit()]
    if not numeric:
        raise RuntimeError("could not parse result count from psql output:\n" + output)
    return int(numeric[-1])


def measure_query(
    container: str,
    database: str,
    graph: str,
    depth: int,
    query: str,
    warmups: int,
    runs: int,
) -> list[float]:
    statements = [
        "LOAD 'age';",
        "SET search_path TO ag_catalog, public;",
        "SET client_min_messages TO warning;",
        "SET statement_timeout TO '60s';",
        "\\pset tuples_only on",
        "\\pset format unaligned",
    ]
    select = cypher_select(graph, query)
    for _ in range(warmups):
        statements.append(select)
    statements.append("\\timing on")
    for run in range(1, runs + 1):
        statements.append("\\echo __MEASURE__ depth=%d run=%d" % (depth, run))
        statements.append(select)

    output = run_psql(container, database, "\n".join(statements) + "\n")
    measurements: list[float] = []
    awaiting_time = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if MARKER_RE.match(line):
            awaiting_time = True
            continue
        match = TIME_RE.match(line)
        if awaiting_time and match:
            measurements.append(float(match.group(1)))
            awaiting_time = False
    if len(measurements) != runs:
        raise RuntimeError(
            "expected %d timings for depth %d, found %d\n%s"
            % (runs, depth, len(measurements), output)
        )
    return measurements


def capture_plan(
    container: str,
    database: str,
    graph: str,
    query: str,
) -> list[dict]:
    select = cypher_select(graph, query).rstrip(";")
    sql = "\n".join([
        "LOAD 'age';",
        "SET search_path TO ag_catalog, public;",
        "SET client_min_messages TO warning;",
        "SET statement_timeout TO '60s';",
        "\\pset tuples_only on",
        "\\pset format unaligned",
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + select + ";",
    ])
    output = run_psql(container, database, sql)
    start = output.find("[")
    end = output.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError("could not parse JSON plan:\n" + output)
    return json.loads(output[start:end + 1])


def plan_summary(plan: list[dict]) -> dict:
    root = plan[0]
    node_types: set[str] = set()
    index_names: set[str] = set()

    def visit(node: dict) -> None:
        if "Node Type" in node:
            node_types.add(node["Node Type"])
        if "Index Name" in node:
            index_names.add(node["Index Name"])
        for child in node.get("Plans", []):
            visit(child)

    visit(root["Plan"])
    return {
        "planning_time_ms": root.get("Planning Time"),
        "execution_time_ms": root.get("Execution Time"),
        "node_types": sorted(node_types),
        "index_names": sorted(index_names),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="age-spatial-pg16")
    parser.add_argument("--database", default="age_spatial_bench")
    parser.add_argument("--graph", default="age_spatial_system_metrics")
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument(
        "--workload",
        choices=sorted(WORKLOADS),
        default="within_hierarchy",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark/results/relationship_spatial_depth"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    workload = WORKLOADS[args.workload]
    queries = workload["queries"]
    candidate_queries = workload["candidate_queries"]

    raw_rows: list[dict] = []
    summaries: list[dict] = []
    for depth, query in queries.items():
        candidate_count = None
        if depth in candidate_queries:
            candidate_count = get_result_count(
                args.container, args.database, args.graph,
                candidate_queries[depth],
            )
        result_count = get_result_count(
            args.container, args.database, args.graph, query
        )
        times = measure_query(
            args.container, args.database, args.graph, depth, query,
            args.warmups, args.runs,
        )
        plan = capture_plan(args.container, args.database, args.graph, query)
        (args.output_dir / ("depth_%d_plan.json" % depth)).write_text(
            json.dumps(plan, indent=2) + "\n", encoding="utf-8"
        )
        psummary = plan_summary(plan)
        for run, elapsed_ms in enumerate(times, 1):
            raw_rows.append({
                "depth": depth,
                "run": run,
                "execution_time_ms": elapsed_ms,
                "result_count": result_count,
            })
        summaries.append({
            "depth": depth,
            "relationship_pattern": workload["patterns"][depth],
            "candidate_count_before_spatial": candidate_count,
            "result_count": result_count,
            "runs": len(times),
            "mean_ms": statistics.fmean(times),
            "p50_ms": percentile(times, 0.50),
            "p95_ms": percentile(times, 0.95),
            "min_ms": min(times),
            "max_ms": max(times),
            "explain_execution_ms": psummary["execution_time_ms"],
            "explain_planning_ms": psummary["planning_time_ms"],
            "plan_node_types": "; ".join(psummary["node_types"]),
            "plan_index_names": "; ".join(psummary["index_names"]),
            "query": " ".join(line.strip() for line in query.splitlines()),
        })

    with (args.output_dir / "runs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(raw_rows[0]))
        writer.writeheader()
        writer.writerows(raw_rows)
    with (args.output_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    (args.output_dir / "summary.json").write_text(
        json.dumps({
            "graph": args.graph,
            "workload": args.workload,
            "description": workload["description"],
            "warmups": args.warmups,
            "measured_runs": args.runs,
            "spatial_candidate_label": "OSM_town",
            "spatial_predicate": workload["spatial_predicate"],
            "queries": summaries,
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
