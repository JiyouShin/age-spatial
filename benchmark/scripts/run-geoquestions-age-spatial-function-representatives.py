#!/usr/bin/env python3
"""Run representative GeoQuestions spatial-function queries with age-spatial.

This script deliberately targets GeoQuestions whose SPARQL uses spatial
functions such as geof:sfWithin(), strdf:area(), and geof:distance(). It does
not target precomputed RDF topological relation predicates as benchmark
evidence, because the point is to exercise PostGIS function calls inside AGE
Cypher through age-spatial.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
PSQL = ROOT / "benchmark" / "scripts" / "psql.sh"
GRAPH = "yago2geo"
YAGO = "http://yago-knowledge.org/resource/"


OUTPUT_FIELDS = [
    "category",
    "자연어질문",
    "SPARQL 쿼리",
    "AGE쿼리",
    "원래 정답",
    "age 쿼리 수행결과 정답",
    "실행시간",
]


@dataclass(frozen=True)
class Representative:
    qid: int
    cypher: str
    signature: str


def q(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def uri(var: str, value: str) -> str:
    return f"{var}.uri = {q(value)}"


def geom(var: str) -> str:
    return f"{var}.geom"


def geom_4326(var: str) -> str:
    return f"ST_Transform({var}.geom, 4326)"


def geom_m(var: str) -> str:
    return f"ST_Transform({var}.geom, 3857)"


def sql_wrapper(cypher: str, signature: str) -> str:
    return (
        "LOAD 'age';\n"
        "SET search_path TO ag_catalog, public;\n"
        f"SELECT * FROM ag_catalog.cypher('{GRAPH}', $$\n"
        f"{cypher}\n"
        f"$$) AS ({signature});"
    )


def ask_two(
    left_uri: str,
    right_uri: str,
    predicate: str,
    left_var: str = "a",
    right_var: str = "b",
) -> str:
    return "\n".join(
        [
            f"MATCH ({left_var})",
            f"WHERE {uri(left_var, left_uri)}",
            f"WITH {left_var}",
            f"MATCH ({right_var})",
            f"WHERE {uri(right_var, right_uri)}",
            f"WITH {left_var}, {right_var}",
            f"WHERE {predicate}",
            "RETURN count(*) > 0 AS answer",
        ]
    )


def representatives() -> List[Representative]:
    county_union = [
        "MATCH (anchor)",
        f"WHERE {uri('anchor', YAGO + 'County_Mayo')}",
        "WITH anchor",
        "MATCH (county:OSI_County_Council)",
        f"WHERE ST_Touches({geom('anchor')}, {geom('county')})",
        "RETURN DISTINCT county.uri AS county",
        "UNION",
        "MATCH (anchor)",
        f"WHERE {uri('anchor', YAGO + 'County_Mayo')}",
        "WITH anchor",
        "MATCH (county:OSI_City_and_County_Council)",
        f"WHERE ST_Touches({geom('anchor')}, {geom('county')})",
        "RETURN DISTINCT county.uri AS county",
    ]

    return [
        Representative(
            6,
            "\n".join(
                [
                    "MATCH (n)",
                    f"WHERE {uri('n', YAGO + 'County_Galway')}",
                    "RETURN ST_Area(n.geom) AS area",
                ]
            ),
            "area agtype",
        ),
        Representative(
            144,
            ask_two(
                YAGO + "Crete",
                YAGO + "Thessaly",
                f"ST_YMax({geom_4326('a')}) < ST_YMin({geom_4326('b')})",
            ),
            "answer agtype",
        ),
        Representative(
            145,
            ask_two(
                YAGO + "Worcestershire",
                YAGO + "Oxfordshire",
                f"ST_XMax({geom('a')}) < ST_XMin({geom('b')})",
            ),
            "answer agtype",
        ),
        Representative(
            146,
            ask_two(
                YAGO + "County_Cavan",
                YAGO + "County_Mayo",
                f"ST_XMin({geom('a')}) > ST_XMax({geom('b')})",
            ),
            "answer agtype",
        ),
        Representative(
            149,
            ask_two(
                YAGO + "County_Mayo",
                YAGO + "County_Sligo",
                f"ST_Touches({geom('a')}, {geom('b')})",
            ),
            "answer agtype",
        ),
        Representative(
            150,
            ask_two(
                YAGO + "Dublin",
                YAGO + "Kilkenny",
                f"ST_YMin({geom('a')}) > ST_YMax({geom('b')})",
            ),
            "answer agtype",
        ),
        Representative(
            153,
            ask_two(
                YAGO + "Oxfordshire",
                YAGO + "Kent",
                f"ST_Distance({geom_m('a')}, {geom_m('b')}) > 100000",
            ),
            "answer agtype",
        ),
        Representative(
            154,
            ask_two(
                YAGO + "Lough_Leane",
                YAGO + "County_Kerry",
                f"ST_Within({geom('a')}, {geom('b')})",
            ),
            "answer agtype",
        ),
        Representative(
            157,
            ask_two(
                YAGO + "County_Galway",
                YAGO + "Claregalway",
                f"ST_Contains({geom('a')}, {geom('b')})",
            ),
            "answer agtype",
        ),
        Representative(
            278,
            "\n".join(
                [
                    "MATCH (region)",
                    f"WHERE {uri('region', YAGO + 'Epirus_(region)')}",
                    "WITH ST_Transform(region.geom, 4326) AS region_geom",
                    "MATCH (lake:OSM_lake)",
                    f"WHERE ST_Within({geom('lake')}, region_geom)",
                    "RETURN DISTINCT lake.uri AS lake",
                ]
            ),
            "lake agtype",
        ),
        Representative(280, "\n".join(county_union), "county agtype"),
        Representative(
            285,
            "\n".join(
                [
                    "MATCH (doolin)",
                    f"WHERE {uri('doolin', YAGO + 'Doolin')}",
                    "WITH doolin",
                    "MATCH (bay:OSM_bay)",
                    f"WHERE ST_Distance({geom_m('doolin')}, {geom_m('bay')}) < 5000",
                    "RETURN DISTINCT bay.uri AS bay",
                ]
            ),
            "bay agtype",
        ),
        Representative(
            302,
            "\n".join(
                [
                    "MATCH (x)",
                    f"WHERE {uri('x', YAGO + 'geoentity_Perifereiakí_Enótita_Kentrikoú_Toméa_Athinón_8200482')}",
                    "WITH x",
                    "MATCH (y:GAG_Municipality)",
                    f"WHERE ST_Contains({geom_4326('x')}, {geom_4326('y')})",
                    "RETURN DISTINCT y.uri AS y",
                ]
            ),
            "y agtype",
        ),
        Representative(
            435,
            "\n".join(
                [
                    "MATCH (x:OSM_stream), (y:OSM_city)",
                    f"WHERE ST_Crosses({geom('x')}, {geom('y')})",
                    "RETURN DISTINCT x.uri AS x",
                ]
            ),
            "x agtype",
        ),
        Representative(
            436,
            "\n".join(
                [
                    "MATCH (m:OSI_Barony), (l:OSM_forest), (townland:OSI_Townland)",
                    f"WHERE ST_Within({geom('l')}, {geom('m')})",
                    f"  AND ST_Intersects({geom('l')}, {geom('townland')})",
                    "RETURN DISTINCT l.uri AS l",
                ]
            ),
            "l agtype",
        ),
        Representative(
            438,
            "\n".join(
                [
                    "MATCH (town:OSM_town), (beach:OSM_beach)",
                    f"WHERE ST_Within({geom('beach')}, {geom('town')})",
                    "RETURN DISTINCT town.uri AS town",
                ]
            ),
            "town agtype",
        ),
        Representative(
            443,
            "\n".join(
                [
                    "MATCH (lake:OSM_lake), (village:OSM_village), (forest:OSM_forest)",
                    f"WHERE ST_Touches({geom('village')}, {geom('lake')})",
                    f"  AND ST_Touches({geom('forest')}, {geom('lake')})",
                    "RETURN count(*) > 0 AS answer",
                ]
            ),
            "answer agtype",
        ),
        Representative(
            455,
            "\n".join(
                [
                    "MATCH (country)",
                    f"WHERE {uri('country', YAGO + 'Republic_of_Ireland')}",
                    "WITH country",
                    "MATCH (vill:OSM_lake), (forest:OSM_forest)",
                    f"WHERE ST_Within({geom('forest')}, {geom('country')})",
                    f"  AND ST_Intersects({geom('vill')}, {geom('forest')})",
                    "RETURN DISTINCT vill.uri AS vill",
                ]
            ),
            "vill agtype",
        ),
        Representative(
            469,
            "\n".join(
                [
                    "MATCH (suffolk:OS_County)",
                    f"WHERE {uri('suffolk', YAGO + 'Suffolk')}",
                    "WITH suffolk",
                    "MATCH (district:OS_District)",
                    f"WHERE ST_Within({geom('district')}, {geom('suffolk')})",
                    "WITH district",
                    "MATCH (forest:OSM_forest)",
                    f"WHERE ST_Within({geom('forest')}, {geom('district')})",
                    "RETURN DISTINCT district.uri AS district",
                ]
            ),
            "district agtype",
        ),
        Representative(
            484,
            "\n".join(
                [
                    "MATCH (england)",
                    f"WHERE {uri('england', YAGO + 'England')}",
                    "WITH england",
                    "MATCH (scotland)",
                    f"WHERE {uri('scotland', YAGO + 'Scotland')}",
                    "WITH england, scotland",
                    "MATCH (d:OS_UnitaryAuthority)",
                    f"WHERE ST_Within({geom('d')}, {geom('england')})",
                    f"  AND ST_Touches({geom('d')}, {geom('scotland')})",
                    "RETURN DISTINCT d.uri AS d",
                ]
            ),
            "d agtype",
        ),
        Representative(
            594,
            "\n".join(
                [
                    "MATCH (region)",
                    f"WHERE {uri('region', YAGO + 'Central_Macedonia')}",
                    "WITH region",
                    "MATCH (municipality:GAG_Municipality)",
                    "WHERE municipality.population > 50000",
                    f"  AND ST_Within({geom_4326('municipality')}, {geom_4326('region')})",
                    "RETURN DISTINCT municipality.uri AS municipality",
                ]
            ),
            "municipality agtype",
        ),
        Representative(
            614,
            "\n".join(
                [
                    "MATCH (england)",
                    f"WHERE {uri('england', YAGO + 'England')}",
                    "WITH england",
                    "MATCH (x:OSM_forest)",
                    f"WHERE ST_Within({geom('x')}, {geom('england')})",
                    "RETURN count(DISTINCT x) AS forests",
                ]
            ),
            "forests agtype",
        ),
        Representative(
            627,
            "\n".join(
                [
                    "MATCH (village:OSM_village), (lake:OSM_lake)",
                    f"WHERE ST_Touches({geom('lake')}, {geom('village')})",
                    "RETURN count(DISTINCT village) AS villages",
                ]
            ),
            "villages agtype",
        ),
        Representative(
            769,
            "\n".join(
                [
                    "MATCH (england)",
                    f"WHERE {uri('england', YAGO + 'England')}",
                    "WITH england",
                    "MATCH (forest:OSM_forest)",
                    f"WHERE ST_Within({geom('forest')}, {geom('england')})",
                    "RETURN DISTINCT forest.uri AS forest, ST_Area(forest.geom) AS totalArea",
                    "ORDER BY totalArea DESC",
                    "LIMIT 1",
                ]
            ),
            "forest agtype, totalArea agtype",
        ),
        Representative(
            873,
            "\n".join(
                [
                    "MATCH (england)",
                    f"WHERE {uri('england', YAGO + 'England')}",
                    "WITH england",
                    "MATCH (district:OS_District)",
                    f"WHERE ST_Within({geom('district')}, {geom('england')})",
                    "WITH district",
                    "MATCH (lake:OSM_lake)",
                    f"WHERE ST_Within({geom('lake')}, {geom('district')})",
                    "WITH district, count(DISTINCT lake) AS lake_count",
                    "RETURN district.uri AS district",
                    "ORDER BY lake_count DESC",
                    "LIMIT 1",
                ]
            ),
            "district agtype",
        ),
    ]


def load_questions(path: Path) -> Dict[int, Dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(qid): row for qid, row in data.items()}


def load_answers(path: Path) -> Dict[int, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(qid): str(answer) for qid, answer in data.items()}


def run_sql(sql: str, qid: int, timeout: int) -> Tuple[str, float]:
    temp_dir = ROOT / "benchmark" / "results" / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"age-spatial-function-q{qid}.sql"
    temp_path.write_text(sql + "\n", encoding="utf-8")
    container_path = f"/benchmark/results/tmp/{temp_path.name}"

    start = time.monotonic()
    try:
        proc = subprocess.run(
            [
                str(PSQL),
                "-X",
                "-q",
                "-A",
                "-t",
                "-F",
                "\t",
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
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s", (time.monotonic() - start) * 1000
    finally:
        temp_path.unlink(missing_ok=True)

    elapsed = (time.monotonic() - start) * 1000
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        return "ERROR: " + message, elapsed
    return proc.stdout.strip(), elapsed


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run representative GeoQuestions spatial-function AGE Cypher queries."
    )
    parser.add_argument(
        "--geoquestions",
        default="benchmark/queries/GeoQuestions1089.json",
    )
    parser.add_argument(
        "--answers",
        default="benchmark/queries/GeoQuestions1089Answers.json",
    )
    parser.add_argument(
        "--output",
        default=(
            "benchmark/results/geoquestions/age_spatial_function_representative/"
            "geoquestions_age_spatial_function_representative_results.csv"
        ),
    )
    parser.add_argument("--timeout", type=int, default=90)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    questions = load_questions(ROOT / args.geoquestions)
    answers = load_answers(ROOT / args.answers)
    reps = representatives()

    output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for index, rep in enumerate(reps, 1):
            question = questions[rep.qid]
            age_query = sql_wrapper(rep.cypher, rep.signature)
            result, elapsed = run_sql(age_query, rep.qid, args.timeout)
            writer.writerow(
                {
                    "category": question.get("Category", ""),
                    "자연어질문": question.get("Question", ""),
                    "SPARQL 쿼리": " ".join(question.get("Query", "").split()),
                    "AGE쿼리": age_query,
                    "원래 정답": answers.get(rep.qid, ""),
                    "age 쿼리 수행결과 정답": result,
                    "실행시간": f"{elapsed:.3f}",
                }
            )
            print(
                f"[{index}/{len(reps)}] q{rep.qid} {question.get('Category', '')} "
                f"{elapsed:.1f}ms",
                flush=True,
            )

    print(f"wrote={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
