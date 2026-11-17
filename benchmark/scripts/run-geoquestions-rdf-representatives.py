#!/usr/bin/env python3
"""Run representative GeoQuestions SPARQL queries on Jena and Virtuoso.

The input is the 21-row representative CSV produced for the age-spatial
experiment. This runner keeps the original SPARQL body intact, adds the common
GeoQuestions prefixes, and records each RDF engine's raw CSV response, elapsed
time, and execution status.
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
    / "geoquestions_jena_virtuoso_representative_results.csv"
)


PREFIXES = """\
PREFIX yago: <http://yago-knowledge.org/resource/>
PREFIX y2geoo: <http://kr.di.uoa.gr/yago2geo/ontology/>
PREFIX yago2geo: <http://kr.di.uoa.gr/yago2geo/ontology/>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX geof: <http://www.opengis.net/def/function/geosparql/>
PREFIX uom: <http://www.opengis.net/def/uom/OGC/1.0/>
PREFIX strdf: <http://strdf.di.uoa.gr/ontology#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""


OUTPUT_FIELDS = [
    "category",
    "자연어질문",
    "SPARQL 쿼리",
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


def build_query(query_body: str) -> str:
    query = query_body.strip()
    if query.upper().startswith("PREFIX "):
        return query
    return PREFIXES + "\n" + query


def post_sparql(
    endpoint: str,
    query: str,
    timeout: float,
    default_graph_uri: Optional[str] = None,
) -> Tuple[str, str, float]:
    form: Dict[str, str] = {"query": query}
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
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    rows = list(iter_rows(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        for idx, row in enumerate(rows, 1):
            category = row["category"]
            question = row["자연어질문"]
            sparql_body = row["SPARQL 쿼리"]
            query = build_query(sparql_body)

            print(
                f"[{idx}/{len(rows)}] {category} {question[:80]}",
                file=sys.stderr,
                flush=True,
            )

            jena_result, jena_status, jena_ms = post_sparql(
                args.jena_endpoint,
                query,
                args.timeout,
            )
            virtuoso_result, virtuoso_status, virtuoso_ms = post_sparql(
                args.virtuoso_endpoint,
                query,
                args.timeout,
                default_graph_uri=args.virtuoso_graph,
            )

            writer.writerow(
                {
                    "category": category,
                    "자연어질문": question,
                    "SPARQL 쿼리": sparql_body,
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
