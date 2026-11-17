#!/usr/bin/env python3
"""Find invalid YAGO2Geo geometries and GeoQuestions that may use them.

The question pool intentionally matches the spatial-function-only subset used
for age-spatial evaluation:

* SPARQL contains query-time spatial functions (`geof:*()` or `strdf:*()`).
* Precomputed RDF topological relation predicates (`geo:sfWithin` as a triple)
  are excluded.
* Questions that require non-YAGO2Geo/base-YAGO predicates are excluded.

Question-to-invalid matching is conservative:

* direct_entity: an invalid geometry URI appears directly in the SPARQL query.
* typed_candidate_label: the query ranges over a label that contains invalid
  geometries, for example `?forest rdf:type y2geoo:OSM_forest`.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
PSQL = ROOT / "benchmark" / "scripts" / "psql.sh"
YAGO_BASE = "http://yago-knowledge.org/resource/"
Y2GEOR_BASE = "http://kr.di.uoa.gr/yago2geo/resource/"

SPATIAL_FUNC_RE = re.compile(r"\b(?:geof|strdf):[A-Za-z_][\w-]*\s*\(", re.IGNORECASE)
SPATIAL_FUNC_NAME_RE = re.compile(
    r"\b(?P<prefix>geof|strdf):(?P<name>[A-Za-z_][\w-]*)\s*\(",
    re.IGNORECASE,
)
GEO_RELATION_PREDICATE_RE = re.compile(
    r"\bgeo:sf[A-Za-z_][\w-]*\b(?!\s*\()",
    re.IGNORECASE,
)
BASE_YAGO_PREDICATE_RE = re.compile(
    r"(?:^|[\s.;{}])(?:\?\w+|<[^>]+>|(?:yago|y2geor):[^\s{};,]+)\s+"
    r"(?:yago|rdfs|dbo|dbp|foaf|owl):[A-Za-z_][\w-]*",
    re.IGNORECASE,
)
TYPE_RE = re.compile(r"rdf:type\s+y2geoo:(?P<label>[A-Za-z0-9_]+)", re.IGNORECASE)
ENTITY_RE = re.compile(
    r"<(?P<iri>https?://[^>\s]+)>|"
    r"\byago:(?P<yago>[^\s\{\};,\)\]]+)|"
    r"\by2geor:(?P<y2geor>[^\s\{\};,\)\]]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Question:
    qid: int
    category: str
    question: str
    sparql: str
    spatial_functions: str
    referenced_types: str
    referenced_entities: str


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def normalize_query(query: str) -> str:
    return " ".join(query.split())


def spatial_functions(query: str) -> List[str]:
    return sorted(
        {
            f"{match.group('prefix').lower()}:{match.group('name')}"
            for match in SPATIAL_FUNC_NAME_RE.finditer(query)
        }
    )


def referenced_types(query: str) -> List[str]:
    return sorted(set(TYPE_RE.findall(query)))


def expand_entity(match: re.Match[str]) -> str:
    if match.group("iri"):
        return match.group("iri")
    if match.group("yago"):
        return YAGO_BASE + match.group("yago")
    return Y2GEOR_BASE + match.group("y2geor")


def referenced_entities(query: str) -> List[str]:
    return sorted({expand_entity(match) for match in ENTITY_RE.finditer(query)})


def is_clean_spatial_function_question(query: str) -> bool:
    if not SPATIAL_FUNC_RE.search(query):
        return False
    if GEO_RELATION_PREDICATE_RE.search(query):
        return False
    if BASE_YAGO_PREDICATE_RE.search(query):
        return False
    return True


def load_questions(path: Path) -> List[Question]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: List[Question] = []
    for qid_text, row in data.items():
        query = normalize_query(row["Query"])
        if not is_clean_spatial_function_question(query):
            continue
        rows.append(
            Question(
                qid=int(qid_text),
                category=row.get("Category", ""),
                question=row.get("Question", ""),
                sparql=query,
                spatial_functions="|".join(spatial_functions(query)),
                referenced_types="|".join(referenced_types(query)),
                referenced_entities="|".join(referenced_entities(query)),
            )
        )
    return sorted(rows, key=lambda question: question.qid)


def run_psql(sql: str) -> List[Dict[str, str]]:
    proc = subprocess.run(
        [
            str(PSQL),
            "-X",
            "-q",
            "-A",
            "-F",
            "\t",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("(")]
    if not lines:
        return []
    return list(csv.DictReader(lines, delimiter="\t"))


def geometry_labels() -> List[str]:
    rows = run_psql(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema = 'yago2geo'
          AND column_name = 'geom'
        ORDER BY table_name;
        """
    )
    return [row["table_name"] for row in rows]


