#!/usr/bin/env python3
"""Run representative GeoQuestions with stSPARQL rewrites for RDF engines.

This focuses on the representative questions whose original SPARQL uses
strdf:* functions.  The rewritten queries keep the RDF graph structure, but
replace stSPARQL calls with functions available in the target RDF engine where
possible:

* Jena: direction predicates use geof:envelope plus SPARQL string extraction.
  Area is rewritten to GeoSPARQL 1.1 geof:metricArea, which documents current
  implementation support if the value remains unbound.
* Virtuoso: direction predicates also use geof:envelope plus SPARQL string
  extraction. Area is attempted as geof:metricArea.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT
    / "benchmark"
    / "results"
    / "geoquestions"
    / "age_spatial_function_representative"
    / "geoquestions_age_spatial_function_representative_results.csv"
)
DEFAULT_OUTPUT = (
    ROOT
    / "benchmark"
    / "results"
    / "geoquestions"
    / "rdf_representative"
    / "geoquestions_jena_virtuoso_strdf_rewrite_results.csv"
)


PREFIXES = """\
PREFIX yago: <http://yago-knowledge.org/resource/>
PREFIX y2geoo: <http://kr.di.uoa.gr/yago2geo/ontology/>
PREFIX yago2geo: <http://kr.di.uoa.gr/yago2geo/ontology/>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX geof: <http://www.opengis.net/def/function/geosparql/>
PREFIX uom: <http://www.opengis.net/def/uom/OGC/1.0/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX bif: <bif:>
"""


OUTPUT_FIELDS = [
    "category",
    "자연어질문",
    "원본 SPARQL 쿼리",
    "rewrite 대상 함수",
    "Jena rewrite SPARQL 쿼리",
    "Virtuoso rewrite SPARQL 쿼리",
    "rewrite note",
    "원래 정답",
    "jena 쿼리 수행결과 정답",
    "jena 실행시간",
    "jena status",
    "virtuoso 쿼리 수행결과 정답",
    "virtuoso 실행시간",
    "virtuoso status",
]


class HardTimeout(Exception):
    pass


@contextlib.contextmanager
def hard_timeout(seconds: float):
    def _raise_timeout(_signum, _frame):
        raise HardTimeout()

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def with_prefixes(query: str) -> str:
    query = query.strip()
    if query.upper().startswith("PREFIX "):
        return query
    return PREFIXES + "\n" + query


def jena_bbox_bindings(wkt_var: str, prefix: str) -> str:
    """Return SPARQL that extracts bbox ordinates from geof:envelope(?wkt)."""
    # geof:envelope returns a rectangular POLYGON literal.  The regex extracts:
    # minX minY, minX maxY, maxX maxY, maxX minY, minX minY
    return "\n".join(
        [
            f"BIND(STR(geof:envelope({wkt_var})) AS ?{prefix}Env)",
            f"BIND(REPLACE(?{prefix}Env, '^.*\\\\(\\\\(([-0-9.Ee]+) ([-0-9.Ee]+),.*$', '$1') AS ?{prefix}MinXText)",
            f"BIND(REPLACE(?{prefix}Env, '^.*\\\\(\\\\(([-0-9.Ee]+) ([-0-9.Ee]+),.*$', '$2') AS ?{prefix}MinYText)",
            f"BIND(REPLACE(?{prefix}Env, '^.*\\\\(\\\\([-0-9.Ee]+ [-0-9.Ee]+, [-0-9.Ee]+ ([-0-9.Ee]+), ([-0-9.Ee]+) ([-0-9.Ee]+),.*$', '$2') AS ?{prefix}MaxXText)",
            f"BIND(REPLACE(?{prefix}Env, '^.*\\\\(\\\\([-0-9.Ee]+ [-0-9.Ee]+, [-0-9.Ee]+ ([-0-9.Ee]+), ([-0-9.Ee]+) ([-0-9.Ee]+),.*$', '$3') AS ?{prefix}MaxYText)",
            f"BIND(xsd:double(?{prefix}MinXText) AS ?{prefix}MinX)",
            f"BIND(xsd:double(?{prefix}MaxXText) AS ?{prefix}MaxX)",
            f"BIND(xsd:double(?{prefix}MinYText) AS ?{prefix}MinY)",
            f"BIND(xsd:double(?{prefix}MaxYText) AS ?{prefix}MaxY)",
        ]
    )


def jena_direction_query(subject1: str, subject2: str, var1: str, var2: str, relation: str) -> str:
    if relation == "below":
        condition = "?aMaxY < ?bMinY"
    elif relation == "above":
        condition = "?aMinY > ?bMaxY"
    elif relation == "left":
        condition = "?aMaxX < ?bMinX"
    elif relation == "right":
        condition = "?aMinX > ?bMaxX"
    else:
        raise ValueError(relation)

    return f"""ASK WHERE {{
  {subject1} geo:hasGeometry ?geo1 .
  {subject2} geo:hasGeometry ?geo2 .
  ?geo1 geo:asWKT {var1} .
  ?geo2 geo:asWKT {var2} .
  {jena_bbox_bindings(var1, "a")}
  {jena_bbox_bindings(var2, "b")}
  FILTER ({condition})
}}"""


def virtuoso_direction_query(subject1: str, subject2: str, var1: str, var2: str, relation: str) -> str:
    return jena_direction_query(subject1, subject2, var1, var2, relation)


def rewritten_queries(row: Dict[str, str]) -> Optional[Tuple[str, str, str, str]]:
    q = row["자연어질문"]
    original = row["SPARQL 쿼리"]

    if "strdf:area" in original:
        rewritten = original.replace("strdf:area", "geof:metricArea")
        note = (
            "strdf:area -> geof:metricArea. This is the GeoSPARQL 1.1 equivalent, "
            "but the current Jena/Virtuoso images do not return a numeric value."
        )
        return rewritten, rewritten, "strdf:area", note

    if q == "Is Crete south of Thessaly?":
        return (
            jena_direction_query("yago:Crete", "yago:Thessaly", "?geoWKT1", "?geoWKT2", "below"),
            virtuoso_direction_query("yago:Crete", "yago:Thessaly", "?geoWKT1", "?geoWKT2", "below"),
            "strdf:below",
            "strdf:below -> bbox maxY(left) < minY(right).",
        )

    if q == "Is Worcestershire west of Oxfordshire?":
        return (
            jena_direction_query("yago:Worcestershire", "yago:Oxfordshire", "?geoWKT1", "?geoWKT2", "left"),
            virtuoso_direction_query("yago:Worcestershire", "yago:Oxfordshire", "?geoWKT1", "?geoWKT2", "left"),
            "strdf:left",
            "strdf:left -> bbox maxX(left) < minX(right).",
        )

    if q == "Is county Cavan east of county Mayo?":
        return (
            jena_direction_query("yago:County_Cavan", "yago:County_Mayo", "?geoWKT1", "?geoWKT2", "right"),
            virtuoso_direction_query("yago:County_Cavan", "yago:County_Mayo", "?geoWKT1", "?geoWKT2", "right"),
            "strdf:right",
            "strdf:right -> bbox minX(left) > maxX(right).",
        )

    if q == "Is Dublin north of Kilkenny?":
        return (
            jena_direction_query("yago:Dublin", "yago:Kilkenny", "?geoWKT", "?geoWKT1", "above"),
            virtuoso_direction_query("yago:Dublin", "yago:Kilkenny", "?geoWKT", "?geoWKT1", "above"),
            "strdf:above",
            "strdf:above -> bbox minY(left) > maxY(right).",
        )

    return None


def post_sparql(
    endpoint: str,
    query: str,
    timeout: float,
    default_graph_uri: Optional[str] = None,
) -> Tuple[str, str, float]:
    form: Dict[str, str] = {"query": with_prefixes(query)}
    if default_graph_uri:
        form["default-graph-uri"] = default_graph_uri

    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Accept": "text/csv",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    started = time.perf_counter()
    try:
        with hard_timeout(timeout):
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                elapsed_ms = (time.perf_counter() - started) * 1000
                return body.strip(), "OK", elapsed_ms
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        elapsed_ms = (time.perf_counter() - started) * 1000
        return body.strip(), f"HTTP_{exc.code}", elapsed_ms
    except HardTimeout:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return f"timeout after {timeout:.1f}s", "TIMEOUT", elapsed_ms
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return str(exc), "ERROR", elapsed_ms


def iter_rows(path: Path) -> Iterable[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--jena-endpoint",
        default="http://127.0.0.1:13030/yago2geo/sparql",
    )
    parser.add_argument(
        "--virtuoso-endpoint",
        default="http://127.0.0.1:18890/sparql",
    )
    parser.add_argument(
        "--virtuoso-graph",
        default="http://yago-knowledge.org/graph/yago2geo",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    candidates = []
    for row in iter_rows(args.input):
        rewritten = rewritten_queries(row)
        if rewritten is not None:
            candidates.append((row, *rewritten))

    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        for idx, (row, jena_query, virtuoso_query, function_name, note) in enumerate(candidates, 1):
            print(
                f"[{idx}/{len(candidates)}] {function_name} {row['자연어질문'][:80]}",
                file=sys.stderr,
                flush=True,
            )
            jena_result, jena_status, jena_ms = post_sparql(
                args.jena_endpoint,
                jena_query,
                args.timeout,
            )
            virtuoso_result, virtuoso_status, virtuoso_ms = post_sparql(
                args.virtuoso_endpoint,
                virtuoso_query,
                args.timeout,
                default_graph_uri=args.virtuoso_graph,
            )

            writer.writerow(
                {
                    "category": row["category"],
                    "자연어질문": row["자연어질문"],
                    "원본 SPARQL 쿼리": row["SPARQL 쿼리"],
                    "rewrite 대상 함수": function_name,
                    "Jena rewrite SPARQL 쿼리": jena_query,
                    "Virtuoso rewrite SPARQL 쿼리": virtuoso_query,
                    "rewrite note": note,
                    "원래 정답": row["원래 정답"],
                    "jena 쿼리 수행결과 정답": jena_result,
                    "jena 실행시간": f"{jena_ms:.3f}",
                    "jena status": jena_status,
                    "virtuoso 쿼리 수행결과 정답": virtuoso_result,
                    "virtuoso 실행시간": f"{virtuoso_ms:.3f}",
                    "virtuoso status": virtuoso_status,
                }
            )
            f.flush()

    print(args.output, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
