#!/usr/bin/env python3
"""Generate AGE Cypher queries for spatial-operation GeoQuestions.

This script targets the age-spatial feature where registered physical geometry
columns can be referenced inside Cypher as `n.geom` and passed to PostGIS
functions such as `ST_Touches(n.geom, m.geom)`.

It intentionally selects questions that require query-time spatial operations
(`geof:*` or `strdf:*`). Precomputed topological relation predicates such as
`geo:sfWithin` used as ordinary RDF triples are not the target here.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


GRAPH_NAME = "yago2geo"
YAGO_BASE = "http://yago-knowledge.org/resource/"
Y2GEOR_BASE = "http://kr.di.uoa.gr/yago2geo/resource/"
ENTITY_TOKEN_RE = r"(?:<https?://[^>\s]+>|(?:yago|y2geor):[^\s\{\};,]+)"
GEOM_BINDING_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN_RE}|\?\w+)\s+geo:hasGeometry\s+"
    r"(?P<geom>\?\w+)\s*\.\s*(?P=geom)\s+geo:asWKT\s+(?P<wkt>\?\w+)",
    re.IGNORECASE,
)
TYPE_RE = re.compile(r"(?P<var>\?\w+)\s+rdf:type\s+y2geoo:(?P<label>[A-Za-z0-9_]+)", re.IGNORECASE)
TYPE_ANY_RE = re.compile(r"rdf:type\s+y2geoo:(?P<label>[A-Za-z0-9_]+)", re.IGNORECASE)
SPATIAL_FUNC_RE = re.compile(
    r"\b(?P<prefix>geof|strdf):(?P<name>[A-Za-z_][\w-]*)\s*"
    r"\(\s*(?P<a>\?\w+)\s*,\s*(?P<b>\?\w+)"
    r"(?:\s*,\s*uom:metre)?\s*\)",
    re.IGNORECASE,
)
DISTANCE_FILTER_RE = re.compile(
    r"geof:distance\s*\(\s*(?P<a>\?\w+)\s*,\s*(?P<b>\?\w+)\s*,\s*uom:metre\s*\)"
    r"\s*(?P<op><=|>=|<|>|=)\s*(?P<value>[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
AREA_RE = re.compile(
    r"strdf:area\s*\(\s*(?P<wkt>\?\w+)\s*\)(?:\s+AS\s+(?P<alias>\?\w+))?",
    re.IGNORECASE,
)
SELECT_VAR_RE = re.compile(r"SELECT\s+(?:DISTINCT\s+)?(?P<var>\?\w+)", re.IGNORECASE)
COUNT_RE = re.compile(r"SELECT\s*\(\s*COUNT\s*\(\s*(?:DISTINCT\s+)?(?P<var>\?\w+)", re.IGNORECASE)


SPATIAL_FUNC_TO_CYPHER = {
    ("geof", "sfwithin"): "ST_Within",
    ("strdf", "within"): "ST_Within",
    ("geof", "sfcontains"): "ST_Contains",
    ("strdf", "contains"): "ST_Contains",
    ("geof", "sftouches"): "ST_Touches",
    ("strdf", "touches"): "ST_Touches",
    ("geof", "sfintersects"): "ST_Intersects",
    ("strdf", "intersects"): "ST_Intersects",
    ("geof", "sfcrosses"): "ST_Crosses",
    ("strdf", "crosses"): "ST_Crosses",
    ("geof", "sfoverlaps"): "ST_Overlaps",
    ("strdf", "overlaps"): "ST_Overlaps",
}

LABEL_COUNTRY_PREFIXES = [
    ("GAG", "greece"),
    ("Gag", "greece"),
    ("OSI", "ireland"),
    ("Osi", "ireland"),
    ("OSNI", "uk"),
    ("OS_", "uk"),
]
USA_LABELS = {
    "County",
    "ExternalYAGO",
    "IncorporatedPlace",
    "MinorCivilDivision",
    "State",
    "UnincorporatedPlace",
}
KEYWORDS = {
    "greece": [
        "greece",
        "greek",
        "athens",
        "thessaloniki",
        "crete",
        "corfu",
        "naxos",
        "paros",
        "patmos",
        "lesbos",
        "thessaly",
        "epirus",
        "macedonia",
        "sfakia",
        "eretria",
        "kallithea",
        "chania",
        "acharnes",
        "ioannina",
        "aegean",
        "paxi",
        "alonnisos",
    ],
    "ireland": [
        "ireland",
        "irish",
        "dublin",
        "cork",
        "galway",
        "limerick",
        "waterford",
        "kerry",
        "donegal",
        "mayo",
        "clare",
        "meath",
        "wexford",
        "sligo",
        "longford",
        "leitrim",
        "offaly",
        "tipperary",
        "carlow",
        "lough",
    ],
    "uk": [
        "united kingdom",
        " uk ",
        "england",
        "scotland",
        "wales",
        "northern ireland",
        "london",
        "birmingham",
        "manchester",
        "liverpool",
        "cardiff",
        "oxford",
        "oxfordshire",
        "kent",
        "dorset",
        "cambridge",
        "cambridgeshire",
        "hampshire",
        "essex",
        "norfolk",
        "sussex",
        "plymouth",
        "edinburgh",
        "leicester",
        "newport",
        "southampton",
        "westminster",
        "greenwich",
        "loch",
        "whitehall",
    ],
    "usa": [
        "united states",
        " usa ",
        "texas",
        "new york",
        "california",
        "alaska",
        "florida",
        "arkansas",
        "iowa",
        "kentucky",
        "alabama",
        "louisiana",
        "oregon",
        "missouri",
        "illinois",
        "oklahoma",
        "lake michigan",
        "houston",
        "miami",
    ],
}


@dataclass
class Question:
    qid: int
    question: str
    query: str
    answer: str
    category: str
    resolved_qid: int
    resolved_query: str


@dataclass
class EntityMatch:
    uri: str
    label: str
    matched_column: str
    country: str


@dataclass
class CypherConversion:
    status: str
    kind: str
    cypher: str = ""
    sql_wrapper: str = ""
    return_signature: str = ""
    note: str = ""


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cypher_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip())


def expand_entity(token: str) -> str:
    token = token.strip()
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    if token.startswith("yago:"):
        return YAGO_BASE + token.split(":", 1)[1]
    if token.startswith("y2geor:"):
        return Y2GEOR_BASE + token.split(":", 1)[1]
    raise ValueError(f"unsupported entity token: {token}")


def decode_csv_value(value: str) -> str:
    if value == "":
        return ""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def label_country(label: str) -> str:
    if label in USA_LABELS:
        return "usa"
    if label.startswith("GADM"):
        return "gadm"
    if label.startswith("OSM"):
        return "osm"
    for prefix, country in LABEL_COUNTRY_PREFIXES:
        if label.startswith(prefix):
            return country
    if label == "ExternalYAGO":
        return "uk"
    return "unknown"


def read_answers_json(path: Optional[Path]) -> Dict[int, str]:
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(qid): str(answer) for qid, answer in data.items()}


def read_questions(path: Path, answers_path: Optional[Path] = None) -> List[Question]:
    answers = read_answers_json(answers_path)
    raw: List[Dict[str, str]] = []
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        for qid in sorted(data, key=lambda value: int(value)):
            row = data[qid]
            raw.append(
                {
                    "Question": row.get("Question", ""),
                    "Query": row.get("Query", ""),
                    "Answer": row.get("Answer", answers.get(int(qid), "")),
                    "Category": row.get("Category", ""),
                }
            )
    else:
        with path.open(newline="", encoding="utf-8") as handle:
            raw.extend(csv.DictReader(handle))

    def resolve_query(index: int, seen: Optional[Set[int]] = None) -> Tuple[int, str]:
        if seen is None:
            seen = set()
        query = raw[index - 1]["Query"].strip()
        match = re.fullmatch(r"same-as:(\d+)", query, re.IGNORECASE)
        if match and index not in seen:
            seen.add(index)
            return resolve_query(int(match.group(1)), seen)
        return index, query

    def resolve_category(index: int, seen: Optional[Set[int]] = None) -> str:
        if seen is None:
            seen = set()
        category = raw[index - 1].get("Category", "").strip()
        match = re.fullmatch(r"same-as:(\d+)", category, re.IGNORECASE)
        if match and index not in seen:
            seen.add(index)
            return resolve_category(int(match.group(1)), seen)
        return category

    questions: List[Question] = []
    for idx, row in enumerate(raw, 1):
        resolved_qid, resolved_query = resolve_query(idx)
        questions.append(
            Question(
                qid=idx,
                question=row["Question"],
                query=row["Query"],
                answer=row.get("Answer", ""),
                category=resolve_category(idx),
                resolved_qid=resolved_qid,
                resolved_query=resolved_query,
            )
        )
    return questions


def spatial_functions(query: str) -> List[str]:
    return re.findall(r"\b(?:geof|strdf):[A-Za-z_][\w-]*", query)


def referenced_entities(query: str) -> List[str]:
    entities: List[str] = []
    for token in re.findall(ENTITY_TOKEN_RE, query):
        try:
            entities.append(expand_entity(token))
        except ValueError:
            pass
    return entities


def geometry_bindings(query: str) -> Dict[str, str]:
    bindings: Dict[str, str] = {}
    for match in GEOM_BINDING_RE.finditer(query):
        bindings[match.group("wkt")] = match.group("subject")
    return bindings


def type_bindings(query: str) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = defaultdict(list)
    for match in TYPE_RE.finditer(query):
        result[match.group("var")].append(match.group("label"))
    return dict(result)


def load_manifest(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_uri_lookup(vertices_dirs: Sequence[Path], needed_uris: Set[str]) -> Dict[str, EntityMatch]:
    lookup: Dict[str, EntityMatch] = {}
    for vertices_dir in vertices_dirs:
        for path in sorted(vertices_dir.glob("*.csv")):
            label = path.stem
            country = label_country(label)
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    continue
                has_yago = "yago_uri" in reader.fieldnames
                for row in reader:
                    uri = decode_csv_value(row.get("uri", ""))
                    if uri in needed_uris and uri not in lookup:
                        lookup[uri] = EntityMatch(uri, label, "uri", country)
                    if has_yago:
                        yago_uri = decode_csv_value(row.get("yago_uri", ""))
                        if yago_uri in needed_uris and yago_uri not in lookup:
                            lookup[yago_uri] = EntityMatch(yago_uri, label, "yago_uri", country)
                    if len(lookup) == len(needed_uris):
                        return lookup
    return lookup


def infer_countries(
    question: Question,
    query: str,
    entities: Iterable[str],
    uri_lookup: Dict[str, EntityMatch],
    loaded_labels: Set[str],
) -> List[str]:
    countries: Set[str] = set()
    for uri in entities:
        match = uri_lookup.get(uri)
        if match and match.country != "unknown":
            countries.add(match.country)

    for label in TYPE_ANY_RE.findall(query):
        if label in loaded_labels:
            country = label_country(label)
            if country != "unknown":
                countries.add(country)

    text = f" {question.question} {query} ".lower().replace("_", " ")
    for country, keywords in KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                countries.add(country)
                break

    return sorted(countries) if countries else ["unknown"]


def geom_expr(var: str) -> str:
    return f"ST_Transform({var}.geom, 4326)"


def spatial_condition(function_name: str, left_var: str, right_var: str) -> Optional[str]:
    key = function_name.lower()
    if key in {"geof:sfwithin", "strdf:within"}:
        return f"ST_Within({geom_expr(left_var)}, {geom_expr(right_var)})"
    if key in {"geof:sfcontains", "strdf:contains"}:
        return f"ST_Contains({geom_expr(left_var)}, {geom_expr(right_var)})"
    if key in {"geof:sftouches", "strdf:touches"}:
        return f"ST_Touches({geom_expr(left_var)}, {geom_expr(right_var)})"
    if key in {"geof:sfintersects", "strdf:intersects"}:
        return f"ST_Intersects({geom_expr(left_var)}, {geom_expr(right_var)})"
    if key in {"geof:sfcrosses", "strdf:crosses"}:
        return f"ST_Crosses({geom_expr(left_var)}, {geom_expr(right_var)})"
    if key in {"geof:sfoverlaps", "strdf:overlaps"}:
        return f"ST_Overlaps({geom_expr(left_var)}, {geom_expr(right_var)})"
    if key == "strdf:left":
        return f"ST_XMax({geom_expr(left_var)}) < ST_XMin({geom_expr(right_var)})"
    if key == "strdf:right":
        return f"ST_XMin({geom_expr(left_var)}) > ST_XMax({geom_expr(right_var)})"
    if key == "strdf:above":
        return f"ST_YMin({geom_expr(left_var)}) > ST_YMax({geom_expr(right_var)})"
    if key == "strdf:below":
        return f"ST_YMax({geom_expr(left_var)}) < ST_YMin({geom_expr(right_var)})"
    return None


def distance_condition(left_var: str, right_var: str, op: str, value: str) -> str:
    return (
        f"ST_DistanceSphere(ST_Centroid({geom_expr(left_var)}), "
        f"ST_Centroid({geom_expr(right_var)})) {op} {value}"
    )


def uri_predicate(var: str, uri: str) -> str:
    q = cypher_string(uri)
    return f"({var}.uri = {q} OR {var}.yago_uri = {q})"


def cypher_wrapper(cypher: str, signature: str) -> str:
    return (
        f"SELECT * FROM ag_catalog.cypher({sql_literal(GRAPH_NAME)}, $$\n"
        f"{cypher}\n"
        f"$$) AS ({signature});"
    )


def variable_for_subject(
    subject: str,
    entity_subjects: Dict[str, str],
    variable_subjects: Dict[str, str],
) -> Optional[str]:
    if subject in entity_subjects:
        return entity_subjects[subject]
    if subject in variable_subjects:
        return variable_subjects[subject]
    return None


def convert_area(
    question: Question,
    query: str,
    bindings: Dict[str, str],
    uri_lookup: Dict[str, EntityMatch],
) -> Optional[CypherConversion]:
    if "strdf:area" not in query:
        return None
    area_match = AREA_RE.search(query)
    if not area_match:
        return None
    subject = bindings.get(area_match.group("wkt"))
    if subject is None or subject.startswith("?"):
        return None
    uri = expand_entity(subject)
    entity = uri_lookup.get(uri)
    if entity is None:
        return CypherConversion(
            "missing_entity",
            "area",
            note=f"entity URI is not present in the loaded LPG graph: {uri}",
        )
    alias = "area"
    if area_match.group("alias"):
        alias = area_match.group("alias").lstrip("?")
    else:
        select_match = SELECT_VAR_RE.search(query)
        if select_match:
            alias = select_match.group("var").lstrip("?")
    cypher = "\n".join(
        [
            f"MATCH (e:{entity.label})",
            f"WHERE {uri_predicate('e', uri)}",
            f"RETURN ST_Area(e.geom) AS {alias}",
        ]
    )
    return CypherConversion(
        "converted",
        "area",
        cypher,
        cypher_wrapper(cypher, f"{alias} agtype"),
        f"{alias} agtype",
    )


def convert_binary_ask(
    question: Question,
    query: str,
    bindings: Dict[str, str],
    uri_lookup: Dict[str, EntityMatch],
) -> Optional[CypherConversion]:
    if not query.upper().startswith("ASK "):
        return None

    entity_subjects: Dict[str, str] = {}
    entity_matches: Dict[str, EntityMatch] = {}
    for subject in set(bindings.values()):
        if subject.startswith("?"):
            continue
        uri = expand_entity(subject)
        entity = uri_lookup.get(uri)
        if entity is None:
            return CypherConversion(
                "missing_entity",
                "ask_spatial",
                note=f"entity URI is not present in the loaded LPG graph: {uri}",
            )
        var = chr(ord("a") + len(entity_subjects))
        entity_subjects[subject] = var
        entity_matches[var] = entity

    if len(entity_subjects) < 2:
        return None

    conditions: List[str] = []
    for match in DISTANCE_FILTER_RE.finditer(query):
        left_subject = bindings.get(match.group("a"))
        right_subject = bindings.get(match.group("b"))
        left_var = variable_for_subject(left_subject or "", entity_subjects, {})
        right_var = variable_for_subject(right_subject or "", entity_subjects, {})
        if left_var and right_var:
            conditions.append(
                distance_condition(left_var, right_var, match.group("op"), match.group("value"))
            )

    for match in SPATIAL_FUNC_RE.finditer(query):
        function = f"{match.group('prefix')}:{match.group('name')}"
        if function.lower() == "geof:distance":
            continue
        left_subject = bindings.get(match.group("a"))
        right_subject = bindings.get(match.group("b"))
        left_var = variable_for_subject(left_subject or "", entity_subjects, {})
        right_var = variable_for_subject(right_subject or "", entity_subjects, {})
        if left_var and right_var:
            condition = spatial_condition(function, left_var, right_var)
            if condition:
                conditions.append(condition)

    if not conditions:
        return None

    match_parts = [
        f"({var}:{entity_matches[var].label})" for var in sorted(entity_matches)
    ]
    where_parts = [
        uri_predicate(var, entity_matches[var].uri) for var in sorted(entity_matches)
    ] + conditions
    cypher = "\n".join(
        [
            "MATCH " + ", ".join(match_parts),
            "WHERE " + "\n  AND ".join(where_parts),
            "RETURN true AS answer",
        ]
    )
    return CypherConversion(
        "converted",
        "ask_spatial",
        cypher,
        cypher_wrapper(cypher, "answer agtype"),
        "answer agtype",
    )


def candidate_labels_for_var(var: str, types: Dict[str, List[str]], loaded_labels: Set[str]) -> List[str]:
    return sorted(label for label in types.get(var, []) if label in loaded_labels)


def convert_type_spatial_select(
    question: Question,
    query: str,
    bindings: Dict[str, str],
    types: Dict[str, List[str]],
    uri_lookup: Dict[str, EntityMatch],
    loaded_labels: Set[str],
) -> Optional[CypherConversion]:
    if "rdf:type" not in query or "geo:hasGeometry" not in query:
        return None

    entity_subjects: Dict[str, str] = {}
    entity_matches: Dict[str, EntityMatch] = {}
    variable_subjects: Dict[str, str] = {}

    for subject in set(bindings.values()):
        if subject.startswith("?"):
            labels = candidate_labels_for_var(subject, types, loaded_labels)
            if labels and subject not in variable_subjects:
                variable_subjects[subject] = "candidate"
            continue
        uri = expand_entity(subject)
        entity = uri_lookup.get(uri)
        if entity is None:
            return CypherConversion(
                "missing_entity",
                "type_spatial_select",
                note=f"entity URI is not present in the loaded LPG graph: {uri}",
            )
        if subject not in entity_subjects:
            var = "anchor" if not entity_subjects else f"anchor{len(entity_subjects) + 1}"
            entity_subjects[subject] = var
            entity_matches[var] = entity

    if not entity_matches or not variable_subjects:
        return None

    candidate_subject = next(iter(variable_subjects))
    candidate_labels = candidate_labels_for_var(candidate_subject, types, loaded_labels)
    if not candidate_labels:
        missing = sorted(label for labels in types.values() for label in labels if label not in loaded_labels)
        return CypherConversion(
            "missing_label",
            "type_spatial_select",
            note="missing candidate label(s): " + "|".join(missing),
        )

    conditions: List[str] = []
    for match in SPATIAL_FUNC_RE.finditer(query):
        function = f"{match.group('prefix')}:{match.group('name')}"
        if function.lower() == "geof:distance":
            continue
        left_subject = bindings.get(match.group("a"))
        right_subject = bindings.get(match.group("b"))
        left_var = variable_for_subject(left_subject or "", entity_subjects, variable_subjects)
        right_var = variable_for_subject(right_subject or "", entity_subjects, variable_subjects)
        if left_var and right_var:
            condition = spatial_condition(function, left_var, right_var)
            if condition:
                conditions.append(condition)

    for match in DISTANCE_FILTER_RE.finditer(query):
        left_subject = bindings.get(match.group("a"))
        right_subject = bindings.get(match.group("b"))
        left_var = variable_for_subject(left_subject or "", entity_subjects, variable_subjects)
        right_var = variable_for_subject(right_subject or "", entity_subjects, variable_subjects)
        if left_var and right_var:
            conditions.append(
                distance_condition(left_var, right_var, match.group("op"), match.group("value"))
            )

    if not conditions:
        return None

    select_match = SELECT_VAR_RE.search(query)
    count_match = COUNT_RE.search(query)
    if count_match:
        alias_match = re.search(r"AS\s+(\?\w+)", query, re.IGNORECASE)
        alias = alias_match.group(1).lstrip("?") if alias_match else "count"
        return_clause = f"RETURN count(DISTINCT candidate) AS {alias}"
        signature = f"{alias} agtype"
    else:
        alias = select_match.group("var").lstrip("?") if select_match else "result"
        return_clause = f"RETURN DISTINCT candidate.uri AS {alias}"
        signature = f"{alias} agtype"

    anchor_lines = []
    anchor_where = []
    for var, entity in sorted(entity_matches.items()):
        anchor_lines.append(f"({var}:{entity.label})")
        anchor_where.append(uri_predicate(var, entity.uri))

    branches: List[str] = []
    for label in candidate_labels:
        match_clause = "MATCH " + ", ".join(anchor_lines + [f"(candidate:{label})"])
        where_clause = "WHERE " + "\n  AND ".join(anchor_where + conditions)
        branches.append("\n".join([match_clause, where_clause, return_clause]))

    cypher = "\nUNION\n".join(branches)
    return CypherConversion(
        "converted",
        "type_spatial_select",
        cypher,
        cypher_wrapper(cypher, signature),
        signature,
    )


def convert_question(
    question: Question,
    query: str,
    uri_lookup: Dict[str, EntityMatch],
    loaded_labels: Set[str],
) -> CypherConversion:
    bindings = geometry_bindings(query)
    types = type_bindings(query)

    for converter in (
        lambda: convert_area(question, query, bindings, uri_lookup),
        lambda: convert_binary_ask(question, query, bindings, uri_lookup),
        lambda: convert_type_spatial_select(
            question, query, bindings, types, uri_lookup, loaded_labels
        ),
    ):
        converted = converter()
        if converted is not None:
            return converted

    missing_labels = sorted(label for label in TYPE_ANY_RE.findall(query) if label not in loaded_labels)
    if missing_labels:
        return CypherConversion(
            "missing_label",
            "unsupported",
            note="missing label(s): " + "|".join(missing_labels),
        )
    return CypherConversion(
        "unsupported_pattern",
        "unsupported",
        note="SPARQL pattern is not covered by the current Cypher generator.",
    )


def write_outputs(
    output_dir: Path,
    rows: List[Dict[str, str]],
    country_rows: List[Dict[str, str]],
    summary_rows: List[Dict[str, str]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    main_fields = [
        "id",
        "resolved_id",
        "country_scope",
        "countries",
        "category",
        "status",
        "kind",
        "spatial_functions",
        "question",
        "sparql",
        "referenced_types",
        "referenced_entities",
        "resolved_entity_labels",
        "missing_entities",
        "missing_labels",
        "age_cypher",
        "age_sql_wrapper",
        "return_signature",
        "note",
    ]
    with (output_dir / "geoquestions_spatial_age_cypher.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=main_fields)
        writer.writeheader()
        writer.writerows(rows)

    country_fields = ["country"] + main_fields
    with (output_dir / "geoquestions_spatial_age_cypher_by_country.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=country_fields)
        writer.writeheader()
        writer.writerows(country_rows)

    summary_fields = [
        "country",
        "spatial_questions",
        "converted",
        "missing_entity",
        "missing_label",
        "unsupported_pattern",
    ]
    with (output_dir / "geoquestions_spatial_age_cypher_summary_by_country.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summary_rows)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate AGE Cypher CSVs for GeoQuestions spatial operations."
    )
    parser.add_argument(
        "--input",
        default="benchmark/queries/GeoQuestions1089.csv",
        help="GeoQuestions1089 CSV or JSON",
    )
    parser.add_argument(
        "--answers",
        default=None,
        help="optional GeoQuestions answers JSON used when --input has no Answer field",
    )
    parser.add_argument(
        "--manifest",
        action="append",
        default=None,
        help=(
            "AGE load manifest. Repeat this option to combine multiple loaded "
            "artifact manifests, for example unified plus gadm."
        ),
    )
    parser.add_argument(
        "--vertices-dir",
        action="append",
        default=None,
        help=(
            "vertex CSV directory used to resolve URI labels. Repeat this option "
            "in the same order/style as --manifest when artifacts are separate."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark/results/geoquestions/cypher",
        help="output directory",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest_paths = args.manifest or ["benchmark/results/load/yago2geo/unified/manifest.json"]
    vertices_dirs = args.vertices_dir or ["benchmark/results/load/yago2geo/unified/vertices"]

    questions = read_questions(
        Path(args.input),
        Path(args.answers) if args.answers else None,
    )
    manifests = [load_manifest(Path(path)) for path in manifest_paths]
    loaded_labels = set()
    for manifest in manifests:
        loaded_labels.update(manifest["vertex_counts"])

    spatial_questions = [
        question
        for question in questions
        if spatial_functions(question.resolved_query)
    ]
    needed_uris = {
        uri
        for question in spatial_questions
        for uri in referenced_entities(question.resolved_query)
    }
    uri_lookup = build_uri_lookup([Path(path) for path in vertices_dirs], needed_uris)

    rows: List[Dict[str, str]] = []
    country_rows: List[Dict[str, str]] = []
    country_status_counts: Dict[str, Counter[str]] = defaultdict(Counter)

    for question in spatial_questions:
        query = normalize_query(question.resolved_query)
        functions = spatial_functions(query)
        entities = referenced_entities(query)
        types = sorted(TYPE_ANY_RE.findall(query))
        conversion = convert_question(question, query, uri_lookup, loaded_labels)
        countries = infer_countries(question, query, entities, uri_lookup, loaded_labels)
        country_scope = countries[0] if len(countries) == 1 else "multi"
        missing_entities = sorted(uri for uri in entities if uri not in uri_lookup)
        missing_labels = sorted(label for label in types if label not in loaded_labels)
        resolved_labels = sorted(
            {uri_lookup[uri].label for uri in entities if uri in uri_lookup}
        )
        row = {
            "id": str(question.qid),
            "resolved_id": str(question.resolved_qid),
        "country_scope": country_scope,
        "countries": "|".join(countries),
        "category": question.category,
        "status": conversion.status,
            "kind": conversion.kind,
            "spatial_functions": "|".join(functions),
            "question": question.question,
            "sparql": query,
            "referenced_types": "|".join(types),
            "referenced_entities": "|".join(entities),
            "resolved_entity_labels": "|".join(resolved_labels),
            "missing_entities": "|".join(missing_entities),
            "missing_labels": "|".join(missing_labels),
            "age_cypher": conversion.cypher,
            "age_sql_wrapper": conversion.sql_wrapper,
            "return_signature": conversion.return_signature,
            "note": conversion.note,
        }
        rows.append(row)
        for country in countries:
            country_row = {"country": country}
            country_row.update(row)
            country_rows.append(country_row)
            country_status_counts[country][conversion.status] += 1

    summary_rows: List[Dict[str, str]] = []
    for country in sorted(country_status_counts):
        counter = country_status_counts[country]
        summary_rows.append(
            {
                "country": country,
                "spatial_questions": str(sum(counter.values())),
                "converted": str(counter.get("converted", 0)),
                "missing_entity": str(counter.get("missing_entity", 0)),
                "missing_label": str(counter.get("missing_label", 0)),
                "unsupported_pattern": str(counter.get("unsupported_pattern", 0)),
            }
        )

    write_outputs(Path(args.output_dir), rows, country_rows, summary_rows)

    status_counts = Counter(row["status"] for row in rows)
    print(f"spatial_questions={len(rows)}")
    for status, count in status_counts.most_common():
        print(f"{status}={count}")
    print("by_country")
    for row in summary_rows:
        print(
            f"{row['country']}: spatial={row['spatial_questions']} "
            f"converted={row['converted']} missing_entity={row['missing_entity']} "
            f"missing_label={row['missing_label']} unsupported={row['unsupported_pattern']}"
        )
    print(f"wrote={Path(args.output_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
