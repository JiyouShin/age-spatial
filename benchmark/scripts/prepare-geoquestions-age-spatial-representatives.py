#!/usr/bin/env python3
"""Prepare representative GeoQuestions queries for age-spatial evaluation.

The generated queries intentionally call PostGIS spatial functions inside
ag_catalog.cypher(), e.g. ST_Area(n.geom) and ST_Touches(a.geom, b.geom).
That is the path exercised by the age-spatial parser/physical-column support.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
GRAPH_NAME = "yago2geo"
YAGO = "http://yago-knowledge.org/resource/"


@dataclass(frozen=True)
class Representative:
    qid: int
    pattern: str
    kind: str
    spatial_functions: str
    countries: str
    review_status: str
    cypher: str
    signature: str
    note: str = ""


def cypher_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def uri_filter(var: str, uri: str) -> str:
    value = cypher_string(uri)
    return f"({var}.uri = {value} OR {var}.yago_uri = {value})"


def label_in(var: str, labels: Sequence[str]) -> str:
    quoted = ", ".join(cypher_string(label) for label in labels)
    return f"label({var}) IN [{quoted}]"


def geom_4326(var: str) -> str:
    return f"ST_Transform({var}.geom, 4326)"


def wrapper(cypher: str, signature: str) -> str:
    return (
        "LOAD 'age';\n"
        "SET search_path TO ag_catalog, public;\n"
        f"SELECT * FROM ag_catalog.cypher('{GRAPH_NAME}', $$\n"
        f"{cypher}\n"
        f"$$) AS ({signature});"
    )


def load_questions(path: Path) -> Dict[int, Dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(qid): row for qid, row in data.items()}


def load_answers(path: Path) -> Dict[int, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(qid): str(answer) for qid, answer in data.items()}


def representatives() -> List[Representative]:
    county_labels = ["OSI_County_Council", "OSI_City_and_County_Council"]

    return [
        Representative(
            qid=6,
            pattern="single_entity_area",
            kind="area",
            spatial_functions="strdf:area",
            countries="ireland",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (n)",
                    f"WHERE {uri_filter('n', YAGO + 'County_Galway')}",
                    "RETURN ST_Area(n.geom) AS area",
                ]
            ),
            signature="area agtype",
            note="RDF query has no rdf:type, so the LPG query keeps MATCH (n).",
        ),
        Representative(
            qid=149,
            pattern="ask_topological_touches",
            kind="ask_spatial",
            spatial_functions="geof:sfTouches",
            countries="ireland",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (a)",
                    f"WHERE {uri_filter('a', YAGO + 'County_Mayo')}",
                    "WITH a",
                    "MATCH (b)",
                    f"WHERE {uri_filter('b', YAGO + 'County_Sligo')}",
                    "WITH a, b",
                    f"WHERE ST_Touches({geom_4326('a')}, {geom_4326('b')})",
                    "RETURN count(*) > 0 AS answer",
                ]
            ),
            signature="answer agtype",
        ),
        Representative(
            qid=154,
            pattern="ask_topological_within",
            kind="ask_spatial",
            spatial_functions="geof:sfWithin",
            countries="ireland",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (a)",
                    f"WHERE {uri_filter('a', YAGO + 'Lough_Leane')}",
                    "WITH a",
                    "MATCH (b)",
                    f"WHERE {uri_filter('b', YAGO + 'County_Kerry')}",
                    "WITH a, b",
                    f"WHERE ST_Within({geom_4326('a')}, {geom_4326('b')})",
                    "RETURN count(*) > 0 AS answer",
                ]
            ),
            signature="answer agtype",
        ),
        Representative(
            qid=157,
            pattern="ask_topological_contains",
            kind="ask_spatial",
            spatial_functions="geof:sfContains",
            countries="ireland",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (a)",
                    f"WHERE {uri_filter('a', YAGO + 'County_Galway')}",
                    "WITH a",
                    "MATCH (b)",
                    f"WHERE {uri_filter('b', YAGO + 'Claregalway')}",
                    "WITH a, b",
                    f"WHERE ST_Contains({geom_4326('a')}, {geom_4326('b')})",
                    "RETURN count(*) > 0 AS answer",
                ]
            ),
            signature="answer agtype",
        ),
        Representative(
            qid=153,
            pattern="ask_metric_distance",
            kind="ask_spatial",
            spatial_functions="geof:distance",
            countries="uk",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (a)",
                    f"WHERE {uri_filter('a', YAGO + 'Oxfordshire')}",
                    "WITH a",
                    "MATCH (b)",
                    f"WHERE {uri_filter('b', YAGO + 'Kent')}",
                    "WITH a, b",
                    f"WHERE ST_DistanceSphere(ST_Centroid({geom_4326('a')}), ST_Centroid({geom_4326('b')})) > 100000",
                    "RETURN count(*) > 0 AS answer",
                ]
            ),
            signature="answer agtype",
        ),
        Representative(
            qid=150,
            pattern="ask_direction_above",
            kind="ask_spatial",
            spatial_functions="strdf:above",
            countries="ireland",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (a)",
                    f"WHERE {uri_filter('a', YAGO + 'Dublin')}",
                    "WITH a",
                    "MATCH (b)",
                    f"WHERE {uri_filter('b', YAGO + 'Kilkenny')}",
                    "WITH a, b",
                    f"WHERE ST_YMin({geom_4326('a')}) > ST_YMax({geom_4326('b')})",
                    "RETURN count(*) > 0 AS answer",
                ]
            ),
            signature="answer agtype",
        ),
        Representative(
            qid=280,
            pattern="typed_select_touches",
            kind="type_spatial_select",
            spatial_functions="geof:sfTouches",
            countries="ireland",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (anchor)",
                    f"WHERE {uri_filter('anchor', YAGO + 'County_Mayo')}",
                    "WITH anchor",
                    "MATCH (county)",
                    f"WHERE {label_in('county', county_labels)}",
                    "WITH anchor, county",
                    f"WHERE ST_Touches({geom_4326('anchor')}, {geom_4326('county')})",
                    "RETURN DISTINCT county.uri AS county",
                ]
            ),
            signature="county agtype",
            note="SPARQL UNION over two rdf:type classes is mapped to label(county) IN [...].",
        ),
        Representative(
            qid=302,
            pattern="typed_select_contains",
            kind="type_spatial_select",
            spatial_functions="geof:sfContains",
            countries="greece",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (x)",
                    f"WHERE {uri_filter('x', YAGO + 'geoentity_Perifereiakí_Enótita_Kentrikoú_Toméa_Athinón_8200482')}",
                    "WITH x",
                    "MATCH (y:GAG_Municipality)",
                    f"WHERE ST_Contains({geom_4326('x')}, {geom_4326('y')})",
                    "RETURN DISTINCT y.uri AS y",
                ]
            ),
            signature="y agtype",
        ),
        Representative(
            qid=614,
            pattern="typed_count_within",
            kind="count_spatial",
            spatial_functions="geof:sfWithin",
            countries="uk|osm",
            review_status="ready",
            cypher="\n".join(
                [
                    "MATCH (england)",
                    f"WHERE {uri_filter('england', YAGO + 'England')}",
                    "WITH england",
                    "MATCH (x:OSM_forest)",
                    f"WHERE ST_Within({geom_4326('x')}, {geom_4326('england')})",
                    "RETURN count(DISTINCT x) AS forests",
                ]
            ),
            signature="forests agtype",
        ),
        Representative(
            qid=448,
            pattern="count_union_as_label_set",
            kind="count_spatial",
            spatial_functions="geof:sfWithin",
            countries="greece|osm",
            review_status="draft_review",
            cypher="\n".join(
                [
                    "MATCH (greece)",
                    f"WHERE {uri_filter('greece', YAGO + 'Greece')}",
                    "WITH greece",
                    "MATCH (village:OSM_village)",
                    f"WHERE ST_Within({geom_4326('village')}, {geom_4326('greece')})",
                    "WITH village",
                    "MATCH (container)",
                    "WHERE label(container) IN ['OSM_lake', 'OSM_forest']",
                    "WITH village, container",
                    f"WHERE ST_Within({geom_4326('village')}, {geom_4326('container')})",
                    "RETURN count(DISTINCT village) AS count",
                ]
            ),
            signature="count agtype",
            note="Represents SPARQL UNION by matching OSM_lake or OSM_forest as a label set.",
        ),
        Representative(
            qid=455,
            pattern="two_spatial_predicates",
            kind="type_spatial_select",
            spatial_functions="geof:sfWithin|geof:sfIntersects",
            countries="ireland|osm",
            review_status="draft_review",
            cypher="\n".join(
                [
                    "MATCH (country)",
                    f"WHERE {uri_filter('country', YAGO + 'Republic_of_Ireland')}",
                    "WITH country",
                    "MATCH (lake:OSM_lake), (forest:OSM_forest)",
                    f"WHERE ST_Within({geom_4326('forest')}, {geom_4326('country')})",
                    f"  AND ST_Intersects({geom_4326('lake')}, {geom_4326('forest')})",
                    "RETURN DISTINCT lake.uri AS vill",
                ]
            ),
            signature="vill agtype",
            note="Keeps the SPARQL variable alias ?vill, although it denotes lakes in the source query.",
        ),
        Representative(
            qid=780,
            pattern="order_by_area_after_spatial_filter",
            kind="rank_spatial",
            spatial_functions="geof:sfTouches|strdf:area",
            countries="ireland",
            review_status="draft_review",
            cypher="\n".join(
                [
                    "MATCH (cork)",
                    f"WHERE {uri_filter('cork', YAGO + 'geoentity_Cork_City_7778678')}",
                    "WITH cork",
                    "MATCH (county)",
                    f"WHERE {label_in('county', county_labels)}",
                    "WITH cork, county",
                    f"WHERE ST_Touches({geom_4326('cork')}, {geom_4326('county')})",
                    "RETURN DISTINCT county.uri AS county, ST_Area(county.geom) AS area",
                    "ORDER BY area DESC",
                    "LIMIT 1",
                ]
            ),
            signature="county agtype, area agtype",
        ),
        Representative(
            qid=874,
            pattern="group_count_order_after_spatial_filter",
            kind="rank_spatial",
            spatial_functions="geof:sfWithin|geof:sfWithin",
            countries="ireland|osm",
            review_status="draft_review",
            cypher="\n".join(
                [
                    "MATCH (ireland)",
                    f"WHERE {uri_filter('ireland', YAGO + 'Republic_of_Ireland')}",
                    "WITH ireland",
                    "MATCH (reserve:OSM_nature_reserve), (county)",
                    f"WHERE {label_in('county', county_labels)}",
                    "WITH ireland, reserve, county",
                    f"WHERE ST_Within({geom_4326('county')}, {geom_4326('ireland')})",
                    f"  AND ST_Within({geom_4326('reserve')}, {geom_4326('county')})",
                    "WITH county, count(DISTINCT reserve) AS reserve_count",
                    "RETURN county.uri AS county, reserve_count AS reserve_count",
                    "ORDER BY reserve_count DESC",
                    "LIMIT 1",
                ]
            ),
            signature="county agtype, reserve_count agtype",
        ),
    ]


FIELDS = [
    "id",
    "resolved_id",
    "category",
    "country_scope",
    "countries",
    "status",
    "review_status",
    "kind",
    "pattern",
    "spatial_functions",
    "question",
    "sparql",
    "existing_answer",
    "age_cypher",
    "age_sql_wrapper",
    "return_signature",
    "note",
]


def write_markdown(path: Path, rows: List[Dict[str, str]]) -> None:
    lines = [
        "# GeoQuestions Representative age-spatial Queries",
        "",
        "These examples are intentionally written so spatial operations execute inside",
        "`ag_catalog.cypher(...)`, exercising age-spatial physical geometry access.",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"## q{row['id']} - {row['pattern']}",
                "",
                f"- Category: {row['category']}",
                f"- Review status: {row['review_status']}",
                f"- Question: {row['question']}",
                "",
                "```sparql",
                row["sparql"],
                "```",
                "",
                "```cypher",
                row["age_cypher"],
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write representative GeoQuestions age-spatial Cypher examples."
    )
    parser.add_argument(
        "--geoquestions",
        default="benchmark/queries/GeoQuestions1089.json",
        help="GeoQuestions1089 JSON file",
    )
    parser.add_argument(
        "--answers",
        default="benchmark/queries/GeoQuestions1089Answers.json",
        help="GeoQuestions1089 answers JSON file",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark/results/geoquestions/age_spatial_representative",
        help="output directory",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    questions = load_questions(ROOT / args.geoquestions)
    answers = load_answers(ROOT / args.answers)
    output_dir = ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, str]] = []
    for rep in representatives():
        question = questions[rep.qid]
        rows.append(
            {
                "id": str(rep.qid),
                "resolved_id": str(rep.qid),
                "category": question.get("Category", ""),
                "country_scope": rep.countries.split("|")[0] if "|" not in rep.countries else "multi",
                "countries": rep.countries,
                "status": "converted" if rep.review_status == "ready" else "draft_review",
                "review_status": rep.review_status,
                "kind": rep.kind,
                "pattern": rep.pattern,
                "spatial_functions": rep.spatial_functions,
                "question": question.get("Question", ""),
                "sparql": " ".join(question.get("Query", "").split()),
                "existing_answer": answers.get(rep.qid, ""),
                "age_cypher": rep.cypher,
                "age_sql_wrapper": wrapper(rep.cypher, rep.signature),
                "return_signature": rep.signature,
                "note": rep.note,
            }
        )

    csv_path = output_dir / "geoquestions_age_spatial_representative_queries.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    md_path = output_dir / "geoquestions_age_spatial_representative_queries.md"
    write_markdown(md_path, rows)

    ready = sum(1 for row in rows if row["review_status"] == "ready")
    draft = sum(1 for row in rows if row["review_status"] != "ready")
    print(f"wrote={csv_path}")
    print(f"wrote={md_path}")
    print(f"representatives={len(rows)} ready={ready} draft_review={draft}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
