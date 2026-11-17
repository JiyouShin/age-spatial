#!/usr/bin/env python3
"""Run converted age-spatial GeoQuestions queries and write result CSV."""

from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
PSQL = ROOT / "benchmark" / "scripts" / "psql.sh"

OUTPUT_FIELDS = [
    "category",
    "자연어질문",
    "SPARQL 쿼리",
    "AGE쿼리",
    "원래 정답",
    "age 쿼리 수행결과 정답",
    "실행시간",
]


def read_rows(path: Path, ids: Optional[Set[str]]) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows = [row for row in rows if row.get("status") == "converted"]
    if ids is not None:
        rows = [row for row in rows if row["id"] in ids]
    return rows


def result_sql(row: Dict[str, str], timeout: int) -> str:
    wrapper = row["age_sql_wrapper"].rstrip()
    return f"SET statement_timeout = '{timeout}s';\n{wrapper}\n"


def run_one(row: Dict[str, str], timeout: int) -> Tuple[str, float]:
    sql = result_sql(row, timeout)
    temp_dir = ROOT / "benchmark" / "results" / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"geoquestions-age-spatial-full-q{row['id']}.sql"
    temp_path.write_text(sql, encoding="utf-8")
    container_path = f"/benchmark/results/tmp/{temp_path.name}"
    start = time.monotonic()
    proc = subprocess.Popen(
        [
            str(PSQL),
            "-X",
            "-q",
            "-A",
            "-F",
            ",",
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
        duration = (time.monotonic() - start) * 1000
        return f"TIMEOUT: timeout after {timeout}s", duration
    finally:
        temp_path.unlink(missing_ok=True)

    duration = (time.monotonic() - start) * 1000
    if proc.returncode != 0:
        message = (stderr or stdout or "").strip().replace("\r", "")
        return f"ERROR: {message}", duration
    return stdout.strip().replace("\r", ""), duration


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="benchmark/results/geoquestions/age_spatial_full/geoquestions_age_spatial_full_queries.csv",
    )
    parser.add_argument(
        "--output",
        default="benchmark/results/geoquestions/age_spatial_full/geoquestions_age_spatial_full_results.csv",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--ids",
        default="",
        help="comma-separated GeoQuestions ids to run; empty means all converted rows",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_path = ROOT / args.input
    output_path = ROOT / args.output
    ids = {value.strip() for value in args.ids.split(",") if value.strip()} or None
    rows = read_rows(input_path, ids)
    if args.limit:
        rows = rows[: args.limit]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            result, duration = run_one(row, args.timeout)
            writer.writerow(
                {
                    "category": row["category"],
                    "자연어질문": row["question"],
                    "SPARQL 쿼리": row["sparql"],
                    "AGE쿼리": row["age_sql_wrapper"],
                    "원래 정답": row["answer"],
                    "age 쿼리 수행결과 정답": result,
                    "실행시간": f"{duration:.3f}",
                }
            )
            print(f"{index}/{len(rows)} q{row['id']} {duration:.1f}ms", flush=True)

    print(f"wrote={output_path}")
    print(f"rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
