#!/usr/bin/env python3
"""Prepare GeoQuestions1089 for the current YAGO2Geo AGE graph.

The current age-spatial implementation stores GeoSPARQL WKT values in physical
PostGIS columns on AGE label tables. Cypher cannot yet call PostGIS functions on
those physical columns directly, so the generated executable queries use a
shared SQL setup view/table over the AGE label tables and then run PostGIS
predicates against the physical geom column.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


YAGO_BASE = "http://yago-knowledge.org/resource/"
Y2GEOR_BASE = "http://kr.di.uoa.gr/yago2geo/resource/"

ENTITY_TOKEN_RE = r"(?:<https?://[^>\s]+>|(?:yago|y2geor):[^\s\{\};,]+)"
GEOM_BINDING_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN_RE}|\?\w+)\s+geo:hasGeometry\s+"
    r"(?P<geom>\?\w+)\s*\.\s*(?P=geom)\s+geo:asWKT\s+(?P<wkt>\?\w+)",
    re.IGNORECASE,
)
TYPE_RE = re.compile(r"rdf:type\s+y2geoo:([A-Za-z0-9_]+)", re.IGNORECASE)
FUNC_RE = re.compile(
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
PROPERTY_LOOKUP_RE = re.compile(
    rf"SELECT\s+(?:DISTINCT\s+)?(?P<select>\?\w+|\([^)]*\)\s+AS\s+\?\w+)\s+WHERE\s*"
    rf"\{{\s*(?P<entity>{ENTITY_TOKEN_RE})\s+(?P<predicate>[A-Za-z][\w-]*:[A-Za-z_][\w-]*)\s+"
    r"(?P<object>\?\w+)\s*\}\s*",
    re.IGNORECASE,
)


PROPERTY_MAP = {
    "hasGAG_Population": "population",
    "GAG_Population": "population",
    "POPULATION": "population",
    "hasPopulation": "population",
    "AREASQKM": "area_sqkm",
    "hasOSNI_AreaSqKM": "area_sqkm",
    "hasArea": "area_sqkm",
    "hasGAG_Name": "name",
    "GAG_Name": "name",
    "hasOSI_Name": "name",
    "hasOS_Name": "name",
    "hasOSNI_Name": "name",
    "hasONSI_Name": "name",
    "GNIS_NAME": "name",
    "hasGAG_ID": "source_id",
    "GAG_ID": "source_id",
    "hasOSI_ID": "source_id",
    "hasOS_ID": "source_id",
    "hasOSNI_ID": "source_id",
    "GLOBALID": "source_id",
    "GNIS_ID": "gnis_id",
    "STATE_NAME": "state_name",
    "STATE_FIPS": "state_fips",
    "COUNTY_FIP": "county_fips",
    "STCO_FIPSC": "county_fips",
    "PLACE_FIPS": "place_fips",
}

SPATIAL_FUNCTIONS = {
    ("geof", "sfwithin"): "within",
    ("strdf", "within"): "within",
    ("geof", "sfcontains"): "contains",
    ("strdf", "contains"): "contains",
    ("geof", "sftouches"): "touches",
    ("strdf", "touches"): "touches",
    ("geof", "sfintersects"): "intersects",
    ("strdf", "intersects"): "intersects",
    ("geof", "sfcrosses"): "crosses",
    ("strdf", "crosses"): "crosses",
    ("geof", "sfoverlaps"): "overlaps",
    ("strdf", "overlaps"): "overlaps",
    ("strdf", "left"): "left",
    ("strdf", "right"): "right",
    ("strdf", "above"): "above",
    ("strdf", "below"): "below",
}


@dataclass
class GeoQuestion:
    qid: int
    question: str
    query: str
    answer: str
    resolved_qid: int
    resolved_query: str


@dataclass
class Conversion:
    qid: int
    status: str
    kind: str
    sql: str = ""
    note: str = ""
    missing_labels: Tuple[str, ...] = ()


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip())


def local_name(prefixed: str) -> str:
    return prefixed.split(":", 1)[1] if ":" in prefixed else prefixed


def expand_entity(token: str) -> str:
    token = token.strip()
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    if token.startswith("yago:"):
        return YAGO_BASE + token.split(":", 1)[1]
    if token.startswith("y2geor:"):
        return Y2GEOR_BASE + token.split(":", 1)[1]
    raise ValueError(f"unsupported entity token: {token}")


def read_questions(path: Path) -> List[GeoQuestion]:
    raw: List[Dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw.append(row)

    def resolve_query(index: int, seen: Optional[Set[int]] = None) -> Tuple[int, str]:
        if seen is None:
            seen = set()
        if index in seen:
            return index, raw[index - 1]["Query"]
        seen.add(index)
        query = raw[index - 1]["Query"].strip()
        match = re.fullmatch(r"same-as:(\d+)", query, re.IGNORECASE)
        if not match:
            return index, query
        return resolve_query(int(match.group(1)), seen)

    questions: List[GeoQuestion] = []
    for idx, row in enumerate(raw, 1):
        resolved_qid, resolved_query = resolve_query(idx)
        questions.append(
            GeoQuestion(
                qid=idx,
                question=row["Question"],
                query=row["Query"],
                answer=row.get("Answer", ""),
                resolved_qid=resolved_qid,
                resolved_query=resolved_query,
            )
        )
    return questions


def load_manifest(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def referenced_types(query: str) -> Set[str]:
    return set(TYPE_RE.findall(query))


def referenced_entities(query: str) -> List[str]:
    entities: List[str] = []
    for token in re.findall(ENTITY_TOKEN_RE, query):
        try:
            entities.append(expand_entity(token))
        except ValueError:
            continue
    return entities


def geometry_bindings(query: str) -> Dict[str, str]:
    bindings: Dict[str, str] = {}
    for match in GEOM_BINDING_RE.finditer(query):
        bindings[match.group("wkt")] = match.group("subject")
    return bindings


def entity_cte(alias: str, uri: str) -> str:
    q_uri = sql_literal(uri)
    return (
        f"{alias} AS (\n"
        "    SELECT *\n"
        "    FROM gq_vertex\n"
        f"    WHERE uri = {q_uri} OR yago_uri = {q_uri}\n"
        f"    ORDER BY CASE WHEN uri = {q_uri} THEN 0 ELSE 1 END, label, graphid\n"
        "    LIMIT 1\n"
        ")"
    )


def spatial_expr(function_name: str, left_sql: str, right_sql: str) -> Optional[str]:
    if function_name == "within":
        return f"public.ST_Within({left_sql}, {right_sql})"
    if function_name == "contains":
        return f"public.ST_Contains({left_sql}, {right_sql})"
    if function_name == "touches":
        return f"public.ST_Touches({left_sql}, {right_sql})"
    if function_name == "intersects":
        return f"public.ST_Intersects({left_sql}, {right_sql})"
    if function_name == "crosses":
        return f"public.ST_Crosses({left_sql}, {right_sql})"
    if function_name == "overlaps":
        return f"public.ST_Overlaps({left_sql}, {right_sql})"
    if function_name == "left":
        return f"pg_temp.gq_left({left_sql}, {right_sql})"
    if function_name == "right":
        return f"pg_temp.gq_right({left_sql}, {right_sql})"
    if function_name == "above":
        return f"pg_temp.gq_above({left_sql}, {right_sql})"
    if function_name == "below":
        return f"pg_temp.gq_below({left_sql}, {right_sql})"
    return None


def normalized_geom_sql(alias: str) -> str:
    return f"pg_temp.gq_to_4326({alias}.geom)"


def convert_geometry_lookup(question: GeoQuestion, query: str) -> Optional[Conversion]:
    if "geo:hasGeometry" not in query or "geo:asWKT" not in query:
        return None
    if "FILTER" in query or "rdf:type" in query or "strdf:area" in query or "BIND" in query:
        return None
    bindings = geometry_bindings(query)
    entity_tokens = [subject for subject in bindings.values() if not subject.startswith("?")]
    if len(entity_tokens) != 1:
        return None
    uri = expand_entity(entity_tokens[0])
    sql = (
        "WITH\n"
        f"{entity_cte('e', uri)}\n"
        "SELECT public.ST_AsText(e.geom) AS wkt\n"
        "FROM e\n"
        "WHERE e.geom IS NOT NULL;"
    )
    return Conversion(question.qid, "converted", "geometry_lookup", sql)


def convert_property_lookup(question: GeoQuestion, query: str) -> Optional[Conversion]:
    if "geo:hasGeometry" in query or "rdf:type" in query:
        return None
    match = PROPERTY_LOOKUP_RE.fullmatch(query)
    if not match:
        return None
    predicate = local_name(match.group("predicate"))
    column = PROPERTY_MAP.get(predicate)
    if column is None:
        return None
    uri = expand_entity(match.group("entity"))
    alias = match.group("object").lstrip("?")
    if "xsd:integer" in match.group("select"):
        expr = f"NULLIF(e.{column}, '')::numeric::bigint"
    elif "xsd:double" in match.group("select"):
        expr = f"NULLIF(e.{column}, '')::double precision"
    else:
        expr = f"e.{column}"
    sql = (
        "WITH\n"
        f"{entity_cte('e', uri)}\n"
        f"SELECT {expr} AS {sql_identifier(alias)}\n"
        "FROM e\n"
        f"WHERE e.{column} IS NOT NULL AND e.{column} <> '';"
    )
    return Conversion(question.qid, "converted", "property_lookup", sql)


def convert_area_lookup(question: GeoQuestion, query: str) -> Optional[Conversion]:
    if "strdf:area" not in query:
        return None
    bindings = geometry_bindings(query)
    entity_tokens = [subject for subject in bindings.values() if not subject.startswith("?")]
    if len(entity_tokens) != 1:
        return None
    uri = expand_entity(entity_tokens[0])
    match = re.search(r"AS\s+(\?\w+)", query, re.IGNORECASE)
    alias = match.group(1).lstrip("?") if match else "area"
    sql = (
        "WITH\n"
        f"{entity_cte('e', uri)}\n"
        f"SELECT pg_temp.gq_area(e.geom) AS {sql_identifier(alias)}\n"
        "FROM e\n"
        "WHERE e.geom IS NOT NULL;"
    )
    return Conversion(question.qid, "converted", "area_lookup", sql)


def convert_binary_ask(question: GeoQuestion, query: str) -> Optional[Conversion]:
    if not query.upper().startswith("ASK "):
        return None
    bindings = geometry_bindings(query)
    if len(bindings) < 2:
        return None

    distance = DISTANCE_FILTER_RE.search(query)
    if distance:
        left_subject = bindings.get(distance.group("a"))
        right_subject = bindings.get(distance.group("b"))
        if not left_subject or not right_subject:
            return None
        if left_subject.startswith("?") or right_subject.startswith("?"):
            return None
        left_uri = expand_entity(left_subject)
        right_uri = expand_entity(right_subject)
        op = distance.group("op")
        value = distance.group("value")
        predicate = f"pg_temp.gq_distance_m({normalized_geom_sql('a')}, {normalized_geom_sql('b')}) {op} {value}"
        sql = (
            "WITH\n"
            f"{entity_cte('a', left_uri)},\n"
            f"{entity_cte('b', right_uri)}\n"
            "SELECT EXISTS (\n"
            "    SELECT 1\n"
            "    FROM a, b\n"
            "    WHERE a.geom IS NOT NULL\n"
            "      AND b.geom IS NOT NULL\n"
            f"      AND {predicate}\n"
            ") AS boolean;"
        )
        return Conversion(question.qid, "converted", "ask_distance", sql)

    func = FUNC_RE.search(query)
    if not func:
        return None
    canonical = SPATIAL_FUNCTIONS.get((func.group("prefix").lower(), func.group("name").lower()))
    if canonical is None:
        return None
    left_subject = bindings.get(func.group("a"))
    right_subject = bindings.get(func.group("b"))
    if not left_subject or not right_subject:
        return None
    if left_subject.startswith("?") or right_subject.startswith("?"):
        return None
    left_uri = expand_entity(left_subject)
    right_uri = expand_entity(right_subject)
    predicate = spatial_expr(canonical, normalized_geom_sql("a"), normalized_geom_sql("b"))
    if predicate is None:
        return None
    sql = (
        "WITH\n"
        f"{entity_cte('a', left_uri)},\n"
        f"{entity_cte('b', right_uri)}\n"
        "SELECT EXISTS (\n"
        "    SELECT 1\n"
        "    FROM a, b\n"
        "    WHERE a.geom IS NOT NULL\n"
        "      AND b.geom IS NOT NULL\n"
        f"      AND {predicate}\n"
        ") AS boolean;"
    )
    return Conversion(question.qid, "converted", "ask_spatial", sql)


def convert_type_spatial_select(
    question: GeoQuestion, query: str, loaded_labels: Set[str]
) -> Optional[Conversion]:
    if "rdf:type" not in query or "geo:hasGeometry" not in query or "FILTER" not in query:
        return None
    types = tuple(sorted(referenced_types(query)))
    if not types:
        return None
    missing = tuple(sorted(t for t in types if t not in loaded_labels))
    if missing:
        return Conversion(
            question.qid,
            "unsupported_missing_labels",
            "type_spatial_select",
            missing_labels=missing,
            note="GeoQuestions references rdf:type labels that are not loaded in the current graph.",
        )

    bindings = geometry_bindings(query)
    func = FUNC_RE.search(query)
    if not func:
        return None
    canonical = SPATIAL_FUNCTIONS.get((func.group("prefix").lower(), func.group("name").lower()))
    if canonical is None:
        return None

    left_subject = bindings.get(func.group("a"))
    right_subject = bindings.get(func.group("b"))
    if not left_subject or not right_subject:
        return None

    subjects = set(bindings.values())
    entity_subjects = [subject for subject in subjects if not subject.startswith("?")]
    variable_subjects = [subject for subject in subjects if subject.startswith("?")]
    if len(entity_subjects) != 1 or not variable_subjects:
        return None
    anchor_uri = expand_entity(entity_subjects[0])
    candidate_var = sorted(variable_subjects)[0]

    def subject_sql(subject: str) -> Optional[str]:
        if subject == entity_subjects[0]:
            return normalized_geom_sql("anchor")
        if subject == candidate_var:
            return normalized_geom_sql("candidate")
        return None

    left_sql = subject_sql(left_subject)
    right_sql = subject_sql(right_subject)
    if left_sql is None or right_sql is None:
        return None

    predicate = spatial_expr(canonical, left_sql, right_sql)
    if predicate is None:
        return None

    labels_sql = ", ".join(sql_literal(label) for label in types)
    output_alias = re.search(r"SELECT\s+(?:DISTINCT\s+)?(\?\w+)", query, re.IGNORECASE)
    alias = output_alias.group(1).lstrip("?") if output_alias else candidate_var.lstrip("?")

    is_count = bool(re.search(r"SELECT\s*\(\s*COUNT", query, re.IGNORECASE))
    if is_count:
        select_clause = f"COUNT(DISTINCT candidate.uri) AS {sql_identifier(alias)}"
        distinct = ""
        order_by = ""
    else:
        select_clause = f"candidate.uri AS {sql_identifier(alias)}"
        distinct = "DISTINCT "
        order_by = f"\nORDER BY {sql_identifier(alias)}"

    sql = (
        "WITH\n"
        f"{entity_cte('anchor', anchor_uri)},\n"
        "candidates AS (\n"
        "    SELECT *\n"
        "    FROM gq_vertex\n"
        f"    WHERE label IN ({labels_sql})\n"
        "      AND geom IS NOT NULL\n"
        ")\n"
        f"SELECT {distinct}{select_clause}\n"
        "FROM anchor, candidates candidate\n"
        "WHERE anchor.geom IS NOT NULL\n"
        f"  AND {predicate}"
        f"{order_by};"
    )
    return Conversion(question.qid, "converted", "type_spatial_select", sql)


def classify_only(question: GeoQuestion, query: str, loaded_labels: Set[str]) -> Conversion:
    missing = tuple(sorted(t for t in referenced_types(query) if t not in loaded_labels))
    if missing:
        return Conversion(
            question.qid,
            "unsupported_missing_labels",
            "unconverted",
            missing_labels=missing,
            note="GeoQuestions references rdf:type labels that are not loaded in the current graph.",
        )
    if "same-as:" in question.query:
        return Conversion(question.qid, "unsupported_no_pattern", "same_as_unconverted")
    return Conversion(question.qid, "unsupported_no_pattern", "unconverted")


def convert_question(question: GeoQuestion, loaded_labels: Set[str]) -> Conversion:
    query = normalize_query(question.resolved_query)
    for converter in (
        convert_property_lookup,
        convert_area_lookup,
        convert_geometry_lookup,
        convert_binary_ask,
    ):
        result = converter(question, query)
        if result is not None:
            return result

    result = convert_type_spatial_select(question, query, loaded_labels)
    if result is not None:
        return result

    return classify_only(question, query, loaded_labels)


def setup_sql(manifest: Dict[str, object]) -> str:
    graph_name = str(manifest.get("graph_name", "yago2geo"))
    vertex_counts = manifest["vertex_counts"]
    spatial_labels = set(manifest.get("loader_srids_by_label", {}))

    selects: List[str] = []
    for label in sorted(vertex_counts):
        geom_expr = "geom::public.geometry" if label in spatial_labels else "NULL::public.geometry"
        relation = f"{sql_identifier(graph_name)}.{sql_identifier(label)}"
        selects.append(
            "\n".join(
                [
                    f"SELECT {sql_literal(label)}::text AS label,",
                    "       id AS graphid,",
                    "       properties,",
                    "       NULLIF(properties->>'\"uri\"', '') AS uri,",
                    "       NULLIF(properties->>'\"yago_uri\"', '') AS yago_uri,",
                    "       NULLIF(properties->>'\"name\"', '') AS name,",
                    "       NULLIF(properties->>'\"rdf_type\"', '') AS rdf_type,",
                    "       NULLIF(properties->>'\"population\"', '') AS population,",
                    "       NULLIF(properties->>'\"area_sqkm\"', '') AS area_sqkm,",
                    "       NULLIF(properties->>'\"source_id\"', '') AS source_id,",
                    "       NULLIF(properties->>'\"gnis_id\"', '') AS gnis_id,",
                    "       NULLIF(properties->>'\"state_name\"', '') AS state_name,",
                    "       NULLIF(properties->>'\"state_fips\"', '') AS state_fips,",
                    "       NULLIF(properties->>'\"county_fips\"', '') AS county_fips,",
                    "       NULLIF(properties->>'\"place_fips\"', '') AS place_fips,",
                    f"       {geom_expr} AS geom",
                    f"FROM ONLY {relation}",
                ]
            )
        )

    return (
        "LOAD 'age';\n"
        "SET search_path TO ag_catalog, public;\n"
        "DROP TABLE IF EXISTS pg_temp.gq_vertex;\n"
        "CREATE TEMP TABLE gq_vertex AS\n"
        + "\nUNION ALL\n".join(selects)
        + ";\n\n"
        "CREATE INDEX gq_vertex_uri_idx ON gq_vertex(uri);\n"
        "CREATE INDEX gq_vertex_yago_uri_idx ON gq_vertex(yago_uri);\n"
        "CREATE INDEX gq_vertex_label_idx ON gq_vertex(label);\n"
        "CREATE INDEX gq_vertex_geom_idx ON gq_vertex USING gist(geom);\n"
        "ANALYZE gq_vertex;\n\n"
        "CREATE OR REPLACE FUNCTION pg_temp.gq_to_4326(g public.geometry)\n"
        "RETURNS public.geometry\n"
        "LANGUAGE sql\n"
        "IMMUTABLE\n"
        "STRICT\n"
        "AS $$\n"
        "    SELECT CASE\n"
        "        WHEN public.ST_SRID(g) = 4326 THEN g\n"
        "        ELSE public.ST_Transform(g, 4326)\n"
        "    END\n"
        "$$;\n\n"
        "CREATE OR REPLACE FUNCTION pg_temp.gq_distance_m(a public.geometry, b public.geometry)\n"
        "RETURNS double precision\n"
        "LANGUAGE sql\n"
        "IMMUTABLE\n"
        "STRICT\n"
        "AS $$\n"
        "    SELECT public.ST_Distance(\n"
        "        public.ST_Centroid(pg_temp.gq_to_4326(a))::public.geography,\n"
        "        public.ST_Centroid(pg_temp.gq_to_4326(b))::public.geography\n"
        "    )\n"
        "$$;\n\n"
        "CREATE OR REPLACE FUNCTION pg_temp.gq_area(g public.geometry)\n"
        "RETURNS double precision\n"
        "LANGUAGE sql\n"
        "IMMUTABLE\n"
        "STRICT\n"
        "AS $$\n"
        "    SELECT public.ST_Area(g)\n"
        "$$;\n\n"
        "CREATE OR REPLACE FUNCTION pg_temp.gq_left(a public.geometry, b public.geometry)\n"
        "RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$\n"
        "    SELECT public.ST_XMax(pg_temp.gq_to_4326(a)::public.box3d) < public.ST_XMin(pg_temp.gq_to_4326(b)::public.box3d)\n"
        "$$;\n"
        "CREATE OR REPLACE FUNCTION pg_temp.gq_right(a public.geometry, b public.geometry)\n"
        "RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$\n"
        "    SELECT public.ST_XMin(pg_temp.gq_to_4326(a)::public.box3d) > public.ST_XMax(pg_temp.gq_to_4326(b)::public.box3d)\n"
        "$$;\n"
        "CREATE OR REPLACE FUNCTION pg_temp.gq_above(a public.geometry, b public.geometry)\n"
        "RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$\n"
        "    SELECT public.ST_YMin(pg_temp.gq_to_4326(a)::public.box3d) > public.ST_YMax(pg_temp.gq_to_4326(b)::public.box3d)\n"
        "$$;\n"
        "CREATE OR REPLACE FUNCTION pg_temp.gq_below(a public.geometry, b public.geometry)\n"
        "RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$\n"
        "    SELECT public.ST_YMax(pg_temp.gq_to_4326(a)::public.box3d) < public.ST_YMin(pg_temp.gq_to_4326(b)::public.box3d)\n"
        "$$;\n"
    )


def write_outputs(
    output_dir: Path,
    questions: Sequence[GeoQuestion],
    conversions: Sequence[Conversion],
    manifest: Dict[str, object],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    sql_dir = output_dir / "age_sql"
    query_dir = sql_dir / "queries"
    query_dir.mkdir(parents=True, exist_ok=True)

    (sql_dir / "_setup.sql").write_text(setup_sql(manifest), encoding="utf-8")

    q_by_id = {q.qid: q for q in questions}
    converted = [item for item in conversions if item.status == "converted"]
    for item in converted:
        question = q_by_id[item.qid]
        body = (
            f"-- GeoQuestions1089 #{item.qid}\n"
            f"-- Question: {question.question}\n"
            f"-- Kind: {item.kind}\n"
            f"-- Original SPARQL: {normalize_query(question.resolved_query)}\n"
            f"{item.sql}\n"
        )
        (query_dir / f"q{item.qid:04d}.sql").write_text(body, encoding="utf-8")

    run_lines = [
        "\\timing on",
        "\\i /benchmark/results/geoquestions/age_sql/_setup.sql",
        "",
    ]
    for item in converted:
        run_lines.extend(
            [
                f"\\echo 'GeoQuestion {item.qid:04d} {item.kind}'",
                f"\\i /benchmark/results/geoquestions/age_sql/queries/q{item.qid:04d}.sql",
                "",
            ]
        )
    (sql_dir / "run_converted_all.sql").write_text(
        "\n".join(run_lines) + "\n", encoding="utf-8"
    )

    sample_lines = [
        "\\timing on",
        "\\i /benchmark/results/geoquestions/age_sql/_setup.sql",
        "",
    ]
    for item in converted[:25]:
        sample_lines.extend(
            [
                f"\\echo 'GeoQuestion {item.qid:04d} {item.kind}'",
                f"\\i /benchmark/results/geoquestions/age_sql/queries/q{item.qid:04d}.sql",
                "",
            ]
        )
    (sql_dir / "run_converted_sample.sql").write_text(
        "\n".join(sample_lines) + "\n", encoding="utf-8"
    )

    validation_ids = [1, 4, 27, 153, 176, 300]
    converted_ids = {item.qid for item in converted}
    validation_lines = [
        "\\timing on",
        "\\i /benchmark/results/geoquestions/age_sql/_setup.sql",
        "",
    ]
    for qid in validation_ids:
        if qid not in converted_ids:
            continue
        item = next(item for item in converted if item.qid == qid)
        validation_lines.extend(
            [
                f"\\echo 'GeoQuestion {item.qid:04d} {item.kind}'",
                f"\\i /benchmark/results/geoquestions/age_sql/queries/q{item.qid:04d}.sql",
                "",
            ]
        )
    (sql_dir / "run_validation.sql").write_text(
        "\n".join(validation_lines) + "\n", encoding="utf-8"
    )

    entity_refs: List[Tuple[int, str]] = []
    for question in questions:
        for uri in referenced_entities(question.resolved_query):
            entity_refs.append((question.qid, uri))

    value_rows = [
        f"    ({qid}, {sql_literal(uri)})" for qid, uri in sorted(set(entity_refs))
    ]
    entity_coverage_sql = [
        "\\i /benchmark/results/geoquestions/age_sql/_setup.sql",
        "",
        "WITH refs(qid, uri) AS (",
        "VALUES",
        ",\n".join(value_rows),
        "), resolved AS (",
        "    SELECT r.qid, r.uri, v.label, v.graphid",
        "    FROM refs r",
        "    LEFT JOIN LATERAL (",
        "        SELECT label, graphid",
        "        FROM gq_vertex",
        "        WHERE uri = r.uri OR yago_uri = r.uri",
        "        ORDER BY CASE WHEN uri = r.uri THEN 0 ELSE 1 END, label, graphid",
        "        LIMIT 1",
        "    ) v ON true",
        ")",
        "SELECT",
        "    COUNT(*) AS entity_references,",
        "    COUNT(*) FILTER (WHERE label IS NOT NULL) AS resolved_references,",
        "    COUNT(*) FILTER (WHERE label IS NULL) AS missing_references,",
        "    COUNT(DISTINCT qid) AS questions_with_entity_refs,",
        "    COUNT(DISTINCT qid) FILTER (WHERE label IS NULL) AS questions_with_missing_entity_refs",
        "FROM resolved;",
        "",
        "WITH refs(qid, uri) AS (",
        "VALUES",
        ",\n".join(value_rows),
        "), resolved AS (",
        "    SELECT r.qid, r.uri, v.label",
        "    FROM refs r",
        "    LEFT JOIN LATERAL (",
        "        SELECT label",
        "        FROM gq_vertex",
        "        WHERE uri = r.uri OR yago_uri = r.uri",
        "        ORDER BY CASE WHEN uri = r.uri THEN 0 ELSE 1 END, label, graphid",
        "        LIMIT 1",
        "    ) v ON true",
        ")",
        "SELECT qid, uri",
        "FROM resolved",
        "WHERE label IS NULL",
        "ORDER BY qid, uri",
        "LIMIT 200;",
    ]
    (sql_dir / "check_entity_coverage.sql").write_text(
        "\n".join(entity_coverage_sql) + "\n", encoding="utf-8"
    )

    with (output_dir / "geoquestions_clean.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "id",
            "resolved_id",
            "question",
            "query",
            "resolved_query",
            "answer",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for q in questions:
            writer.writerow(
                {
                    "id": q.qid,
                    "resolved_id": q.resolved_qid,
                    "question": q.question,
                    "query": q.query,
                    "resolved_query": q.resolved_query,
                    "answer": q.answer,
                }
            )

    with (output_dir / "conversion_status.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "id",
            "resolved_id",
            "question",
            "status",
            "kind",
            "missing_labels",
            "referenced_types",
            "referenced_entities",
            "sql_file",
            "note",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in conversions:
            q = q_by_id[item.qid]
            sql_file = (
                f"benchmark/results/geoquestions/age_sql/queries/q{item.qid:04d}.sql"
                if item.status == "converted"
                else ""
            )
            writer.writerow(
                {
                    "id": item.qid,
                    "resolved_id": q.resolved_qid,
                    "question": q.question,
                    "status": item.status,
                    "kind": item.kind,
                    "missing_labels": "|".join(item.missing_labels),
                    "referenced_types": "|".join(sorted(referenced_types(q.resolved_query))),
                    "referenced_entities": "|".join(referenced_entities(q.resolved_query)),
                    "sql_file": sql_file,
                    "note": item.note,
                }
            )

    status_counts = Counter(item.status for item in conversions)
    kind_counts = Counter(item.kind for item in conversions if item.status == "converted")
    missing_counts: Counter[str] = Counter()
    for item in conversions:
        missing_counts.update(item.missing_labels)

    summary = [
        "# GeoQuestions1089 AGE Conversion Summary",
        "",
        f"- input questions: {len(questions)}",
        f"- converted queries: {status_counts.get('converted', 0)}",
        f"- unsupported because labels are not loaded: {status_counts.get('unsupported_missing_labels', 0)}",
        f"- unsupported by current regex translator: {status_counts.get('unsupported_no_pattern', 0)}",
        "",
        "## Converted Kinds",
        "",
    ]
    for kind, count in kind_counts.most_common():
        summary.append(f"- {kind}: {count}")
    summary.extend(["", "## Missing RDF Types", ""])
    for label, count in missing_counts.most_common(40):
        summary.append(f"- {label}: {count}")
    summary.extend(
        [
            "",
            "## Generated SQL",
            "",
            "- setup: `benchmark/results/geoquestions/age_sql/_setup.sql`",
            "- sample runner: `benchmark/results/geoquestions/age_sql/run_converted_sample.sql`",
            "- validation runner: `benchmark/results/geoquestions/age_sql/run_validation.sql`",
            "- all converted runner: `benchmark/results/geoquestions/age_sql/run_converted_all.sql`",
            "- entity coverage check: `benchmark/results/geoquestions/age_sql/check_entity_coverage.sql`",
        ]
    )
    (output_dir / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare GeoQuestions1089 conversion artifacts for AGE/age-spatial."
    )
    parser.add_argument(
        "--input",
        default="benchmark/queries/GeoQuestions1089.csv",
        help="GeoQuestions1089 CSV file",
    )
    parser.add_argument(
        "--manifest",
        default="benchmark/results/load/yago2geo/unified/manifest.json",
        help="unified YAGO2Geo AGE load manifest",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark/results/geoquestions",
        help="output directory",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    questions = read_questions(Path(args.input))
    manifest = load_manifest(Path(args.manifest))
    loaded_labels = set(manifest["vertex_counts"])
    conversions = [convert_question(question, loaded_labels) for question in questions]
    write_outputs(Path(args.output_dir), questions, conversions, manifest)

    status_counts = Counter(item.status for item in conversions)
    kind_counts = Counter(item.kind for item in conversions if item.status == "converted")
    print(f"questions={len(questions)}")
    print(f"converted={status_counts.get('converted', 0)}")
    print(f"unsupported_missing_labels={status_counts.get('unsupported_missing_labels', 0)}")
    print(f"unsupported_no_pattern={status_counts.get('unsupported_no_pattern', 0)}")
    for kind, count in kind_counts.most_common():
        print(f"{kind}={count}")
    print(f"wrote={Path(args.output_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
