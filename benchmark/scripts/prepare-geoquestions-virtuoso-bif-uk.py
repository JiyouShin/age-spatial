#!/usr/bin/env python3
"""Prepare Virtuoso query variants for UK GeoQuestions.

The output keeps the original GeoSPARQL query and writes a Virtuoso query.
Rows are classified into:

* bif_indexed: native bif:* rewrite over the materialized geometry graph.
* bif_indexed_approx: native bif:* rewrite with an approximation note.
* geosparql_noindex_fallback: original GeoSPARQL/stSPARQL-style query.
* geosparql_unsupported_area: original query is kept, but Virtuoso is expected
  to fail because the current image does not support strdf:area/geof:metricArea.

The bif variants use the materialized geometry graph:

    ?feature wgs:geometry ?geom

The materialized graph stores virtrdf:Geometry literals, so Virtuoso can use its
R-tree geometry index for supported bif:st_* predicates.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT
    / "benchmark/results/geoquestions/age_spatial_full"
    / "geoquestions_age_spatial_full_uk_only_questions.csv"
)
DEFAULT_OUTPUT = (
    ROOT
    / "benchmark/results/geoquestions/virtuoso_bif_uk"
    / "geoquestions_uk_virtuoso_mixed_queries.csv"
)

ORIGINAL_GRAPH = "http://yago-knowledge.org/graph/yago2geo_uk_subset"
GEOM_GRAPH = "http://yago-knowledge.org/graph/yago2geo_uk_subset_virtgeom"

PREFIXES = """\
PREFIX yago: <http://yago-knowledge.org/resource/>
PREFIX y2geoo: <http://kr.di.uoa.gr/yago2geo/ontology/>
PREFIX yago2geo: <http://kr.di.uoa.gr/yago2geo/ontology/>
PREFIX y2geor: <http://kr.di.uoa.gr/yago2geo/resource/>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX geof: <http://www.opengis.net/def/function/geosparql/>
PREFIX uom: <http://www.opengis.net/def/uom/OGC/1.0/>
PREFIX strdf: <http://strdf.di.uoa.gr/ontology#>
PREFIX wgs: <http://www.w3.org/2003/01/geo/wgs84_pos#>
PREFIX virtrdf: <http://www.openlinksw.com/schemas/virtrdf#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

GEOM_PAIR_RE = re.compile(
    r"(?P<feature>(?:\?\w+|[A-Za-z][\w-]*:[A-Za-z0-9_.,'()%-]+|<[^>]+>))\s+"
    r"geo:hasGeometry\s+(?P<geom>\?\w+)\s*\.\s*"
    r"(?P=geom)\s+geo:asWKT\s+(?P<wkt>\?\w+)\s*\.?",
    re.S,
)

TYPE_GEOM_PAIR_RE = re.compile(
    r"(?P<feature>\?\w+)\s+"
    r"rdf:type\s+(?P<label>y2geoo:[A-Za-z0-9_]+)\s*;\s*"
    r"geo:hasGeometry\s+(?P<geom>\?\w+)\s*\.\s*"
    r"(?P=geom)\s+geo:asWKT\s+(?P<wkt>\?\w+)\s*\.?",
    re.S,
)

HAS_GEOM_THEN_TYPE_PAIR_RE = re.compile(
    r"(?P<feature>(?:\?\w+|[A-Za-z][\w-]*:[A-Za-z0-9_.,'()%-]+|<[^>]+>))\s+"
    r"geo:hasGeometry\s+(?P<geom>\?\w+)\s*;\s*"
    r"rdf:type\s+(?P<type>\?\w+|y2geoo:[A-Za-z0-9_]+)\s*\.\s*"
    r"(?P=geom)\s+geo:asWKT\s+(?P<wkt>\?\w+)\s*\.?",
    re.S,
)

STANDALONE_HAS_GEOM_RE = re.compile(
    r"(?P<feature>(?:\?\w+|[A-Za-z][\w-]*:[A-Za-z0-9_.,'()%-]+|<[^>]+>))\s+"
    r"geo:hasGeometry\s+(?P<geom>\?\w+)\s*\.",
    re.S,
)
STANDALONE_AS_WKT_RE = re.compile(
    r"(?P<geom>\?\w+)\s+geo:asWKT\s+(?P<wkt>\?\w+)\s*\.",
    re.S,
)