def invalid_geometry_sql(labels: Sequence[str]) -> str:
    selects: List[str] = []
    for label in labels:
        table = f"yago2geo.{quote_ident(label)}"
        selects.append(
            f"""
            SELECT
                {sql_literal(label)} AS label,
                COALESCE(substring(properties::text from '"uri": "([^"]+)"'), '') AS uri,
                COALESCE(substring(properties::text from '"yago_uri": "([^"]+)"'), '') AS yago_uri,
                COALESCE(substring(properties::text from '"name": "([^"]+)"'), '') AS name,
                COALESCE(substring(properties::text from '"rdf_type": "([^"]+)"'), '') AS rdf_type,
                GeometryType(geom) AS geometry_type,
                ST_NumGeometries(geom)::text AS num_geometries,
                ST_NPoints(geom)::text AS npoints,
                ST_Area(geom)::text AS area,
                ST_IsValidReason(geom) AS invalid_reason,
                COALESCE(ST_AsText((ST_IsValidDetail(geom)).location), '') AS invalid_location_wkt,
                ST_AsText(ST_Envelope(geom)) AS bbox_wkt
            FROM {table}
            WHERE geom IS NOT NULL
              AND NOT ST_IsValid(geom)
            """
        )
    return (
        "SET client_min_messages TO warning;\n"
        + "\nUNION ALL\n".join(selects)
        + "\nORDER BY label, uri, yago_uri;"
    )


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def load_answers(path: Path) -> Dict[int, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(qid): str(answer) for qid, answer in data.items()}


def question_rows(questions: Iterable[Question], answers: Dict[int, str]) -> List[Dict[str, str]]:
    return [
        {
            "id": str(question.qid),
            "category": question.category,
            "question": question.question,
            "sparql": question.sparql,
            "spatial_functions": question.spatial_functions,
            "referenced_types": question.referenced_types,
            "referenced_entities": question.referenced_entities,
            "answer": answers.get(question.qid, ""),
        }
        for question in questions
    ]


