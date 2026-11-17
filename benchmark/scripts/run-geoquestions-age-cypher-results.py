#!/usr/bin/env python3
"""Run generated GeoQuestions AGE Cypher/SQL queries and save result CSV.

Input rows are produced by prepare-geoquestions-age-cypher.py or
prepare-geoquestions-age-cypher-broad.py. Only rows with status=converted are
executed. Broad rows execute their SQL wrapper directly.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path
import re
from typing import Dict, List, Optional, Sequence, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
PSQL = ROOT / "benchmark" / "scripts" / "psql.sh"


ENTITY_TOKEN_RE = r"(?:<https?://[^>\s]+>|(?:yago|y2geor):[^\s\{\};,\)]+|\?\w+)"
ANY_PREDICATE_TRIPLE_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN_RE})\s+"
    r"(?P<predicate>(?:rdf:type|(?:y2geoo|yago|geo|rdfs):[A-Za-z_][\w-]*))\s+"
    r"(?P<object>\?\w+|<https?://[^>]+>|\"[^\"]*\"(?:@\w+|\^\^[^\s]+)?|[^\s\.;\{\}]+)",
    re.IGNORECASE,
)

PROPERTY_MAP = {
    "hasGAG_Population",
    "GAG_Population",
    "POPULATION",
    "hasPopulation",
    "AREASQKM",
    "hasOSNI_AreaSqKM",
    "hasArea",
    "hasGAG_Name",
    "GAG_Name",
    "hasOSI_Name",
    "hasOS_Name",
    "hasOSNI_Name",
    "hasONSI_Name",
    "hasOSM_Name",
    "hasGADM_Name",
    "GNIS_NAME",
    "hasGAG_ID",
    "GAG_ID",
    "hasOSI_ID",
    "hasOS_ID",
    "hasOSNI_ID",
    "hasOSM_ID",
    "hasGADM_ID",
    "GLOBALID",
    "GNIS_ID",
    "STATE_NAME",
    "STATE_FIPS",
    "COUNTY_FIP",
    "STCO_FIPSC",
    "PLACE_FIPS",
    "hasGADM_Description",
    "hasGADM_NationalLevel",
    "hasGADM_UpperLevelUnit",
}


OUTPUT_FIELDS = [
    "id",
    "resolved_id",
    "category",
    "country_scope",
    "countries",
    "kind",
    "spatial_functions",
    "자연어질문",
    "기존_SPARQL_쿼리",
    "ApacheAGE_변환쿼리",
    "기존_정답",
    "ApacheAGE_쿼리_실행_결과",
    "executiontime_ms",
    "execution_status",
    "duration_ms",
    "row_count",
    "error",
    "age_sql_wrapper",
]


def read_answers(path: Path) -> Dict[int, str]:
    answers: Dict[int, str] = {}
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(qid): str(answer) for qid, answer in data.items()}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for idx, row in enumerate(reader, 1):
            answers[idx] = row.get("Answer", "")
    return answers


def read_converted(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [
        row
        for row in rows
        if row.get("status") == "converted" and not uses_base_yago_predicate(row.get("sparql", ""))
    ]


def uses_base_yago_predicate(sparql: str) -> bool:
    for match in ANY_PREDICATE_TRIPLE_RE.finditer(sparql):
        predicate = match.group("predicate")
        if predicate.lower() in {"rdf:type", "geo:hasgeometry", "geo:aswkt"}:
            continue
        if predicate.startswith("rdfs:"):
            return True
        if predicate.startswith("yago:"):
            local_name = predicate.split(":", 1)[1]
            if local_name not in PROPERTY_MAP:
                return True
    return False


def result_sql(row: Dict[str, str]) -> str:
    if row.get("age_sql_wrapper"):
        return row["age_sql_wrapper"].rstrip() + "\n"

    cypher = row["age_cypher"]
    return (
        "LOAD 'age';\n"
        "SET search_path TO ag_catalog, public;\n"
        "WITH q AS (\n"
        "    SELECT *\n"
        "    FROM ag_catalog.cypher('yago2geo', $$\n"
        f"{cypher}\n"
        "$$) AS (result agtype)\n"
        "), rows AS (\n"
        "    SELECT result::text AS row_text\n"
        "    FROM q\n"
        ")\n"
        "SELECT COALESCE(string_agg(row_text, E'\\n' ORDER BY row_text), '')\n"
        "FROM rows;\n"
    )


def run_one(row: Dict[str, str], timeout: int) -> Tuple[str, str, int, str, float]:
    sql = result_sql(row)
    start = time.monotonic()
    temp_dir = ROOT / "benchmark" / "results" / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"geoquestions-age-{row['id']}.sql"
    temp_path.write_text(sql, encoding="utf-8")
    container_path = f"/benchmark/results/tmp/{temp_path.name}"

    try:
        proc = subprocess.run(
            [
                str(PSQL),
                "-X",
                "-q",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                container_path,
            ],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        duration = (time.monotonic() - start) * 1000
        return "timeout", "", 0, f"timeout after {timeout}s", duration
    finally:
        temp_path.unlink(missing_ok=True)

    duration = (time.monotonic() - start) * 1000
    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout).strip()
        return "error", "", 0, error, duration

    output = proc.stdout.strip()
    # -q suppresses command tags. The actual query returns exactly one text
    # cell containing zero or more JSON rows joined by newlines.
    row_count = 0 if output == "" else output.count("\n") + 1
    return "ok", output, row_count, "", duration


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run converted GeoQuestions AGE Cypher queries and write CSV results."
    )
    parser.add_argument(
        "--input",
        default="benchmark/results/geoquestions/cypher/geoquestions_spatial_age_cypher.csv",
        help="converted AGE Cypher CSV",
    )
    parser.add_argument(
        "--geoquestions",
        default="benchmark/queries/GeoQuestions1089.csv",
        help="original GeoQuestions CSV with answers",
    )
    parser.add_argument(
        "--answers",
        default="benchmark/queries/GeoQuestions1089Answers.json",
        help="GeoQuestions answers JSON or CSV",
    )
    parser.add_argument(
        "--output",
        default="benchmark/results/geoquestions/cypher/geoquestions_spatial_age_cypher_results.csv",
        help="output result CSV",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="per-query timeout in seconds",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="execute only the first N converted rows; 0 means all rows",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_path = ROOT / args.input
    output_path = ROOT / args.output
    answers_path = ROOT / args.answers
    answers = read_answers(answers_path if answers_path.exists() else ROOT / args.geoquestions)
    rows = read_converted(input_path)
    if args.limit > 0:
        rows = rows[: args.limit]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok = error = timeout = 0
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            status, result, row_count, err, duration_ms = run_one(row, args.timeout)
            if status == "ok":
                ok += 1
            elif status == "timeout":
                timeout += 1
            else:
                error += 1

            qid = int(row["id"])
            writer.writerow(
                {
                    "id": row["id"],
                    "resolved_id": row["resolved_id"],
                    "category": row.get("category", ""),
                    "country_scope": row.get("country_scope", ""),
                    "countries": row.get("countries", ""),
                    "kind": row.get("kind", ""),
                    "spatial_functions": row["spatial_functions"],
                    "자연어질문": row["question"],
                    "기존_SPARQL_쿼리": row["sparql"],
                    "ApacheAGE_변환쿼리": row.get("age_sql_wrapper") or row.get("age_cypher", ""),
                    "기존_정답": row.get("existing_answer") or answers.get(qid, ""),
                    "ApacheAGE_쿼리_실행_결과": result,
                    "executiontime_ms": f"{duration_ms:.3f}",
                    "execution_status": status,
                    "duration_ms": f"{duration_ms:.3f}",
                    "row_count": str(row_count),
                    "error": err,
                    "age_sql_wrapper": row.get("age_sql_wrapper", ""),
                }
            )
            print(
                f"[{index}/{len(rows)}] q{row['id']} "
                f"{row.get('category', '')} {status} rows={row_count} {duration_ms:.1f}ms",
                flush=True,
            )

    print(f"wrote={output_path}")
    print(f"ok={ok} error={error} timeout={timeout}")
    return 0 if error == 0 and timeout == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
