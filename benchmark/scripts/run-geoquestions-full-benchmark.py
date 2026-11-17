#!/usr/bin/env python3
"""Run converted GeoQuestions on AGE/Jena/Virtuoso/Strabon.

The input is the full AGE conversion CSV produced by
prepare-geoquestions-age-spatial-full.py.  Rows with status=converted form the
shared evaluation set.  AGE executes the translated age-spatial Cypher wrapper;
RDF engines execute the original SPARQL.

The output is long-form: one row per (engine, question).  The file is rewritten
every --flush-every completed queries so partial results survive interruption.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import math
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
PSQL = ROOT / "benchmark" / "scripts" / "psql.sh"
TMP_DIR = ROOT / "benchmark" / "results" / "tmp"
DEFAULT_INPUT = (
    ROOT
    / "benchmark"
    / "results"
    / "geoquestions"
    / "age_spatial_full"
    / "geoquestions_age_spatial_full_queries.csv"
)
DEFAULT_OUTPUT_DIR = ROOT / "benchmark" / "results" / "geoquestions" / "full_benchmark"

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
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

SPARQL_NS = {"sr": "http://www.w3.org/2005/sparql-results#"}

OUTPUT_FIELDS = [
    "engine",
    "run_label",
    "engine_query_mode",
    "id",
    "category",
    "pattern",
    "spatial_functions",
    "question",
    "sparql_query",
    "engine_query",
    "expected_answer",
    "engine_result",
    "executiontime_ms",
    "execution_status",
    "match_status",
    "match_method",
    "expected_count",
    "result_count",
    "expected_normalized",
    "result_normalized",
    "error",
]

STATS_FIELDS = [
    "engine",
    "run_label",
    "group_by",
    "group_value",
    "total",
    "ok",
    "error",
    "timeout",
    "http_error",
    "other_status",
    "match",
    "mismatch",
    "not_run",
    "avg_executiontime_ms",
    "avg_executiontime_ms_ok",
    "avg_executiontime_ms_matched",
    "median_executiontime_ms",
    "min_executiontime_ms",
    "min_executiontime_ms_ok",
    "max_executiontime_ms",
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


def read_converted_rows(path: Path, ids: Optional[Set[str]]) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("status") == "converted"]
    if ids is not None:
        rows = [row for row in rows if row["id"] in ids]
    return rows


def default_output_path(engines: Sequence[str], run_label: str) -> Path:
    engine_name = "all" if len(engines) > 1 else engines[0]
    return DEFAULT_OUTPUT_DIR / f"geoquestions_full_{engine_name}_{run_label}_results.csv"


def default_stats_path(output_path: Path) -> Path:
    name = output_path.name
    if name.endswith("_results.csv"):
        return output_path.with_name(name[: -len("_results.csv")] + "_stats.csv")
    return output_path.with_name(output_path.stem + "_stats.csv")


def build_sparql(query_body: str) -> str:
    query = query_body.strip()
    if query.upper().startswith("PREFIX "):
        return query
    return PREFIXES + "\n" + query


def age_sql(row: Dict[str, str], timeout: float) -> str:
    return age_query_sql(row, timeout, include_session_setup=True)


def age_query_sql(row: Dict[str, str], timeout: float, include_session_setup: bool) -> str:
    wrapper = row["age_sql_wrapper"].rstrip()
    graph_name = getattr(age_sql, "graph_name", "")
    if graph_name:
        wrapper = re.sub(r"cypher\('yago2geo'", f"cypher('{graph_name}'", wrapper)
    if not include_session_setup:
        lines = []
        for line in wrapper.splitlines():
            stripped = line.strip()
            if stripped in {"LOAD 'age';", "SET search_path TO ag_catalog, public;"}:
                continue
            lines.append(line)
        wrapper = "\n".join(lines).strip()
    return f"SET statement_timeout = '{int(timeout)}s';\n{wrapper}\n"


def run_age(row: Dict[str, str], timeout: float) -> Tuple[str, str, float, str]:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = TMP_DIR / f"geoquestions-full-age-q{row['id']}.sql"
    temp_path.write_text(age_sql(row, timeout), encoding="utf-8")
    container_path = f"/benchmark/results/tmp/{temp_path.name}"

    started = time.perf_counter()
    proc = subprocess.Popen(
        [
            str(PSQL),
            "-X",
            "-q",
            "-A",
            "-F",
            ",",
            "-P",
            "footer=off",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            container_path,
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        elapsed_ms = (time.perf_counter() - started) * 1000
        temp_path.unlink(missing_ok=True)
        return "", "TIMEOUT", elapsed_ms, f"timeout after {timeout:.1f}s"

    elapsed_ms = (time.perf_counter() - started) * 1000
    temp_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        error = (stderr or stdout or "").strip().replace("\r", "")
        return "", "ERROR", elapsed_ms, error
    return stdout.strip().replace("\r", ""), "OK", elapsed_ms, ""


class AgePersistentSession:
    """Keep one psql session open so LOAD/SET setup is paid once."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen[str]] = None
        self.stdout_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self.reader_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.proc is not None:
            return
        self.proc = subprocess.Popen(
            [
                str(PSQL),
                "-X",
                "-q",
                "-A",
                "-F",
                ",",
                "-P",
                "footer=off",
                "-v",
                "ON_ERROR_STOP=0",
            ],
            cwd=str(ROOT),
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            start_new_session=True,
        )
        self.stdout_queue = queue.Queue()
        self.reader_thread = threading.Thread(target=self._read_stdout_loop, daemon=True)
        self.reader_thread.start()
        marker = "__AGE_SESSION_READY__"
        self._write(
            "LOAD 'age';\n"
            "SET search_path TO ag_catalog, public;\n"
            f"\\echo {marker}\n"
        )
        output = self._read_until(marker, 30)
        if "ERROR:" in output or self.proc.poll() is not None:
            raise RuntimeError(f"failed to initialize persistent AGE session: {output.strip()}")

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write("\\q\n")
                proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def run(self, row: Dict[str, str], timeout: float) -> Tuple[str, str, float, str]:
        self.start()
        marker = f"__AGE_QUERY_END_{row['id']}_{time.time_ns()}__"
        sql = age_query_sql(row, timeout, include_session_setup=False)
        started = time.perf_counter()
        self._write(f"{sql}\n\\echo {marker}\n")
        try:
            output = self._read_until(marker, timeout + 15)
        except subprocess.TimeoutExpired:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.close()
            return "", "TIMEOUT", elapsed_ms, f"timeout after {timeout:.1f}s"
        elapsed_ms = (time.perf_counter() - started) * 1000
        cleaned = output.replace("\r", "").strip()
        if "canceling statement due to statement timeout" in cleaned:
            return "", "TIMEOUT", elapsed_ms, cleaned
        if re.search(r"(^|\n)(psql:|ERROR:)", cleaned):
            return "", "ERROR", elapsed_ms, cleaned
        return cleaned, "OK", elapsed_ms, ""

    def _write(self, text: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("persistent AGE session is not running")
        self.proc.stdin.write(text)
        self.proc.stdin.flush()

    def _read_stdout_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.stdout_queue.put(line)
        finally:
            self.stdout_queue.put(None)

    def _read_until(self, marker: str, timeout: float) -> str:
        if self.proc is None:
            raise RuntimeError("persistent AGE session is not running")
        deadline = time.perf_counter() + timeout
        lines: List[str] = []
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("persistent psql", timeout)
            try:
                line = self.stdout_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError("persistent AGE session exited unexpectedly")
                continue
            if line is None:
                raise RuntimeError("persistent AGE session exited unexpectedly")
            if line.strip() == marker:
                return "".join(lines)
            lines.append(line)


def post_sparql_csv(
    endpoint: str,
    query: str,
    timeout: float,
    default_graph_uri: Optional[str] = None,
) -> Tuple[str, str, float, str]:
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
                return body.strip(), "OK", elapsed_ms, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        elapsed_ms = (time.perf_counter() - started) * 1000
        return body.strip(), f"HTTP_{exc.code}", elapsed_ms, body.strip()
    except HardTimeout:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return "", "TIMEOUT", elapsed_ms, f"timeout after {timeout:.1f}s"
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return "", "ERROR", elapsed_ms, str(exc)


def binding_text(binding: ET.Element) -> str:
    for child in list(binding):
        return child.text or ""
    return ""


def csv_text(headers: List[str], rows: List[List[str]]) -> str:
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
        return csv_text(["_askResult"], [[(boolean.text or "").strip()]]), "OK"
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
    return csv_text(variables, result_rows), "OK"


def post_strabon(endpoint: str, query: str, timeout: float) -> Tuple[str, str, float, str]:
    form = {"query": query}
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
                return result, status, elapsed_ms, "" if status == "OK" else result
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        elapsed_ms = (time.perf_counter() - started) * 1000
        result, status = normalize_strabon_response(body)
        if status == "OK":
            status = f"HTTP_{exc.code}"
        return result, status, elapsed_ms, result
    except HardTimeout:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return "", "TIMEOUT", elapsed_ms, f"timeout after {timeout:.1f}s"
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return "", "ERROR", elapsed_ms, str(exc)


def wait_strabon_endpoint(endpoint: str, timeout: float) -> str:
    query = "ASK WHERE { ?s ?p ?o }"
    form = {"query": query}
    data = urllib.parse.urlencode(form).encode("utf-8")
    deadline = time.perf_counter() + timeout
    last_error = ""
    while time.perf_counter() < deadline:
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Accept": "application/sparql-results+xml",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if 200 <= response.status < 300:
                    response.read(1)
                    return "ready"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    return f"not_ready:{last_error}"


def restart_strabon_endpoint(args: argparse.Namespace) -> str:
    started = time.perf_counter()
    proc = subprocess.run(
        ["docker", "restart", args.strabon_container],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=max(60, int(args.strabon_restart_timeout)),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        return f"restart_failed after {elapsed_ms:.1f}ms: {detail}"
    ready_status = wait_strabon_endpoint(args.strabon_endpoint, args.strabon_restart_timeout)
    total_ms = (time.perf_counter() - started) * 1000
    return f"restart={ready_status} in {total_ms:.1f}ms"


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cleanup_strabon_timeout(args: argparse.Namespace) -> str:
    db_names = [
        db.strip()
        for db in (args.strabon_cleanup_dbs or "").split(",")
        if db.strip()
    ]
    if not db_names:
        return "skipped:no cleanup dbs configured"

    db_list = ", ".join(sql_literal(db) for db in db_names)
    sql = f"""
WITH targets AS (
    SELECT pid, pg_terminate_backend(pid) AS terminated
    FROM pg_stat_activity
    WHERE datname IN ({db_list})
      AND pid <> pg_backend_pid()
      AND state <> 'idle'
)
SELECT COALESCE(sum(CASE WHEN terminated THEN 1 ELSE 0 END), 0)::text
       || '/' || count(*)::text
FROM targets;
""".strip()

    started = time.perf_counter()
    proc = subprocess.run(
        [
            "docker",
            "exec",
            args.strabon_postgis_container,
            "psql",
            "-U",
            args.strabon_user,
            "-d",
            "postgres",
            "-At",
            "-c",
            sql,
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        return f"failed after {elapsed_ms:.1f}ms: {detail}"
    detail = (proc.stdout or "").strip() or "0/0"
    cleanup_detail = f"terminated {detail} backend(s) in {elapsed_ms:.1f}ms"
    if args.strabon_restart_on_timeout:
        restart_detail = restart_strabon_endpoint(args)
        cleanup_detail = f"{cleanup_detail}; {restart_detail}"
    return cleanup_detail


def maybe_cleanup_strabon_timeout(
    args: argparse.Namespace,
    status: str,
    error: str,
) -> str:
    if status != "TIMEOUT" or not args.strabon_cleanup_on_timeout:
        return error
    try:
        cleanup_detail = cleanup_strabon_timeout(args)
    except Exception as exc:  # best-effort cleanup; do not hide query result
        cleanup_detail = f"failed: {exc}"
    separator = "; " if error else ""
    return f"{error}{separator}strabon_timeout_cleanup={cleanup_detail}"


def run_strabon_cmd(row: Dict[str, str], args: argparse.Namespace, query: str) -> Tuple[str, str, float, str]:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_name = f"geoquestions-full-strabon-q{row['id']}.rq"
    container_query = f"/tmp/{temp_name}"
    cmd = (
        f"cat > {container_query}; "
        f"cd /opt/strabon/runtime; "
        f"./strabon-cmd "
        f"-MM {int(args.strabon_memory_mult)} "
        f"-h {args.strabon_host} -p {int(args.strabon_db_port)} "
        f"-db {args.strabon_db} -u {args.strabon_user} -pass {args.strabon_password} "
        f"query \"$(cat {container_query})\" xml"
    )
    started = time.perf_counter()
    proc = subprocess.Popen(
        ["docker", "exec", "-i", args.strabon_container, "bash", "-lc", cmd],
        cwd=str(ROOT),
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=query, timeout=args.timeout + 10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        elapsed_ms = (time.perf_counter() - started) * 1000
        return "", "TIMEOUT", elapsed_ms, f"timeout after {args.timeout:.1f}s"

    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        error = (stderr or stdout or "").strip().replace("\r", "")
        return "", "ERROR", elapsed_ms, error
    result, status = normalize_strabon_response(stdout)
    return result, status, elapsed_ms, "" if status == "OK" else result


def is_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


NUMERIC_MATCH_DECIMALS = 3


def normalize_numeric_for_match(value: str) -> str:
    number = float(value)
    if math.isfinite(number):
        return f"{number:.{NUMERIC_MATCH_DECIMALS}f}"
    return value


def normalize_row_for_match(row: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(normalize_numeric_for_match(cell) if is_numeric(cell) else cell for cell in row)


def has_numeric_cell(rows: Sequence[Tuple[str, ...]]) -> bool:
    return any(is_numeric(cell) for row in rows for cell in row)


def normalize_cell(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("[") and "](" in value and value.endswith(")"):
        value = value[value.rfind("(") + 1 : -1]
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    typed = re.match(r'^"(.*)"\^\^.+$', value)
    if typed:
        value = typed.group(1)
    lang = re.match(r'^"(.*)"@[A-Za-z-]+$', value)
    if lang:
        value = lang.group(1)
    if is_numeric(value):
        number = float(value)
        if math.isfinite(number):
            return f"{number:.12g}"
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered
    return value


def looks_like_header(row: Sequence[str]) -> bool:
    if not row:
        return False
    for cell in row:
        value = cell.strip()
        if value.startswith("http://") or value.startswith("https://"):
            return False
        if is_numeric(value):
            return False
        if value.lower() in {"true", "false"}:
            return False
        if value.startswith('"http://') or value.startswith('"https://'):
            return False
    return True


def parse_table(text: str) -> Tuple[str, List[Tuple[str, ...]]]:
    text = (text or "").strip()
    if not text:
        return "table", []
    boolean = re.search(r"['\"]boolean['\"]\s*:\s*(True|False|true|false)", text)
    if boolean:
        return "ask_boolean", [(boolean.group(1).lower(),)]
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        line
        for line in normalized.split("\n")
        if line.strip() and not re.fullmatch(r"\(\d+\s+rows?\)", line.strip())
    ]
    if not lines:
        return "table", []
    try:
        parsed = list(csv.reader(lines))
    except csv.Error:
        parsed = [[line] for line in lines]
    if parsed and looks_like_header(parsed[0]):
        parsed = parsed[1:]
    rows = [tuple(normalize_cell(cell) for cell in row) for row in parsed]
    if len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] in {"true", "false"}:
        return "ask_boolean", rows
    return "table", rows


def normalize_ask_rows(rows: Sequence[Tuple[str, ...]]) -> List[Tuple[str, ...]]:
    normalized: List[Tuple[str, ...]] = []
    for row in rows:
        if len(row) != 1:
            normalized.append(tuple(row))
            continue
        value = row[0].strip().lower()
        if value in {"1", "t"}:
            normalized.append(("true",))
        elif value in {"0", "f"}:
            normalized.append(("false",))
        else:
            normalized.append((value,))
    return normalized


def compare_answers(expected: str, actual: str, status: str) -> Tuple[str, str, int, int, str, str]:
    expected_kind, expected_rows = parse_table(expected)
    actual_kind, actual_rows = parse_table(actual)
    expected_json = json.dumps(expected_rows, ensure_ascii=False, sort_keys=True)
    actual_json = json.dumps(actual_rows, ensure_ascii=False, sort_keys=True)
    if status != "OK":
        return "not_run", "execution_failed", len(expected_rows), 0, expected_json, actual_json
    if expected_kind == "ask_boolean" or actual_kind == "ask_boolean":
        expected_bool_rows = normalize_ask_rows(expected_rows)
        actual_bool_rows = normalize_ask_rows(actual_rows)
        if expected_bool_rows == actual_bool_rows:
            return "match", "ask_boolean", len(expected_rows), len(actual_rows), expected_json, actual_json
        return "mismatch", "ask_boolean", len(expected_rows), len(actual_rows), expected_json, actual_json
    if len(expected_rows) == len(actual_rows) == 1 and len(expected_rows[0]) == len(actual_rows[0]) == 1:
        e, a = expected_rows[0][0], actual_rows[0][0]
        if is_numeric(e) and is_numeric(a) and normalize_numeric_for_match(e) == normalize_numeric_for_match(a):
            return "match", "numeric_single_3dp", 1, 1, expected_json, actual_json
    expected_set = set(expected_rows)
    actual_set = set(actual_rows)
    if expected_set == actual_set:
        return "match", "row_set", len(expected_rows), len(actual_rows), expected_json, actual_json
    expected_match_set = {normalize_row_for_match(row) for row in expected_rows}
    actual_match_set = {normalize_row_for_match(row) for row in actual_rows}
    if expected_match_set == actual_match_set and has_numeric_cell(expected_rows + actual_rows):
        return "match", "row_set_numeric_3dp", len(expected_rows), len(actual_rows), expected_json, actual_json
    return "mismatch", "row_set", len(expected_rows), len(actual_rows), expected_json, actual_json


def write_results(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def numeric_times(rows: Sequence[Dict[str, str]]) -> List[float]:
    values: List[float] = []
    for row in rows:
        try:
            values.append(float(row.get("executiontime_ms", "")))
        except ValueError:
            pass
    return values


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def status_bucket(status: str) -> str:
    if status == "OK":
        return "ok"
    if status in {"ERROR", "ERROR_RESPONSE"}:
        return "error"
    if status == "TIMEOUT":
        return "timeout"
    if status.startswith("HTTP_"):
        return "http_error"
    return "other_status"


def summarize_group(
    rows: Sequence[Dict[str, str]],
    engine: str,
    run_label: str,
    group_by: str,
    group_value: str,
) -> Dict[str, str]:
    times = numeric_times(rows)
    ok_times = numeric_times([row for row in rows if row.get("execution_status") == "OK"])
    matched_times = numeric_times(
        [
            row
            for row in rows
            if row.get("execution_status") == "OK" and row.get("match_status") == "match"
        ]
    )
    summary = {
        "engine": engine,
        "run_label": run_label,
        "group_by": group_by,
        "group_value": group_value,
        "total": str(len(rows)),
        "ok": "0",
        "error": "0",
        "timeout": "0",
        "http_error": "0",
        "other_status": "0",
        "match": "0",
        "mismatch": "0",
        "not_run": "0",
        "avg_executiontime_ms": f"{(sum(times) / len(times)):.3f}" if times else "0.000",
        "avg_executiontime_ms_ok": f"{(sum(ok_times) / len(ok_times)):.3f}" if ok_times else "0.000",
        "avg_executiontime_ms_matched": f"{(sum(matched_times) / len(matched_times)):.3f}" if matched_times else "0.000",
        "median_executiontime_ms": f"{median(times):.3f}" if times else "0.000",
        "min_executiontime_ms": f"{min(times):.3f}" if times else "0.000",
        "min_executiontime_ms_ok": f"{min(ok_times):.3f}" if ok_times else "0.000",
        "max_executiontime_ms": f"{max(times):.3f}" if times else "0.000",
    }
    for row in rows:
        bucket = status_bucket(row.get("execution_status", ""))
        summary[bucket] = str(int(summary[bucket]) + 1)
        match = row.get("match_status", "")
        if match in {"match", "mismatch", "not_run"}:
            summary[match] = str(int(summary[match]) + 1)
    return summary


def build_stats(rows: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    stats: List[Dict[str, str]] = []
    engines = sorted({row.get("engine", "") for row in rows})
    for engine in engines:
        engine_rows = [row for row in rows if row.get("engine", "") == engine]
        run_labels = sorted({row.get("run_label", "") for row in engine_rows})
        for run_label in run_labels:
            label_rows = [row for row in engine_rows if row.get("run_label", "") == run_label]
            stats.append(summarize_group(label_rows, engine, run_label, "overall", "all"))
            for group_by in ("category", "pattern", "spatial_functions"):
                values = sorted({row.get(group_by, "") for row in label_rows})
                for value in values:
                    group_rows = [row for row in label_rows if row.get(group_by, "") == value]
                    stats.append(summarize_group(group_rows, engine, run_label, group_by, value))
    return stats


def write_stats(path: Path, rows: Sequence[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATS_FIELDS)
        writer.writeheader()
        writer.writerows(build_stats(rows))


def write_outputs(output_path: Path, stats_path: Path, rows: List[Dict[str, str]]) -> None:
    write_results(output_path, rows)
    write_stats(stats_path, rows)


def load_existing(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_virtuoso_queries(path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    if not path:
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["id"]: row for row in csv.DictReader(handle)}


def run_engine(
    engine: str,
    row: Dict[str, str],
    args: argparse.Namespace,
) -> Tuple[str, str, float, str, str]:
    if engine == "age":
        age_sql.graph_name = args.age_graph
        if getattr(args, "age_session", None) is not None:
            result, status, elapsed_ms, error = args.age_session.run(row, args.timeout)
            query = age_query_sql(row, args.timeout, include_session_setup=False)
        else:
            result, status, elapsed_ms, error = run_age(row, args.timeout)
            query = row["age_sql_wrapper"]
        if args.age_graph:
            query = re.sub(r"cypher\('yago2geo'", f"cypher('{args.age_graph}'", query)
        query_mode = (
            "age_cypher_persistent_session"
            if getattr(args, "age_session", None) is not None
            else "age_cypher"
        )
        return result, status, elapsed_ms, error, query, query_mode
    query = build_sparql(row["sparql"])
    query_mode = "original_geosparql"
    if engine == "jena":
        result, status, elapsed_ms, error = post_sparql_csv(args.jena_endpoint, query, args.timeout)
    elif engine == "virtuoso":
        virt_row = args.virtuoso_query_map.get(row["id"], {})
        if virt_row.get("virtuoso_query"):
            query = virt_row["virtuoso_query"]
            query_mode = virt_row.get("virtuoso_mode") or "virtuoso_custom"
        result, status, elapsed_ms, error = post_sparql_csv(
            args.virtuoso_endpoint,
            query,
            args.timeout,
            default_graph_uri=None if virt_row.get("virtuoso_query") else args.virtuoso_graph,
        )
    elif engine == "strabon":
        if args.strabon_mode == "cmd":
            result, status, elapsed_ms, error = run_strabon_cmd(row, args, query)
            query_mode = f"strabon_cmd:{args.strabon_db}"
        else:
            result, status, elapsed_ms, error = post_strabon(args.strabon_endpoint, query, args.timeout)
        error = maybe_cleanup_strabon_timeout(args, status, error)
    else:
        raise ValueError(f"unsupported engine: {engine}")
    return result, status, elapsed_ms, error, query, query_mode


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--engines",
        default="age",
        help="comma-separated engines: age,jena,virtuoso,strabon",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--stats-output",
        type=Path,
        default=None,
        help="statistics CSV path; defaults to output path with _stats.csv suffix",
    )
    parser.add_argument("--run-label", default="noindex")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--flush-every", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="", help="comma-separated GeoQuestions ids")
    parser.add_argument("--resume", action="store_true", help="skip engine/qid pairs already in output")
    parser.add_argument("--jena-endpoint", default="http://127.0.0.1:13030/yago2geo/sparql")
    parser.add_argument("--virtuoso-endpoint", default="http://127.0.0.1:18890/sparql")
    parser.add_argument("--virtuoso-graph", default="http://yago-knowledge.org/graph/yago2geo")
    parser.add_argument("--virtuoso-query-file", type=Path, default=None)
    parser.add_argument("--strabon-endpoint", default="http://127.0.0.1:18080/Strabon/Query")
    parser.add_argument("--strabon-mode", choices=["endpoint", "cmd"], default="endpoint")
    parser.add_argument("--strabon-container", default="yago2geo-strabon")
    parser.add_argument("--strabon-host", default="strabon-postgis")
    parser.add_argument("--strabon-db-port", type=int, default=5432)
    parser.add_argument("--strabon-db", default="endpoint")
    parser.add_argument("--strabon-user", default="postgres")
    parser.add_argument("--strabon-password", default="postgres")
    parser.add_argument("--strabon-memory-mult", type=int, default=24)
    parser.add_argument("--strabon-cleanup-on-timeout", action="store_true")
    parser.add_argument("--strabon-restart-on-timeout", action="store_true")
    parser.add_argument("--strabon-restart-timeout", type=float, default=180.0)
    parser.add_argument("--strabon-postgis-container", default="yago2geo-strabon-postgis")
    parser.add_argument(
        "--strabon-cleanup-dbs",
        default="endpoint,yago2geo_uk_subset",
        help="comma-separated PostGIS databases whose active backends are terminated after a Strabon timeout",
    )
    parser.add_argument("--age-graph", default="")
    parser.add_argument(
        "--age-session-mode",
        choices=["persistent", "per-query"],
        default="persistent",
        help=(
            "persistent keeps one psql session open and runs LOAD/SET once; "
            "per-query preserves the old psql-per-query behavior"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    engines = [engine.strip().lower() for engine in args.engines.split(",") if engine.strip()]
    allowed = {"age", "jena", "virtuoso", "strabon"}
    unknown = sorted(set(engines) - allowed)
    if unknown:
        raise SystemExit(f"unknown engines: {', '.join(unknown)}")

    output_path = args.output or default_output_path(engines, args.run_label)
    stats_path = args.stats_output or default_stats_path(output_path)
    args.virtuoso_query_map = load_virtuoso_queries(args.virtuoso_query_file)
    args.age_session = None
    ids = {value.strip() for value in args.ids.split(",") if value.strip()} or None
    questions = read_converted_rows(args.input, ids)
    if args.limit:
        questions = questions[: args.limit]

    question_ids = {row["id"] for row in questions}
    results = load_existing(output_path) if args.resume else []
    if args.resume:
        results = [
            row
            for row in results
            if row.get("engine") in engines and row.get("id") in question_ids
        ]
        for row in results:
            row["run_label"] = args.run_label
    done = {(row["engine"], row["id"]) for row in results if row.get("execution_status")}
    total = len(questions) * len(engines)
    completed_this_run = 0

    print(f"input={args.input}", flush=True)
    print(f"output={output_path}", flush=True)
    print(f"stats_output={stats_path}", flush=True)
    print(f"questions={len(questions)} engines={','.join(engines)} total_runs={total}", flush=True)

    try:
        if "age" in engines and args.age_session_mode == "persistent":
            args.age_session = AgePersistentSession()
            args.age_session.start()

        for engine in engines:
            for q_index, row in enumerate(questions, 1):
                key = (engine, row["id"])
                if args.resume and key in done:
                    continue
                started_label = f"[{completed_this_run + 1}/{total}] {engine} q{row['id']} {row['category']}"
                print(f"{started_label} running...", flush=True)
                result, status, elapsed_ms, error, engine_query, engine_query_mode = run_engine(engine, row, args)
                match_status, match_method, expected_count, result_count, expected_norm, result_norm = compare_answers(
                    row["answer"],
                    result,
                    status,
                )
                out_row = {
                    "engine": engine,
                    "run_label": args.run_label,
                    "engine_query_mode": engine_query_mode,
                    "id": row["id"],
                    "category": row["category"],
                    "pattern": row["pattern"],
                    "spatial_functions": row["spatial_functions"],
                    "question": row["question"],
                    "sparql_query": row["sparql"],
                    "engine_query": engine_query,
                    "expected_answer": row["answer"],
                    "engine_result": result,
                    "executiontime_ms": f"{elapsed_ms:.3f}",
                    "execution_status": status,
                    "match_status": match_status,
                    "match_method": match_method,
                    "expected_count": str(expected_count),
                    "result_count": str(result_count),
                    "expected_normalized": expected_norm,
                    "result_normalized": result_norm,
                    "error": error,
                }
                if args.resume:
                    results = [existing for existing in results if (existing["engine"], existing["id"]) != key]
                results.append(out_row)
                completed_this_run += 1
                print(
                    f"{started_label} status={status} match={match_status} "
                    f"rows={result_count}/{expected_count} time={elapsed_ms:.1f}ms",
                    flush=True,
                )
                if args.flush_every > 0 and completed_this_run % args.flush_every == 0:
                    write_outputs(output_path, stats_path, results)
                    print(
                        f"checkpoint wrote {len(results)} result rows -> {output_path}; "
                        f"stats -> {stats_path}",
                        flush=True,
                    )
    finally:
        if args.age_session is not None:
            args.age_session.close()

    write_outputs(output_path, stats_path, results)
    counts: Dict[Tuple[str, str, str], int] = {}
    for row in results:
        key = (row["engine"], row["execution_status"], row["match_status"])
        counts[key] = counts.get(key, 0) + 1
    print(f"wrote={output_path}", flush=True)
    print(f"stats={stats_path}", flush=True)
    for (engine, status, match_status), count in sorted(counts.items()):
        print(f"{engine} status={status} match={match_status} count={count}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