def affected_question_rows(
    questions: Iterable[Question],
    answers: Dict[int, str],
    usage_rows: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    usage_by_qid: Dict[int, List[Dict[str, str]]] = defaultdict(list)
    for row in usage_rows:
        usage_by_qid[int(row["question_id"])].append(row)

    rows: List[Dict[str, str]] = []
    for question in questions:
        rows_for_question = usage_by_qid.get(question.qid, [])
        if not rows_for_question:
            continue
        rows.append(
            {
                "id": str(question.qid),
                "category": question.category,
                "question": question.question,
                "sparql": question.sparql,
                "spatial_functions": question.spatial_functions,
                "referenced_types": question.referenced_types,
                "referenced_entities": question.referenced_entities,
                "invalid_match_types": "|".join(
                    sorted({row["match_type"] for row in rows_for_question})
                ),
                "invalid_labels": "|".join(
                    sorted({row["invalid_label"] for row in rows_for_question})
                ),
                "invalid_geometry_count": str(
                    len(
                        {
                            (
                                row["invalid_label"],
                                row["invalid_uri"],
                                row["invalid_yago_uri"],
                            )
                            for row in rows_for_question
                        }
                    )
                ),
                "answer": answers.get(question.qid, ""),
            }
        )
    return rows


def write_csv(path: Path, rows: List[Dict[str, str]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_usage_rows(
    invalid_rows: List[Dict[str, str]],
    questions: List[Question],
) -> tuple[List[Dict[str, str]], Set[int]]:
    invalid_by_uri: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    invalid_by_label: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in invalid_rows:
        if row["uri"]:
            invalid_by_uri[row["uri"]].append(row)
        if row["yago_uri"]:
            invalid_by_uri[row["yago_uri"]].append(row)
        invalid_by_label[row["label"]].append(row)

    usage_rows: List[Dict[str, str]] = []
    affected_question_ids: Set[int] = set()
    seen: Set[tuple[str, str, int, str]] = set()

    def add_usage(
        invalid: Dict[str, str],
        question: Question,
        match_type: str,
        matched_query_term: str,
    ) -> None:
        key = (invalid["label"], invalid["uri"] or invalid["yago_uri"], question.qid, match_type)
        if key in seen:
            return
        seen.add(key)
        affected_question_ids.add(question.qid)
        usage_rows.append(
            {
                "invalid_label": invalid["label"],
                "invalid_uri": invalid["uri"],
                "invalid_yago_uri": invalid["yago_uri"],
                "invalid_name": invalid["name"],
                "invalid_rdf_type": invalid["rdf_type"],
                "geometry_type": invalid["geometry_type"],
                "num_geometries": invalid["num_geometries"],
                "npoints": invalid["npoints"],
                "area": invalid["area"],
                "invalid_reason": invalid["invalid_reason"],
                "invalid_location_wkt": invalid["invalid_location_wkt"],
                "bbox_wkt": invalid["bbox_wkt"],
                "match_type": match_type,
                "matched_query_term": matched_query_term,
                "question_id": str(question.qid),
                "category": question.category,
                "question": question.question,
                "sparql": question.sparql,
                "spatial_functions": question.spatial_functions,
                "referenced_types": question.referenced_types,
                "referenced_entities": question.referenced_entities,
            }
        )

    for question in questions:
        entities = set(question.referenced_entities.split("|")) if question.referenced_entities else set()
        types = set(question.referenced_types.split("|")) if question.referenced_types else set()
        for entity in entities:
            for invalid in invalid_by_uri.get(entity, []):
                add_usage(invalid, question, "direct_entity", entity)
        for label in types:
            for invalid in invalid_by_label.get(label, []):
                add_usage(invalid, question, "typed_candidate_label", label)

    usage_rows.sort(
        key=lambda row: (
            int(row["question_id"]),
            row["match_type"],
            row["invalid_label"],
            row["invalid_uri"],
        )
    )
    return usage_rows, affected_question_ids


def summary_rows(
    invalid_rows: List[Dict[str, str]],
    questions: List[Question],
    usage_rows: List[Dict[str, str]],
    affected_question_ids: Set[int],
) -> List[Dict[str, str]]:
    invalid_counts = Counter(row["label"] for row in invalid_rows)
    affected_label_counts = Counter(row["invalid_label"] for row in usage_rows)
    question_label_counts = Counter()
    for row in usage_rows:
        question_label_counts[(row["invalid_label"], row["question_id"])] += 1

    rows = [
        {
            "metric": "clean_spatial_function_questions",
            "value": str(len(questions)),
            "detail": "",
        },
        {
            "metric": "invalid_geometry_entities",
            "value": str(len(invalid_rows)),
            "detail": "",
        },
        {
            "metric": "invalid_geometry_usage_rows",
            "value": str(len(usage_rows)),
            "detail": "one row per invalid geometry entity/question/match type",
        },
        {
            "metric": "questions_with_invalid_geometry_risk",
            "value": str(len(affected_question_ids)),
            "detail": "",
        },
        {
            "metric": "questions_remaining_after_exclusion",
            "value": str(len(questions) - len(affected_question_ids)),
            "detail": "",
        },
    ]
    for label, count in invalid_counts.most_common():
        rows.append({"metric": "invalid_entities_by_label", "value": str(count), "detail": label})
    for label, count in affected_label_counts.most_common():
        rows.append({"metric": "usage_rows_by_label", "value": str(count), "detail": label})
    for label in sorted({label for label, _ in question_label_counts}):
        count = len({qid for row_label, qid in question_label_counts if row_label == label})
        rows.append({"metric": "affected_questions_by_label", "value": str(count), "detail": label})
    return rows


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze invalid geometry usage in clean GeoQuestions spatial-function queries."
    )
    parser.add_argument("--questions", default="benchmark/queries/GeoQuestions1089.json")
    parser.add_argument("--answers", default="benchmark/queries/GeoQuestions1089Answers.json")
    parser.add_argument(
        "--output-dir",
        default="benchmark/results/geoquestions/invalid_geometry",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    output_dir = ROOT / args.output_dir

    questions = load_questions(ROOT / args.questions)
    answers = load_answers(ROOT / args.answers)
    labels = geometry_labels()
    invalid_rows = run_psql(invalid_geometry_sql(labels))
    usage_rows, affected_question_ids = build_usage_rows(invalid_rows, questions)
    remaining_questions = [
        question for question in questions if question.qid not in affected_question_ids
    ]

    invalid_fields = [
        "label",
        "uri",
        "yago_uri",
        "name",
        "rdf_type",
        "geometry_type",
        "num_geometries",
        "npoints",
        "area",
        "invalid_reason",
        "invalid_location_wkt",
        "bbox_wkt",
    ]
    usage_fields = [
        "invalid_label",
        "invalid_uri",
        "invalid_yago_uri",
        "invalid_name",
        "invalid_rdf_type",
        "geometry_type",
        "num_geometries",
        "npoints",
        "area",
        "invalid_reason",
        "invalid_location_wkt",
        "bbox_wkt",
        "match_type",
        "matched_query_term",
        "question_id",
        "category",
        "question",
        "sparql",
        "spatial_functions",
        "referenced_types",
        "referenced_entities",
    ]
    question_fields = [
        "id",
        "category",
        "question",
        "sparql",
        "spatial_functions",
        "referenced_types",
        "referenced_entities",
        "answer",
    ]
    summary_fields = ["metric", "value", "detail"]

    write_csv(output_dir / "invalid_geometries.csv", invalid_rows, invalid_fields)
    write_csv(
        output_dir / "invalid_geometry_question_usage.csv",
        usage_rows,
        usage_fields,
    )
    write_csv(
        output_dir / "geoquestions_clean_spatial_function_764.csv",
        question_rows(questions, answers),
        question_fields,
    )
    affected_fields = question_fields[:-1] + [
        "invalid_match_types",
        "invalid_labels",
        "invalid_geometry_count",
        "answer",
    ]
    write_csv(
        output_dir / "geoquestions_with_invalid_geometry_risk.csv",
        affected_question_rows(questions, answers, usage_rows),
        affected_fields,
    )
    write_csv(
        output_dir / "geoquestions_without_invalid_geometry.csv",
        question_rows(remaining_questions, answers),
        question_fields,
    )
    write_csv(
        output_dir / "summary.csv",
        summary_rows(invalid_rows, questions, usage_rows, affected_question_ids),
        summary_fields,
    )

    print(f"clean_questions={len(questions)}")
    print(f"invalid_geometries={len(invalid_rows)}")
    print(f"usage_rows={len(usage_rows)}")
    print(f"affected_questions={len(affected_question_ids)}")
    print(f"remaining_questions={len(remaining_questions)}")
    print(f"output_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
