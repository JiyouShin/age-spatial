#!/usr/bin/env python3
"""Generate broader SQL-wrapped AGE Cypher translations for GeoQuestions.

This generator is intentionally more RDF-shaped than
prepare-geoquestions-age-cypher.py:

* A SPARQL `rdf:type y2geoo:Label` becomes an AGE label candidate.
* If a geometry-bearing RDF subject has no explicit rdf:type, the generated
  query does not pre-resolve the URI to one label. Instead it expands the
  subject over every loaded spatial label and filters by `uri`/`yago_uri`.
* Physical geometry is still read through AGE Cypher as `n.geom`. The final
  SQL wrapper then performs joins, aggregation, ordering, and projection.

The output is meant for inspection and later execution. It favors preserving
the original RDF query shape over producing the shortest possible query.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


GRAPH_NAME = "yago2geo"
YAGO_BASE = "http://yago-knowledge.org/resource/"
Y2GEOR_BASE = "http://kr.di.uoa.gr/yago2geo/resource/"

ENTITY_TOKEN_RE = r"(?:<https?://[^>\s]+>|(?:yago|y2geor):[^\s\{\};,\)]+|\?\w+)"
HAS_GEOM_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN_RE})\s+geo:hasGeometry\s+(?P<geom>\?\w+)",
    re.IGNORECASE,
)
HAS_GEOM_SEMI_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN_RE})\s+[^.{{}}]*?;\s*geo:hasGeometry\s+(?P<geom>\?\w+)",
    re.IGNORECASE,
)
AS_WKT_RE = re.compile(r"(?P<geom>\?\w+)\s+geo:asWKT\s+(?P<wkt>\?\w+)", re.IGNORECASE)
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
AREA_CALL_RE = re.compile(r"strdf:area\s*\(\s*(?P<wkt>\?\w+)\s*\)", re.IGNORECASE)
BIND_AREA_RE = re.compile(
    r"BIND\s*\(\s*strdf:area\s*\(\s*(?P<wkt>\?\w+)\s*\)\s+AS\s+(?P<var>\?\w+)\s*\)",
    re.IGNORECASE,
)
LIMIT_RE = re.compile(r"\bLIMIT\s+(?P<limit>\d+)", re.IGNORECASE)
GROUP_BY_RE = re.compile(
    r"\bGROUP\s+BY\s+(?P<vars>(?:\?\w+\s*)+)(?:ORDER\s+BY|LIMIT|$)",
    re.IGNORECASE,
)
ORDER_BY_RE = re.compile(r"\bORDER\s+BY\s+(?P<expr>.+?)(?:\s+LIMIT\s+\d+|$)", re.IGNORECASE)
PROP_TRIPLE_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN_RE})\s+"
    r"(?P<prefix>y2geoo|yago):(?P<predicate>[A-Za-z_][\w-]*)\s+"
    r"(?P<object>\?\w+|<https?://[^>]+>|\"[^\"]*\"(?:@\w+|\^\^[^\s]+)?|[^\s\.;\{{\}}]+)",
    re.IGNORECASE,
)
ANY_PREDICATE_TRIPLE_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN_RE})\s+"
    r"(?P<predicate>(?:rdf:type|(?:y2geoo|yago|geo):[A-Za-z_][\w-]*))\s+"
    r"(?P<object>\?\w+|<https?://[^>]+>|\"[^\"]*\"(?:@\w+|\^\^[^\s]+)?|[^\s\.;\{\}]+)",
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
    "hasOSM_Name": "name",
    "hasGADM_Name": "name",
    "GNIS_NAME": "name",
    "hasGAG_ID": "source_id",
    "GAG_ID": "source_id",
    "hasOSI_ID": "source_id",
    "hasOS_ID": "source_id",
    "hasOSNI_ID": "source_id",
    "hasOSM_ID": "source_id",
    "hasGADM_ID": "source_id",
    "GLOBALID": "source_id",
    "GNIS_ID": "gnis_id",
    "STATE_NAME": "state_name",
    "STATE_FIPS": "state_fips",
    "COUNTY_FIP": "county_fips",
    "STCO_FIPSC": "county_fips",
    "PLACE_FIPS": "place_fips",
    "hasGADM_Description": "gadm_description",
    "hasGADM_NationalLevel": "gadm_level",
    "hasGADM_UpperLevelUnit": "gadm_upper_level_unit",
}


SPATIAL_SQL = {
    "geof:sfwithin": "ST_Within",
    "strdf:within": "ST_Within",
    "geof:sfcontains": "ST_Contains",
    "strdf:contains": "ST_Contains",
    "geof:sftouches": "ST_Touches",
    "strdf:touches": "ST_Touches",
    "geof:sfintersects": "ST_Intersects",
    "strdf:intersects": "ST_Intersects",
    "geof:sfcrosses": "ST_Crosses",
    "strdf:crosses": "ST_Crosses",
    "geof:sfoverlaps": "ST_Overlaps",
    "strdf:overlaps": "ST_Overlaps",
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
class PropertyPattern:
    subject: str
    predicate: str
    column: str
    object: str


@dataclass
class CteSpec:
    name: str
    subject: str
    wkt_var: str
    labels: List[str]
    uri: Optional[str]
    columns: List[str]
    labelless: bool = False


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


def normalize_literal(token: str) -> str:
    token = token.strip()
    if token.startswith('"'):
        match = re.match(r'"(?P<value>(?:[^"\\]|\\.)*)"', token)
        if match:
            return bytes(match.group("value"), "utf-8").decode("unicode_escape")
    return token


def read_answers_json(path: Optional[Path]) -> Dict[int, str]:
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(qid): str(answer) for qid, answer in data.items()}


def read_questions(path: Path, answers_path: Optional[Path]) -> List[Question]:
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

    result: List[Question] = []
    for idx, row in enumerate(raw, 1):
        resolved_qid, resolved_query = resolve_query(idx)
        result.append(
            Question(
                qid=idx,
                question=row.get("Question", ""),
                query=row.get("Query", ""),
                answer=row.get("Answer", ""),
                category=resolve_category(idx),
                resolved_qid=resolved_qid,
                resolved_query=resolved_query,
            )
        )
    return result


def spatial_functions(query: str) -> List[str]:
    return re.findall(r"\b(?:geof|strdf):[A-Za-z_][\w-]*", query)


def load_spatial_labels(manifest_paths: Sequence[Path]) -> Tuple[List[str], Set[str], Dict[str, int]]:
    labels: Set[str] = set()
    all_labels: Set[str] = set()
    srids: Dict[str, int] = {}
    for path in manifest_paths:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        all_labels.update(manifest.get("vertex_counts", {}).keys())
        loader_srids = manifest.get("loader_srids_by_label", {})
        if loader_srids:
            for label, srid in loader_srids.items():
                labels.add(label)
                srids[label] = int(srid)
        else:
            labels.update(manifest.get("vertex_counts", {}).keys())
    return sorted(labels), all_labels, srids


def label_alias(label: str, loaded_labels: Set[str]) -> Optional[str]:
    if label in loaded_labels:
        return label
    lowered = label.lower()
    for candidate in loaded_labels:
        if candidate.lower() == lowered:
            return candidate
    return None


def type_bindings(query: str, loaded_labels: Set[str]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = defaultdict(list)
    for match in TYPE_RE.finditer(query):
        label = label_alias(match.group("label"), loaded_labels)
        if label:
            result[match.group("var")].append(label)
    return {var: sorted(set(labels)) for var, labels in result.items()}


def geometry_bindings(query: str) -> Dict[str, str]:
    geom_subject: Dict[str, str] = {}
    for regex in (HAS_GEOM_RE, HAS_GEOM_SEMI_RE):
        for match in regex.finditer(query):
            geom_subject[match.group("geom")] = match.group("subject")
    bindings: Dict[str, str] = {}
    for match in AS_WKT_RE.finditer(query):
        subject = geom_subject.get(match.group("geom"))
        if subject:
            bindings[match.group("wkt")] = subject
    return bindings


def property_patterns(query: str) -> List[PropertyPattern]:
    patterns: List[PropertyPattern] = []
    for match in PROP_TRIPLE_RE.finditer(query):
        predicate = match.group("predicate")
        if predicate not in PROPERTY_MAP:
            continue
        patterns.append(
            PropertyPattern(
                subject=match.group("subject"),
                predicate=predicate,
                column=PROPERTY_MAP[predicate],
                object=match.group("object"),
            )
        )
    return patterns


def unsupported_binding_relations(query: str, geometry_subjects: Iterable[str]) -> List[str]:
    geometry_vars = {subject for subject in geometry_subjects if subject.startswith("?")}
    if not geometry_vars:
        return []
    unsupported: List[str] = []
    for match in ANY_PREDICATE_TRIPLE_RE.finditer(query):
        predicate = match.group("predicate")
        if predicate.lower() in {"rdf:type", "geo:hasgeometry", "geo:aswkt"}:
            continue
        if predicate.startswith("y2geoo:") and predicate.split(":", 1)[1] in PROPERTY_MAP:
            continue
        if predicate.startswith("yago:") and predicate.split(":", 1)[1] in PROPERTY_MAP:
            continue
        subject = match.group("subject")
        obj = match.group("object")
        if subject in geometry_vars or obj in geometry_vars:
            unsupported.append(f"{subject} {predicate} {obj}")
    return unsupported


def uri_for_subject(subject: str) -> Optional[str]:
    if subject.startswith("?"):
        return None
    try:
        return expand_entity(subject)
    except ValueError:
        return None


def subject_label_candidates(
    subject: str,
    type_map: Dict[str, List[str]],
    spatial_labels: List[str],
    property_map: Dict[str, List[PropertyPattern]],
) -> List[str]:
    if subject.startswith("?") and type_map.get(subject):
        return type_map[subject]
    return []


def has_explicit_binding(
    subject: str,
    type_map: Dict[str, List[str]],
    property_map: Dict[str, List[PropertyPattern]],
) -> bool:
    if not subject.startswith("?"):
        return True
    return bool(type_map.get(subject) or property_map.get(subject))


def cte_name_for_wkt(wkt_var: str, used: Set[str]) -> str:
    base = "v_" + wkt_var.lstrip("?")
    base = re.sub(r"\W+", "_", base)
    name = base
    i = 2
    while name in used:
        name = f"{base}_{i}"
        i += 1
    used.add(name)
    return name


def collect_needed_columns(query: str, bindings: Dict[str, str], props: List[PropertyPattern]) -> Dict[str, Set[str]]:
    needed: Dict[str, Set[str]] = defaultdict(set)
    for pat in props:
        needed[pat.subject].add(pat.column)
    return needed


def build_ctes(
    query: str,
    spatial_labels: List[str],
    loaded_labels: Set[str],
) -> Tuple[List[CteSpec], Dict[str, str], Dict[str, str], List[PropertyPattern], List[str]]:
    notes: List[str] = []
    bindings = geometry_bindings(query)
    types = type_bindings(query, loaded_labels)
    props = property_patterns(query)
    prop_by_subject: Dict[str, List[PropertyPattern]] = defaultdict(list)
    for pat in props:
        prop_by_subject[pat.subject].append(pat)
    needed_columns = collect_needed_columns(query, bindings, props)

    used: Set[str] = set()
    ctes: List[CteSpec] = []
    wkt_to_cte: Dict[str, str] = {}
    subject_to_cte: Dict[str, str] = {}

    for wkt_var, subject in sorted(bindings.items()):
        labels = subject_label_candidates(subject, types, spatial_labels, prop_by_subject)
        labelless = not labels
        if labelless:
            notes.append(f"{subject} has no rdf:type; using label-less MATCH (n)")
        columns = sorted(needed_columns.get(subject, set()))
        name = cte_name_for_wkt(wkt_var, used)
        ctes.append(
            CteSpec(
                name=name,
                subject=subject,
                wkt_var=wkt_var,
                labels=labels,
                uri=uri_for_subject(subject),
                columns=columns,
                labelless=labelless,
            )
        )
        wkt_to_cte[wkt_var] = name
        subject_to_cte.setdefault(subject, name)

    for relation in unsupported_binding_relations(query, bindings.values()):
        notes.append(f"unsupported RDF relation binding: {relation}")

    return ctes, wkt_to_cte, subject_to_cte, props, notes


def cypher_cte_sql(cte: CteSpec) -> str:
    branches: List[str] = []
    signatures = ["uri agtype", "geom geometry"] + [f"{col} agtype" for col in cte.columns]
    match_labels: List[Optional[str]] = [None] if cte.labelless else cte.labels
    for label in match_labels:
        where_parts: List[str] = []
        if cte.uri is not None:
            uri = cypher_string(cte.uri)
            where_parts.append(f"(n.uri = {uri} OR n.yago_uri = {uri})")
        returns = ["n.uri AS uri", "n.geom AS geom"] + [f"n.{col} AS {col}" for col in cte.columns]
        cypher_lines = ["MATCH (n)" if label is None else f"MATCH (n:{label})"]
        if where_parts:
            cypher_lines.append("WHERE " + " AND ".join(where_parts))
        cypher_lines.append("RETURN " + ", ".join(returns))
        cypher = "\n".join(cypher_lines)
        branches.append(
            "    SELECT *\n"
            f"    FROM ag_catalog.cypher({sql_literal(GRAPH_NAME)}, $$\n{cypher}\n$$) "
            f"AS ({', '.join(signatures)})\n"
            "    WHERE geom IS NOT NULL"
        )
    return f"{cte.name} AS (\n" + "\n    UNION ALL\n".join(branches) + "\n)"


def geom_expr(cte_name: str) -> str:
    return f"ST_Transform({cte_name}.geom, 4326)"


def area_expr(cte_name: str) -> str:
    return f"ST_Area({cte_name}.geom)"


def distance_expr(left: str, right: str) -> str:
    return (
        f"ST_DistanceSphere(ST_Centroid({geom_expr(left)}), "
        f"ST_Centroid({geom_expr(right)}))"
    )


def spatial_condition(function_name: str, left: str, right: str) -> Optional[str]:
    key = function_name.lower()
    if key in SPATIAL_SQL:
        return f"{SPATIAL_SQL[key]}({geom_expr(left)}, {geom_expr(right)})"
    if key == "strdf:left":
        return f"ST_XMax({geom_expr(left)}) < ST_XMin({geom_expr(right)})"
    if key == "strdf:right":
        return f"ST_XMin({geom_expr(left)}) > ST_XMax({geom_expr(right)})"
    if key == "strdf:above":
        return f"ST_YMin({geom_expr(left)}) > ST_YMax({geom_expr(right)})"
    if key == "strdf:below":
        return f"ST_YMax({geom_expr(left)}) < ST_YMin({geom_expr(right)})"
    return None


def prop_text_expr(cte: str, column: str) -> str:
    return f"ag_catalog.agtype_to_text({cte}.{column})"


def prop_float_expr(cte: str, column: str) -> str:
    return f"ag_catalog.agtype_to_float8({cte}.{column})"


def uri_text_expr(cte: str) -> str:
    return f"ag_catalog.agtype_to_text({cte}.uri)"


def build_where(
    query: str,
    wkt_to_cte: Dict[str, str],
    subject_to_cte: Dict[str, str],
    props: List[PropertyPattern],
) -> Tuple[List[str], List[str]]:
    conditions: List[str] = []
    notes: List[str] = []

    for match in DISTANCE_FILTER_RE.finditer(query):
        left = wkt_to_cte.get(match.group("a"))
        right = wkt_to_cte.get(match.group("b"))
        if left and right:
            conditions.append(f"{distance_expr(left, right)} {match.group('op')} {match.group('value')}")
        else:
            notes.append(f"unbound distance filter {match.group(0)}")

    for match in SPATIAL_FUNC_RE.finditer(query):
        function = f"{match.group('prefix')}:{match.group('name')}"
        if function.lower() == "geof:distance":
            continue
        left = wkt_to_cte.get(match.group("a"))
        right = wkt_to_cte.get(match.group("b"))
        if left and right:
            condition = spatial_condition(function, left, right)
            if condition:
                conditions.append(condition)
            else:
                notes.append(f"unsupported spatial function {function}")
        else:
            notes.append(f"unbound spatial function {match.group(0)}")

    for pat in props:
        if not pat.object.startswith('"'):
            continue
        cte = subject_to_cte.get(pat.subject)
        if cte:
            conditions.append(f"{prop_text_expr(cte, pat.column)} = {sql_literal(normalize_literal(pat.object))}")

    return conditions, notes


def area_vars(query: str, wkt_to_cte: Dict[str, str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for match in BIND_AREA_RE.finditer(query):
        cte = wkt_to_cte.get(match.group("wkt"))
        if cte:
            result[match.group("var")] = area_expr(cte)
    return result


def prop_var_map(props: List[PropertyPattern], subject_to_cte: Dict[str, str]) -> Dict[str, Tuple[str, str]]:
    result: Dict[str, Tuple[str, str]] = {}
    for pat in props:
        if pat.object.startswith("?") and pat.subject in subject_to_cte:
            result[pat.object] = (subject_to_cte[pat.subject], pat.column)
    return result


def subject_var_map(subject_to_cte: Dict[str, str]) -> Dict[str, str]:
    return {subject: cte for subject, cte in subject_to_cte.items() if subject.startswith("?")}


def sql_expr_for_var(
    var: str,
    subject_vars: Dict[str, str],
    prop_vars: Dict[str, Tuple[str, str]],
    area_var_exprs: Dict[str, str],
    numeric: bool = False,
) -> Optional[str]:
    if var in area_var_exprs:
        return area_var_exprs[var]
    if var in prop_vars:
        cte, col = prop_vars[var]
        return prop_float_expr(cte, col) if numeric else prop_text_expr(cte, col)
    if var in subject_vars:
        return uri_text_expr(subject_vars[var])
    return None


def translate_scalar_expr(
    expr: str,
    subject_vars: Dict[str, str],
    prop_vars: Dict[str, Tuple[str, str]],
    area_var_exprs: Dict[str, str],
    wkt_to_cte: Dict[str, str],
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    translated = expr

    for match in AREA_CALL_RE.finditer(expr):
        wkt = match.group("wkt")
        if wkt in wkt_to_cte:
            translated = translated.replace(match.group(0), area_expr(wkt_to_cte[wkt]))
        else:
            notes.append(f"unbound area expression {match.group(0)}")

    def replace_cast(match: re.Match[str]) -> str:
        var = match.group("var")
        value = sql_expr_for_var(var, subject_vars, prop_vars, area_var_exprs, numeric=True)
        if value is None:
            notes.append(f"unbound numeric variable {var}")
            return "NULL::float8"
        return value

    translated = re.sub(
        r"xsd:(?:float|double|integer)\s*\(\s*(?P<var>\?\w+)\s*\)",
        replace_cast,
        translated,
        flags=re.IGNORECASE,
    )

    for var in sorted(set(re.findall(r"\?\w+", translated)), key=len, reverse=True):
        value = sql_expr_for_var(var, subject_vars, prop_vars, area_var_exprs, numeric=True)
        if value is not None:
            translated = translated.replace(var, value)
        else:
            notes.append(f"unbound scalar variable {var}")

    translated = translated.replace("&&", " AND ")
    return translated, notes


def parse_select_clause(query: str) -> str:
    match = re.search(r"SELECT\s+(?:DISTINCT\s+)?(?P<select>.+?)\s+WHERE\s*\{", query, re.IGNORECASE)
    return match.group("select").strip() if match else ""


def build_projection(
    query: str,
    subject_vars: Dict[str, str],
    prop_vars: Dict[str, Tuple[str, str]],
    area_var_exprs: Dict[str, str],
    wkt_to_cte: Dict[str, str],
) -> Tuple[str, List[str], List[str], List[str]]:
    notes: List[str] = []
    group_exprs: List[str] = []
    order_clauses: List[str] = []
    select_clause = parse_select_clause(query)
    distinct = bool(re.search(r"SELECT\s+DISTINCT", query, re.IGNORECASE))

    def alias_from_as(default: str = "result") -> str:
        match = re.search(r"\bAS\s+(\?\w+)", select_clause, re.IGNORECASE)
        return match.group(1).lstrip("?") if match else default

    if query.upper().startswith("ASK "):
        return "1 AS answer", [], [], notes

    # Aggregate projections.
    count_match = re.search(r"COUNT\s*\(\s*(DISTINCT\s+)?(?P<var>\?\w+)\s*\)", select_clause, re.IGNORECASE)
    if count_match:
        alias = alias_from_as("count")
        expr = sql_expr_for_var(count_match.group("var"), subject_vars, prop_vars, area_var_exprs)
        if expr is None:
            notes.append(f"unbound COUNT variable {count_match.group('var')}")
            expr = "*"
        count_expr = f"COUNT({'DISTINCT ' if count_match.group(1) else ''}{expr})"
        leading_vars = re.findall(r"(?<!AS\s)(\?\w+)", select_clause.split("COUNT", 1)[0], re.IGNORECASE)
        select_parts: List[str] = []
        for var in leading_vars:
            var_expr = sql_expr_for_var(var, subject_vars, prop_vars, area_var_exprs)
            if var_expr:
                select_parts.append(f"{var_expr} AS {var.lstrip('?')}")
                group_exprs.append(var_expr)
        select_parts.append(f"{count_expr} AS {alias}")
        return ", ".join(select_parts), group_exprs, order_clauses, notes

    for aggregate in ("SUM", "AVG", "MAX", "MIN"):
        area_aggregate = re.search(
            rf"{aggregate}\s*\(\s*strdf:area\s*\(\s*(?P<wkt>\?\w+)\s*\)\s*\)",
            select_clause,
            re.IGNORECASE,
        )
        if area_aggregate and area_aggregate.group("wkt") in wkt_to_cte:
            alias = alias_from_as(aggregate.lower())
            return (
                f"{aggregate}({area_expr(wkt_to_cte[area_aggregate.group('wkt')])}) AS {alias}",
                group_exprs,
                order_clauses,
                notes,
            )
        match = re.search(rf"{aggregate}\s*\(\s*(?P<expr>.+?)\s*\)", select_clause, re.IGNORECASE)
        if match:
            alias = alias_from_as(aggregate.lower())
            inner = match.group("expr")
            area_match = AREA_CALL_RE.search(inner)
            if area_match and area_match.group("wkt") in wkt_to_cte:
                expr = area_expr(wkt_to_cte[area_match.group("wkt")])
            else:
                var_match = re.search(r"(?P<var>\?\w+)", inner)
                expr = None
                if var_match:
                    expr = sql_expr_for_var(
                        var_match.group("var"), subject_vars, prop_vars, area_var_exprs, numeric=True
                    )
                if expr is None:
                    notes.append(f"unbound {aggregate} expression {inner}")
                    expr = "NULL::float8"
            return f"{aggregate}({expr}) AS {alias}", group_exprs, order_clauses, notes

    arithmetic_match = re.search(r"\(\s*(?P<expr>.+)\s+AS\s+(?P<alias>\?\w+)\s*\)", select_clause, re.IGNORECASE)
    if arithmetic_match:
        expr, expr_notes = translate_scalar_expr(
            arithmetic_match.group("expr"),
            subject_vars,
            prop_vars,
            area_var_exprs,
            wkt_to_cte,
        )
        notes.extend(expr_notes)
        return f"{expr} AS {arithmetic_match.group('alias').lstrip('?')}", group_exprs, order_clauses, notes

    # Direct area projection.
    area_match = AREA_CALL_RE.search(select_clause)
    if area_match and area_match.group("wkt") in wkt_to_cte:
        alias = alias_from_as("area")
        return f"{area_expr(wkt_to_cte[area_match.group('wkt')])} AS {alias}", group_exprs, order_clauses, notes

    vars_in_select = re.findall(r"\?\w+", select_clause)
    if vars_in_select:
        select_parts = []
        for var in vars_in_select:
            expr = sql_expr_for_var(var, subject_vars, prop_vars, area_var_exprs)
            if expr:
                select_parts.append(f"{expr} AS {var.lstrip('?')}")
            else:
                notes.append(f"unbound SELECT variable {var}")
        if select_parts:
            prefix = "DISTINCT " if distinct else ""
            return prefix + ", ".join(select_parts), group_exprs, order_clauses, notes

    notes.append("could not translate SELECT projection")
    return "*", group_exprs, order_clauses, notes


def build_order_limit(
    query: str,
    subject_vars: Dict[str, str],
    prop_vars: Dict[str, Tuple[str, str]],
    area_var_exprs: Dict[str, str],
    wkt_to_cte: Dict[str, str],
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    clauses: List[str] = []
    order_match = ORDER_BY_RE.search(query)
    if order_match:
        expr = order_match.group("expr").strip()
        direction = ""
        desc_match = re.match(r"DESC\s*\((?P<expr>.+)\)", expr, re.IGNORECASE)
        if desc_match:
            expr = desc_match.group("expr").strip()
            direction = " DESC"
        asc_match = re.match(r"ASC\s*\((?P<expr>.+)\)", expr, re.IGNORECASE)
        if asc_match:
            expr = asc_match.group("expr").strip()
            direction = " ASC"
        dist_match = re.search(
            r"geof:distance\s*\(\s*(?P<a>\?\w+)\s*,\s*(?P<b>\?\w+)(?:\s*,\s*uom:metre)?\s*\)",
            expr,
            re.IGNORECASE,
        )
        area_match = AREA_CALL_RE.search(expr)
        count_match = re.search(r"COUNT\s*\(\s*(?:DISTINCT\s+)?(?P<var>\?\w+)\s*\)", expr, re.IGNORECASE)
        var_match = re.fullmatch(r"\?\w+", expr)
        if dist_match and dist_match.group("a") in wkt_to_cte and dist_match.group("b") in wkt_to_cte:
            clauses.append(
                f"ORDER BY {distance_expr(wkt_to_cte[dist_match.group('a')], wkt_to_cte[dist_match.group('b')])}{direction}"
            )
        elif area_match and area_match.group("wkt") in wkt_to_cte:
            clauses.append(f"ORDER BY {area_expr(wkt_to_cte[area_match.group('wkt')])}{direction}")
        elif count_match:
            expr_var = sql_expr_for_var(count_match.group("var"), subject_vars, prop_vars, area_var_exprs)
            if expr_var:
                clauses.append(f"ORDER BY COUNT({expr_var}){direction}")
        elif var_match:
            expr_var = sql_expr_for_var(expr, subject_vars, prop_vars, area_var_exprs, numeric=True)
            if expr_var:
                clauses.append(f"ORDER BY {expr_var}{direction}")
        else:
            notes.append(f"unsupported ORDER BY expression: {expr}")

    limit_match = LIMIT_RE.search(query)
    if limit_match:
        clauses.append(f"LIMIT {limit_match.group('limit')}")
    return "\n".join(clauses), notes


def build_group_by(
    query: str,
    subject_vars: Dict[str, str],
    prop_vars: Dict[str, Tuple[str, str]],
    area_var_exprs: Dict[str, str],
    projection_group_exprs: List[str],
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    exprs = list(projection_group_exprs)
    match = GROUP_BY_RE.search(query)
    if match:
        for var in re.findall(r"\?\w+", match.group("vars")):
            expr = sql_expr_for_var(var, subject_vars, prop_vars, area_var_exprs)
            if expr and expr not in exprs:
                exprs.append(expr)
    if exprs:
        return "GROUP BY " + ", ".join(exprs), notes
    return "", notes


def translate_question(
    question: Question,
    spatial_labels: List[str],
    loaded_labels: Set[str],
) -> Dict[str, str]:
    query = normalize_query(question.resolved_query)
    ctes, wkt_to_cte, subject_to_cte, props, notes = build_ctes(query, spatial_labels, loaded_labels)
    status = "converted"
    if not ctes:
        status = "unsupported_pattern"
        notes.append("no GeoSPARQL geometry bindings were found")

    where_conditions, where_notes = build_where(query, wkt_to_cte, subject_to_cte, props)
    notes.extend(where_notes)
    if spatial_functions(query) and not where_conditions and "strdf:area" not in query:
        status = "needs_review"
        notes.append("no executable spatial predicate was translated")

    prop_vars = prop_var_map(props, subject_to_cte)
    subject_vars = subject_var_map(subject_to_cte)
    area_var_exprs = area_vars(query, wkt_to_cte)
    projection, projection_groups, _, projection_notes = build_projection(
        query, subject_vars, prop_vars, area_var_exprs, wkt_to_cte
    )
    notes.extend(projection_notes)
    group_by, group_notes = build_group_by(query, subject_vars, prop_vars, area_var_exprs, projection_groups)
    notes.extend(group_notes)
    order_limit, order_notes = build_order_limit(query, subject_vars, prop_vars, area_var_exprs, wkt_to_cte)
    notes.extend(order_notes)

    cte_sql = ",\n".join(cypher_cte_sql(cte) for cte in ctes)
    from_sql = ", ".join(cte.name for cte in ctes) if ctes else ""
    where_sql = "WHERE " + "\n  AND ".join(where_conditions) if where_conditions else ""

    if query.upper().startswith("ASK "):
        inner = "\n".join(part for part in [f"FROM {from_sql}" if from_sql else "", where_sql] if part)
        final_sql = f"SELECT EXISTS (\n    SELECT 1\n    {inner}\n) AS answer"
    else:
        final_parts = [f"SELECT {projection}"]
        if from_sql:
            final_parts.append(f"FROM {from_sql}")
        if where_sql:
            final_parts.append(where_sql)
        if group_by:
            final_parts.append(group_by)
        if order_limit:
            final_parts.append(order_limit)
        final_sql = "\n".join(final_parts)

    sql_wrapper = (
        "LOAD 'age';\n"
        "SET search_path TO ag_catalog, public;\n"
        "WITH\n"
        f"{cte_sql}\n"
        f"{final_sql};"
    )

    if any("unsupported" in note or "unbound" in note for note in notes) and status == "converted":
        status = "needs_review"

    return {
        "id": str(question.qid),
        "resolved_id": str(question.resolved_qid),
        "category": question.category,
        "status": status,
        "spatial_functions": "|".join(spatial_functions(query)),
        "question": question.question,
        "sparql": query,
        "existing_answer": question.answer,
        "age_sql_wrapper": sql_wrapper,
        "geometry_ctes": "|".join(cte.name for cte in ctes),
        "geometry_subjects": "|".join(f"{cte.wkt_var}:{cte.subject}" for cte in ctes),
        "label_policy": "rdf:type labels; untyped geometry subjects use label-less MATCH (n)",
        "notes": "|".join(dict.fromkeys(notes)),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate broad SQL-wrapped AGE Cypher translations for spatial GeoQuestions."
    )
    parser.add_argument("--input", default="benchmark/queries/GeoQuestions1089.json")
    parser.add_argument("--answers", default="benchmark/queries/GeoQuestions1089Answers.json")
    parser.add_argument("--manifest", action="append", required=True)
    parser.add_argument(
        "--output-dir",
        default="benchmark/results/geoquestions/cypher_lpg_broad",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest_paths = [Path(path) for path in args.manifest]
    spatial_labels, loaded_labels, _ = load_spatial_labels(manifest_paths)
    questions = read_questions(Path(args.input), Path(args.answers) if args.answers else None)
    spatial_questions = [q for q in questions if spatial_functions(q.resolved_query)]

    rows = [translate_question(q, spatial_labels, loaded_labels) for q in spatial_questions]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "geoquestions_spatial_age_cypher_broad.csv"
    fields = [
        "id",
        "resolved_id",
        "category",
        "status",
        "spatial_functions",
        "question",
        "sparql",
        "existing_answer",
        "age_sql_wrapper",
        "geometry_ctes",
        "geometry_subjects",
        "label_policy",
        "notes",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary_path = output_dir / "geoquestions_spatial_age_cypher_broad_summary.csv"
    counts = Counter(row["status"] for row in rows)
    category_counts: Dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        category_counts[row["category"]][row["status"]] += 1
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["category", "total", "converted", "needs_review", "unsupported_pattern"])
        writer.writeheader()
        for category in sorted(category_counts):
            counter = category_counts[category]
            writer.writerow(
                {
                    "category": category,
                    "total": sum(counter.values()),
                    "converted": counter.get("converted", 0),
                    "needs_review": counter.get("needs_review", 0),
                    "unsupported_pattern": counter.get("unsupported_pattern", 0),
                }
            )

    print(f"spatial_questions={len(rows)}")
    for status, count in counts.most_common():
        print(f"{status}={count}")
    print(f"spatial_labels={len(spatial_labels)}")
    print(f"wrote={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