def add_prefix_and_from(query: str) -> str:
    body = query.strip()
    body = re.sub(r"(?is)^\s*(PREFIX\s+\w+:\s*<[^>]+>\s*)+", "", body).strip()
    dataset = f"\nFROM <{ORIGINAL_GRAPH}>\nFROM <{GEOM_GRAPH}>\nWHERE"
    body = re.sub(r"(?is)\bWHERE\b", dataset, body, count=1)
    body = body.replace(".FILTER", ". FILTER")
    return PREFIXES + "\n" + body


def add_prefix_and_original_from(query: str) -> str:
    body = query.strip()
    body = re.sub(r"(?is)^\s*(PREFIX\s+\w+:\s*<[^>]+>\s*)+", "", body).strip()
    body = re.sub(r"(?is)\bWHERE\b", f"\nFROM <{ORIGINAL_GRAPH}>\nWHERE", body, count=1)
    body = body.replace(".FILTER", ". FILTER")
    return PREFIXES + "\n" + body


def rewrite_geometry_patterns(query: str) -> Tuple[str, int]:
    count = 0

    def repl_type(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        feature = match.group("feature")
        label = match.group("label")
        wkt = match.group("wkt")
        return f"{feature} rdf:type {label} . {feature} wgs:geometry {wkt} ."

    query = TYPE_GEOM_PAIR_RE.sub(repl_type, query)

    def repl_has_geom_then_type(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        feature = match.group("feature")
        type_value = match.group("type")
        wkt = match.group("wkt")
        return f"{feature} wgs:geometry {wkt} . {feature} rdf:type {type_value} ."

    query = HAS_GEOM_THEN_TYPE_PAIR_RE.sub(repl_has_geom_then_type, query)

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        feature = match.group("feature")
        wkt = match.group("wkt")
        return f"{feature} wgs:geometry {wkt} ."

    query = GEOM_PAIR_RE.sub(repl, query)

    # Some GeoQuestions write all hasGeometry triples first, then all asWKT
    # triples. Convert those non-adjacent pairs as a second pass.
    has_matches = list(STANDALONE_HAS_GEOM_RE.finditer(query))
    wkt_by_geom = {
        match.group("geom"): match.group("wkt")
        for match in STANDALONE_AS_WKT_RE.finditer(query)
    }
    for match in has_matches:
        geom = match.group("geom")
        wkt = wkt_by_geom.get(geom)
        if not wkt:
            continue
        feature = match.group("feature")
        query = query.replace(match.group(0), f"{feature} wgs:geometry {wkt} .")
        query = re.sub(
            rf"\s*{re.escape(geom)}\s+geo:asWKT\s+{re.escape(wkt)}\s*\.",
            "",
            query,
            count=1,
        )
        count += 1

    return query, count


def rewrite_distance(query: str) -> Tuple[str, int]:
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        left, right = match.group(1), match.group(2)
        return f"(bif:st_distance({left}, {right}) * 1000.0)"

    pattern = re.compile(
        r"geof:distance\s*\(\s*(\?\w+)\s*,\s*(\?\w+)\s*,\s*uom:metre\s*\)"
    )
    return pattern.sub(repl, query), count


def rewrite_direction(query: str) -> Tuple[str, List[str]]:
    replacements = {
        "strdf:left": lambda a, b: f"(bif:st_xmax({a}) < bif:st_xmin({b}))",
        "strdf:right": lambda a, b: f"(bif:st_xmin({a}) > bif:st_xmax({b}))",
        "strdf:above": lambda a, b: f"(bif:st_ymin({a}) > bif:st_ymax({b}))",
        "strdf:below": lambda a, b: f"(bif:st_ymax({a}) < bif:st_ymin({b}))",
    }
    used: List[str] = []
    for fn, builder in replacements.items():
        pattern = re.compile(
            rf"{re.escape(fn)}\s*\(\s*(\?\w+)\s*,\s*(\?\w+)\s*\)"
        )

        def repl(match: re.Match[str], *, fn: str = fn, builder=builder) -> str:
            if fn not in used:
                used.append(fn)
            return builder(match.group(1), match.group(2))

        query = pattern.sub(repl, query)
    return query, [f"{fn}->bbox comparison" for fn in used]


def rewrite_supported_functions(query: str) -> Tuple[str, List[str]]:
    replacements = {
        "geof:sfWithin": "bif:st_within",
        "strdf:within": "bif:st_within",
        "geof:sfContains": "bif:st_contains",
        "geof:sfIntersects": "bif:st_intersects",
    }
    used: List[str] = []
    for src, dst in replacements.items():
        if src in query:
            query = query.replace(src, dst)
            used.append(f"{src}->{dst}")
    query, distance_count = rewrite_distance(query)
    if distance_count:
        used.append("geof:distance(uom:metre)->bif:st_distance(km)")
    query, direction_used = rewrite_direction(query)
    used.extend(direction_used)
    return query, used


AREA_FUNCTIONS = {"strdf:area", "geof:metricArea"}
APPROX_TO_INTERSECTS = {
    "geof:sfTouches": "bif:st_intersects",
    "strdf:touches": "bif:st_intersects",
    "geof:sfCrosses": "bif:st_intersects",
    "strdf:crosses": "bif:st_intersects",
    "strdf:overlaps": "bif:st_intersects",
}
TOPOLOGY_FALLBACK_FUNCTIONS = set(APPROX_TO_INTERSECTS)
DIR_FUNCTIONS = {"strdf:above", "strdf:below", "strdf:left", "strdf:right"}


def function_set(row: Dict[str, str]) -> set[str]:
    return {fn for fn in row.get("spatial_functions", "").split("|") if fn}


def rewrite_approx_topology(query: str) -> Tuple[str, List[str]]:
    used = []
    for src, dst in APPROX_TO_INTERSECTS.items():
        if src in query:
            query = query.replace(src, dst)
            used.append(f"{src}->{dst} approximation")
    return query, used


def rewrite_query(row: Dict[str, str]) -> Tuple[str, str, str]:
    fns = function_set(row)
    if fns & AREA_FUNCTIONS:
        return (
            add_prefix_and_original_from(row["sparql"]),
            "geosparql_unsupported_area",
            "kept original query; Virtuoso image does not support strdf:area/geof:metricArea",
        )
    if (fns & TOPOLOGY_FALLBACK_FUNCTIONS) and not (fns & DIR_FUNCTIONS):
        return (
            add_prefix_and_original_from(row["sparql"]),
            "geosparql_noindex_fallback",
            "kept original GeoSPARQL topology query; no bif exact equivalent in current Virtuoso image",
        )

    query = row["sparql"]
    query, geom_count = rewrite_geometry_patterns(query)
    query, used = rewrite_supported_functions(query)
    mode = "bif_indexed"
    if fns & TOPOLOGY_FALLBACK_FUNCTIONS:
        query, approx_used = rewrite_approx_topology(query)
        used.extend(approx_used)
        mode = "bif_indexed_approx"
    if geom_count == 0:
        return "", "review_needed", "no geo:hasGeometry/geo:asWKT pair was rewritten"
    if not used:
        return "", "review_needed", "no supported spatial function was rewritten"
    return add_prefix_and_from(query), mode, "; ".join(used)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    with args.input.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    output_rows = []
    for row in rows:
        query, status, note = rewrite_query(row)
        output_rows.append(
            {
                "id": row["id"],
                "category": row["category"],
                "question": row["question"],
                "original_sparql": row["sparql"],
                "spatial_functions": row["spatial_functions"],
                "pattern": row["pattern"],
                "answer": row["answer"],
                "virtuoso_mode": status,
                "virtuoso_note": note,
                "virtuoso_query": query,
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "category",
        "question",
        "original_sparql",
        "spatial_functions",
        "pattern",
        "answer",
        "virtuoso_mode",
        "virtuoso_note",
        "virtuoso_query",
    ]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)

    from collections import Counter

    status_counts = Counter(row["virtuoso_mode"] for row in output_rows)
    print(f"Wrote {args.output}")
    print("mode:", dict(status_counts))


if __name__ == "__main__":
    main()
