#!/usr/bin/env python3
"""Run representative GeoQuestions SPARQL queries on Strabon.

This mirrors the Jena/Virtuoso representative runner, but targets the Strabon
endpoint and normalizes Strabon's SPARQL XML response into a compact CSV-like
text cell.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


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
    / "rdf_representative"
    / "geoquestions_jena_virtuoso_representative_results.csv"
)
DEFAULT_OUTPUT = (
    ROOT
    / "benchmark"
    / "results"
    / "geoquestions"
    / "rdf_representative"
    / "geoquestions_strabon_representative_results.csv"
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
    "strabon 쿼리 수행결과 정답",
    "strabon 실행시간",
    "strabon status",
]


SPARQL_NS = {"sr": "http://www.w3.org/2005/sparql-results#"}


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


def iter_rows(path: Path) -> Iterable[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def binding_text(binding: ET.Element) -> str:
    for child in list(binding):
        if child.text is not None:
            return child.text
        return ""
    return ""


def to_csv_text(headers: List[str], rows: List[List[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return out.getvalue().rstrip("\n")


def normalize_strabon_response(body: str) -> Tuple[str, str]:
    stripped = body.strip()
    if not stripped:
        return "", "OK"

    try:
        root = ET.fromstring(stripped)
    except ET.ParseError:
        return stripped, "OK"

    if root.tag == "response":
        exception = root.find("exception")
        if exception is not None:
            return "".join(exception.itertext()).strip(), "ERROR_RESPONSE"
        return stripped, "OK"

    boolean = root.find("sr:boolean", SPARQL_NS)
    if boolean is not None:
        return to_csv_text(["_askResult"], [[(boolean.text or "").strip()]]), "OK"

    variables = [
        var.attrib["name"]
        for var in root.findall("sr:head/sr:variable", SPARQL_NS)
        if "name" in var.attrib
    ]
    result_rows: List[List[str]] = []
    for result in root.findall("sr:results/sr:result", SPARQL_NS):
        values: Dict[str, str] = {}
        for binding in result.findall("sr:binding", SPARQL_NS):
            name = binding.attrib.get("name")
            if name:
                values[name] = binding_text(binding)
        result_rows.append([values.get(var, "") for var in variables])

    if variables:
        return to_csv_text(variables, result_rows), "OK"

    return stripped, "OK"


def post_sparql(endpoint: str, query: str, timeout: float) -> Tuple[str, str, float]:
    form: Dict[str, str] = {"query": query}
    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Accept": "application/sparql-results+xml",
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
                result, status = normalize_strabon_response(body)
                return result, status, elapsed_ms
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        elapsed_ms = (time.perf_counter() - started) * 1000
        result, status = normalize_strabon_response(body)
        if status == "OK":
            status = f"HTTP_{exc.code}"
        return result, status, elapsed_ms
    except HardTimeout:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return f"timeout after {timeout:.1f}s", "TIMEOUT", elapsed_ms
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return str(exc), "ERROR", elapsed_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--strabon-endpoint",
        default="http://127.0.0.1:18080/Strabon/Query",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
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

            result, status, elapsed_ms = post_sparql(
                args.strabon_endpoint,
                query,
                args.timeout,
            )

            writer.writerow(
                {
                    "category": category,
                    "자연어질문": question,
                    "SPARQL 쿼리": sparql_body,
                    "원래 정답": row["원래 정답"],
                    "strabon 쿼리 수행결과 정답": result,
                    "strabon 실행시간": f"{elapsed_ms:.3f}",
                    "strabon status": status,
                }
            )
            f.flush()

    print(args.output, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
