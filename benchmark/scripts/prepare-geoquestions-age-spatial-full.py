#!/usr/bin/env python3
"""Translate GeoQuestions spatial-function SPARQL into age-spatial Cypher.

This converter is intentionally conservative.  It only emits AGE queries when
the GeoSPARQL structure can be represented by one Cypher query that keeps the
same logical shape:

* SPARQL rdf:type y2geoo:Label becomes an AGE label constraint.
* Variables without rdf:type stay label-free in AGE.
* geo:hasGeometry/geo:asWKT become physical `n.geom` access inside Cypher.
* geof:/strdf: spatial functions become ST_* calls inside Cypher.

Rows that need a real SPARQL algebra rewrite, such as non-trivial UNION or
FILTER NOT EXISTS, are written as review_needed instead of being approximated.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
GRAPH_NAME = "yago2geo"
YAGO_BASE = "http://yago-knowledge.org/resource/"
Y2GEOR_BASE = "http://kr.di.uoa.gr/yago2geo/resource/"

ENTITY_TOKEN = r"(?:<https?://[^>\s]+>|(?:yago|y2geor):[^\s\{\};,\)\]]+|\?\w+)"
HAS_GEOM_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN})\s+geo:hasGeometry\s+(?P<geom>\?\w+)",
    re.IGNORECASE,
)
HAS_GEOM_SEMI_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN})\s+"
    r"(?:(?:rdf:type|(?:y2geoo|yago):[A-Za-z_][\w-]*)\s+[^.;{}]+)"
    r"(?:\s*;\s*(?:(?!geo:hasGeometry)[^.{};])+)*"
    r"\s*;\s*geo:hasGeometry\s+(?P<geom>\?\w+)",
    re.IGNORECASE,
)
AS_WKT_RE = re.compile(r"(?P<geom>\?\w+)\s+geo:asWKT\s+(?P<wkt>\?\w+)", re.IGNORECASE)
TYPE_RE = re.compile(r"(?P<var>\?\w+)\s+rdf:type\s+y2geoo:(?P<label>[A-Za-z0-9_]+)", re.IGNORECASE)
NON_Y2GEO_TYPE_RE = re.compile(r"\?\w+\s+rdf:type\s+(?!(?:y2geoo):)[A-Za-z_][\w-]*:", re.IGNORECASE)
SPATIAL_FUNC_RE = re.compile(
    r"\b(?P<prefix>geof|strdf):(?P<name>[A-Za-z_][\w-]*)\s*"
    r"\(\s*(?P<a>\?\w+)\s*,\s*(?P<b>\?\w+)"
    r"(?:\s*,\s*uom:metre)?\s*\)",
    re.IGNORECASE,
)
DISTANCE_RE = re.compile(
    r"geof:distance\s*\(\s*(?P<a>\?\w+)\s*,\s*(?P<b>\?\w+)\s*,\s*uom:metre\s*\)"
    r"\s*(?P<op><=|>=|<|>|=)\s*(?P<value>[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
DISTANCE_EXPR_RE = re.compile(
    r"geof:distance\s*\(\s*(?P<a>\?\w+)\s*,\s*(?P<b>\?\w+)"
    r"(?:\s*,\s*uom:metre)?\s*\)",
    re.IGNORECASE,
)
AREA_RE = re.compile(r"strdf:area\s*\(\s*(?P<wkt>\?\w+)\s*\)", re.IGNORECASE)
BIND_AREA_RE = re.compile(
    r"BIND\s*\(\s*strdf:area\s*\(\s*(?P<wkt>\?\w+)\s*\)\s+AS\s+(?P<var>\?\w+)\s*\)",
    re.IGNORECASE,
)
SELECT_RE = re.compile(r"^(?P<form>ASK|SELECT)\s+(?P<select>.*?)(?:\s+WHERE\s*\{)", re.IGNORECASE)
GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\s+(?P<vars>(?:\?\w+\s*)+)", re.IGNORECASE)
HAVING_RE = re.compile(
    r"\bHAVING\s*\(\s*(?P<expr>COUNT\s*\(\s*\?\w+\s*\)|\?\w+)\s*"
    r"(?P<op><=|>=|=|<|>)\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\s*\)",
    re.IGNORECASE,
)
ORDER_BY_RE = re.compile(r"\bORDER\s+BY\s+(?P<expr>.+?)(?:\s+LIMIT\s+\d+|$)", re.IGNORECASE)
LIMIT_RE = re.compile(r"\bLIMIT\s+(?P<limit>\d+)", re.IGNORECASE)
COUNT_RE = re.compile(
    r"COUNT\s*\(\s*(?P<distinct>DISTINCT\s+)?(?P<var>\?\w+)\s*\)\s+AS\s+(?P<alias>\?\w+)",
    re.IGNORECASE,
)
AGG_RE = re.compile(
    r"(?P<fn>SUM|AVG|MAX|MIN)\s*\(\s*(?P<expr>.*?)\s*\)\s+AS\s+(?P<alias>\?\w+)",
    re.IGNORECASE,
)
PROJECT_AREA_RE = re.compile(
    r"strdf:area\s*\(\s*(?P<wkt>\?\w+)\s*\)\s+AS\s+(?P<alias>\?\w+)",
    re.IGNORECASE,
)
PROP_RE = re.compile(
    rf"(?P<subject>{ENTITY_TOKEN})\s+"
    r"(?P<prefix>y2geoo|yago):(?P<predicate>[A-Za-z_][\w-]*)\s+"
    r"(?P<object>\?\w+|<https?://[^>]+>|\"[^\"]*\"(?:@\w+|\^\^[^\s]+)?|[^\s\.;\{\}]+)",
    re.IGNORECASE,
)
FILTER_COMPARE_RE = re.compile(
    r"(?P<left>\?\w+)\s*(?P<op>!=|=|<=|>=|<|>)\s*(?P<right>\?\w+|yago:[^\s\)\]&|]+|[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


PROPERTY_MAP = {
    "hasGAG_Population": "population",
    "GAG_Population": "population",
    "POPULATION": "population",
    "hasPopulation": "population",
    "hasGADM_Description": "gadm_description",
    "hasGADM_NationalLevel": "gadm_level",
    "hasGADM_UpperLevelUnit": "gadm_upper_level_unit",
    "hasGAG_Name": "name",
    "GAG_Name": "name",
    "hasOSI_Name": "name",
    "hasOS_Name": "name",
    "hasOSNI_Name": "name",
    "hasONSI_Name": "name",
    "hasOSM_Name": "name",
    "hasGADM_Name": "name",
    "GNIS_NAME": "name",
}

SPATIAL_FUNC_MAP = {
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
    "geof:sfequals": "ST_Equals",
    "strdf:equals": "ST_Equals",
}


@dataclass
class QuestionRow:
    qid: str
    category: str
    question: str
    sparql: str
    spatial_functions: str
    answer: str


@dataclass
class SubjectSpec:
    token: str
    cypher_var: str
    uri: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    srids: Set[int] = field(default_factory=set)
    needs_geom: bool = False
    columns: Set[str] = field(default_factory=set)
    properties: Dict[str, str] = field(default_factory=dict)


@dataclass
class PropertyPattern:
    subject: str
    column: str
    obj: str


@dataclass
class Conversion:
    status: str
    reason: str = ""
    pattern: str = ""
    cypher: str = ""
    sql_wrapper: str = ""
    signature: str = ""


def normalize_query(query: str) -> str:
    return " ".join(query.split())


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cypher_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def safe_var(name: str, used: Set[str]) -> str:
    value = re.sub(r"\W+", "_", name.strip("?")).lower()
    if not value or value[0].isdigit():
        value = "v_" + value
    base = value
    suffix = 2
    while value in used:
        value = f"{base}_{suffix}"
        suffix += 1
    used.add(value)
    return value


def expand_entity(token: str) -> str:
    token = token.strip()
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    if token.startswith("yago:"):
        return YAGO_BASE + token.split(":", 1)[1]
    if token.startswith("y2geor:"):
        return Y2GEOR_BASE + token.split(":", 1)[1]
    raise ValueError(f"unsupported entity token: {token}")


def literal_value(token: str) -> str:
    token = token.strip()
    if token.startswith('"'):
        match = re.match(r'"(?P<value>(?:[^"\\]|\\.)*)"', token)
        if match:
            return bytes(match.group("value"), "utf-8").decode("unicode_escape")
    return token


def load_label_srids(manifest_paths: Sequence[Path]) -> Tuple[Set[str], Dict[str, int]]:
    labels: Set[str] = set()
    srids: Dict[str, int] = {}
    for path in manifest_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        labels.update(data.get("vertex_counts", {}).keys())
        for label, srid in data.get("loader_srids_by_label", {}).items():
            labels.add(label)
            srids[label] = int(srid)
    return labels, srids


def decode_csv_value(value: str) -> str:
    if value == "":
        return ""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def collect_needed_uris(rows: Sequence[QuestionRow]) -> Set[str]:
    needed: Set[str] = set()
    for row in rows:
        for token in re.findall(r"<https?://[^>\s]+>|(?:yago|y2geor):[^\s\{\};,\)\]]+", row.sparql):
            try:
                needed.add(expand_entity(token))
            except ValueError:
                pass
    return needed


def load_uri_srids(
    vertices_dirs: Sequence[Path],
    needed_uris: Set[str],
    label_srids: Dict[str, int],
) -> Dict[str, Set[int]]:
    uri_srids: Dict[str, Set[int]] = defaultdict(set)
    if not needed_uris:
        return uri_srids

    for vertices_dir in vertices_dirs:
        if not vertices_dir.exists():
            continue
        for path in sorted(vertices_dir.glob("*.csv")):
            label = path.stem
            srid = label_srids.get(label)
            if srid is None:
                continue
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    continue
                has_uri = "uri" in reader.fieldnames
                has_yago_uri = "yago_uri" in reader.fieldnames
                if not has_uri and not has_yago_uri:
                    continue
                for csv_row in reader:
                    if has_uri:
                        uri = decode_csv_value(csv_row.get("uri", ""))
                        if uri in needed_uris:
                            uri_srids[uri].add(srid)
                    if has_yago_uri:
                        yago_uri = decode_csv_value(csv_row.get("yago_uri", ""))
                        if yago_uri in needed_uris:
                            uri_srids[yago_uri].add(srid)
        if set(uri_srids) >= needed_uris:
            break
    return uri_srids


def label_alias(label: str, loaded_labels: Set[str]) -> Optional[str]:
    if label in loaded_labels:
        return label
    lower = label.lower()
    for candidate in loaded_labels:
        if candidate.lower() == lower:
            return candidate
    return None


def simple_type_union_only(query: str) -> bool:
    if "UNION" not in query.upper():
        return True
    reduced = re.sub(
        r"\{\s*\?\w+\s+rdf:type\s+y2geoo:[A-Za-z0-9_]+\s*\}",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    reduced = re.sub(r"\bUNION\b", " ", reduced, flags=re.IGNORECASE)
    return "UNION" not in reduced.upper()


def geometry_bindings(query: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    geom_subject: Dict[str, str] = {}
    for regex in (HAS_GEOM_RE, HAS_GEOM_SEMI_RE):
        for match in regex.finditer(query):
            geom_subject[match.group("geom")] = match.group("subject")
    wkt_subject: Dict[str, str] = {}
    for match in AS_WKT_RE.finditer(query):
        subject = geom_subject.get(match.group("geom"))
        if subject:
            wkt_subject[match.group("wkt")] = subject
    return geom_subject, wkt_subject


def type_bindings(query: str, loaded_labels: Set[str]) -> Tuple[Dict[str, List[str]], List[str]]:
    labels_by_var: Dict[str, Set[str]] = defaultdict(set)
    missing: List[str] = []
    for match in TYPE_RE.finditer(query):
        label = label_alias(match.group("label"), loaded_labels)
        if label:
            labels_by_var[match.group("var")].add(label)
        else:
            missing.append(match.group("label"))
    return {var: sorted(labels) for var, labels in labels_by_var.items()}, sorted(set(missing))


def property_patterns(query: str) -> Tuple[List[PropertyPattern], List[str]]:
    patterns: List[PropertyPattern] = []
    unsupported: List[str] = []
    seen: Set[Tuple[str, str, str]] = set()

    def add_pattern(subject: str, prefix: str, predicate: str, obj: str) -> None:
        column = PROPERTY_MAP.get(predicate)
        key = (subject, predicate, obj)
        if key in seen:
            return
        seen.add(key)
        if column is None:
            if prefix.lower() == "yago":
                unsupported.append(f"yago:{predicate}")
            return
        patterns.append(PropertyPattern(subject=subject, column=column, obj=obj))

    for match in PROP_RE.finditer(query):
        add_pattern(match.group("subject"), match.group("prefix"), match.group("predicate"), match.group("object"))

    statement_re = re.compile(
        rf"(?P<subject>{ENTITY_TOKEN})\s+(?P<body>[^.{{}};]+(?:\s*;\s*[^.{{}};]+)+)",
        re.IGNORECASE,
    )
    predicate_re = re.compile(
        r"^(?P<prefix>y2geoo|yago):(?P<predicate>[A-Za-z_][\w-]*)\s+"
        r"(?P<object>\?\w+|<https?://[^>]+>|\"[^\"]*\"(?:@\w+|\^\^[^\s]+)?|[^\s\.;\{\}]+)",
        re.IGNORECASE,
    )
    for stmt in statement_re.finditer(query):
        subject = stmt.group("subject")
        for part in stmt.group("body").split(";"):
            match = predicate_re.match(part.strip())
            if match:
                add_pattern(subject, match.group("prefix"), match.group("predicate"), match.group("object"))

    return patterns, sorted(set(unsupported))


def build_subjects(
    query: str,
    loaded_labels: Set[str],
    uri_srids: Dict[str, Set[int]],
) -> Tuple[Dict[str, SubjectSpec], Dict[str, str], Dict[str, str], Dict[str, List[str]], List[str]]:
    geom_subject, wkt_subject = geometry_bindings(query)
    types, missing_labels = type_bindings(query, loaded_labels)
    props, unsupported_props = property_patterns(query)
    notes = [f"missing label {label}" for label in missing_labels]
    notes += [f"unsupported base predicate {predicate}" for predicate in unsupported_props]

    used: Set[str] = set()
    subjects: Dict[str, SubjectSpec] = {}

    def ensure_subject(token: str) -> SubjectSpec:
        if token in subjects:
            return subjects[token]
        if token.startswith("?"):
            cypher_var = safe_var(token, used)
            labels = types.get(token, [])
            spec = SubjectSpec(token=token, cypher_var=cypher_var, labels=labels)
        else:
            uri = expand_entity(token)
            local = uri.rsplit("/", 1)[-1]
            cypher_var = safe_var(local[:40] or "entity", used)
            spec = SubjectSpec(token=token, cypher_var=cypher_var, uri=uri, srids=set(uri_srids.get(uri, set())))
        subjects[token] = spec
        return spec

    for subject in wkt_subject.values():
        ensure_subject(subject).needs_geom = True
    for pat in props:
        spec = ensure_subject(pat.subject)
        spec.columns.add(pat.column)
        if pat.obj.startswith("?"):
            spec.properties[pat.obj] = pat.column

    arg_subject = dict(geom_subject)
    arg_subject.update(wkt_subject)
    return subjects, wkt_subject, arg_subject, types, notes


def subject_srids(subject: str, subjects: Dict[str, SubjectSpec], label_srids: Dict[str, int]) -> Set[int]:
    spec = subjects[subject]
    srids = {label_srids[label] for label in spec.labels if label in label_srids}
    srids.update(spec.srids)
    if not srids and spec.uri:
        # URI constants absent from the four-country unified CSV are typically
        # OSM/GADM resources in this benchmark, both loaded as WGS84.  GAG URI
        # constants are in the unified CSV and keep explicit 2100 above.
        srids.add(4326)
    if not srids and {"gadm_description", "gadm_level", "gadm_upper_level_unit"} & spec.columns:
        srids.add(4326)
    return srids


def needs_common_transform(left: str, right: str, subjects: Dict[str, SubjectSpec], label_srids: Dict[str, int]) -> bool:
    left_srids = subject_srids(left, subjects, label_srids)
    right_srids = subject_srids(right, subjects, label_srids)
    if not left_srids or not right_srids:
        return True
    if left_srids and right_srids and len(left_srids) == 1 and left_srids == right_srids:
        return False
    if left_srids == {4326} and right_srids == {4326}:
        return False
    if left_srids == {2100} and right_srids == {2100}:
        return False
    return bool(2100 in left_srids or 2100 in right_srids)


def geom_expr(
    subject: str,
    subjects: Dict[str, SubjectSpec],
    label_srids: Dict[str, int],
    transform: bool = False,
) -> str:
    base = f"{subjects[subject].cypher_var}.geom"
    return f"ST_Transform({base}, 4326)" if transform else base


def area_expr(wkt: str, wkt_subject: Dict[str, str], subjects: Dict[str, SubjectSpec]) -> Optional[str]:
    subject = wkt_subject.get(wkt)
    if not subject:
        return None
    return f"ST_Area({subjects[subject].cypher_var}.geom)"


def numeric_expr_for_var(var: str, subjects: Dict[str, SubjectSpec], area_vars: Dict[str, str]) -> Optional[str]:
    if var in area_vars:
        return area_vars[var]
    for spec in subjects.values():
        if var in spec.properties:
            return f"toFloat({spec.cypher_var}.{spec.properties[var]})"
    return None


def value_expr_for_var(var: str, subjects: Dict[str, SubjectSpec], area_vars: Dict[str, str]) -> Optional[str]:
    if var in area_vars:
        return area_vars[var]
    token = var if var.startswith("?") else f"?{var}"
    if token in subjects:
        return f"{subjects[token].cypher_var}.uri"
    for spec in subjects.values():
        if token in spec.properties:
            return f"{spec.cypher_var}.{spec.properties[token]}"
    return None


def spatial_conditions(
    query: str,
    wkt_subject: Dict[str, str],
    subjects: Dict[str, SubjectSpec],
    label_srids: Dict[str, int],
) -> Tuple[List[str], List[str]]:
    conditions: List[str] = []
    notes: List[str] = []

    for match in DISTANCE_RE.finditer(query):
        left = wkt_subject.get(match.group("a"))
        right = wkt_subject.get(match.group("b"))
        if not left or not right:
            notes.append(f"unbound distance {match.group(0)}")
            continue
        left_geom = geom_expr(
            left,
            subjects,
            label_srids,
            transform=subject_srids(left, subjects, label_srids) != {4326},
        )
        right_geom = geom_expr(
            right,
            subjects,
            label_srids,
            transform=subject_srids(right, subjects, label_srids) != {4326},
        )
        conditions.append(
            "ST_DistanceSphere("
            f"ST_Centroid({left_geom}), ST_Centroid({right_geom})"
            f") {match.group('op')} {match.group('value')}"
        )

    for match in SPATIAL_FUNC_RE.finditer(query):
        function = f"{match.group('prefix')}:{match.group('name')}"
        if function.lower() == "geof:distance":
            continue
        left = wkt_subject.get(match.group("a"))
        right = wkt_subject.get(match.group("b"))
        if not left or not right:
            notes.append(f"unbound spatial function {match.group(0)}")
            continue
        transform = needs_common_transform(left, right, subjects, label_srids)
        left_geom = geom_expr(left, subjects, label_srids, transform)
        right_geom = geom_expr(right, subjects, label_srids, transform)
        key = function.lower()
        if key in SPATIAL_FUNC_MAP:
            conditions.append(f"{SPATIAL_FUNC_MAP[key]}({left_geom}, {right_geom})")
        elif key == "strdf:left":
            conditions.append(f"ST_XMax({left_geom}) < ST_XMin({right_geom})")
        elif key == "strdf:right":
            conditions.append(f"ST_XMin({left_geom}) > ST_XMax({right_geom})")
        elif key == "strdf:above":
            conditions.append(f"ST_YMin({left_geom}) > ST_YMax({right_geom})")
        elif key == "strdf:below":
            conditions.append(f"ST_YMax({left_geom}) < ST_YMin({right_geom})")
        else:
            notes.append(f"unsupported spatial function {function}")

    return conditions, notes


def property_conditions(query: str, subjects: Dict[str, SubjectSpec]) -> List[str]:
    conditions: List[str] = []
    props, _ = property_patterns(query)
    for pat in props:
        spec = subjects.get(pat.subject)
        if not spec or pat.obj.startswith("?"):
            continue
        if pat.obj.startswith('"'):
            conditions.append(f"{spec.cypher_var}.{pat.column} = {cypher_string(literal_value(pat.obj))}")
        elif re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", pat.obj):
            conditions.append(f"toFloat({spec.cypher_var}.{pat.column}) = {pat.obj}")
    return conditions


def comparison_conditions(query: str, subjects: Dict[str, SubjectSpec], area_vars: Dict[str, str]) -> List[str]:
    conditions: List[str] = []
    for match in re.finditer(
        r"xsd:(?:double|float|integer|decimal)\s*\(\s*(?P<var>\?\w+)\s*\)\s*"
        r"(?P<op><=|>=|=|<|>)\s*(?P<value>[0-9]+(?:\.[0-9]+)?)",
        query,
        re.IGNORECASE,
    ):
        left = numeric_expr_for_var(match.group("var"), subjects, area_vars)
        if left:
            conditions.append(f"{left} {match.group('op')} {match.group('value')}")

    for match in FILTER_COMPARE_RE.finditer(query):
        left_token = match.group("left")
        right_token = match.group("right")
        op = "<>" if match.group("op") == "!=" else match.group("op")
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", right_token) and op in {"<", ">", "<=", ">=", "="}:
            left = numeric_expr_for_var(left_token, subjects, area_vars) or value_expr_for_var(left_token, subjects, area_vars)
        else:
            left = value_expr_for_var(left_token, subjects, area_vars) or numeric_expr_for_var(left_token, subjects, area_vars)
        if not left:
            continue
        if right_token.startswith("?"):
            right = value_expr_for_var(right_token, subjects, area_vars) or numeric_expr_for_var(right_token, subjects, area_vars)
            if right:
                conditions.append(f"{left} {op} {right}")
        elif right_token.startswith("yago:"):
            conditions.append(f"{left} {op} {cypher_string(expand_entity(right_token))}")
        else:
            conditions.append(f"{left} {op} {right_token}")
    return conditions


def bind_area_vars(query: str, wkt_subject: Dict[str, str], subjects: Dict[str, SubjectSpec]) -> Dict[str, str]:
    area_vars: Dict[str, str] = {}
    for match in BIND_AREA_RE.finditer(query):
        expr = area_expr(match.group("wkt"), wkt_subject, subjects)
        if expr:
            area_vars[match.group("var")] = expr
    return area_vars


def replace_numeric_expression(
    expr: str,
    wkt_subject: Dict[str, str],
    subjects: Dict[str, SubjectSpec],
    area_vars: Dict[str, str],
) -> str:
    result = expr

    def replace_area(match: re.Match[str]) -> str:
        return area_expr(match.group("wkt"), wkt_subject, subjects) or match.group(0)

    result = AREA_RE.sub(replace_area, result)

    def replace_cast(match: re.Match[str]) -> str:
        var = match.group("var")
        return numeric_expr_for_var(var, subjects, area_vars) or match.group(0)

    result = re.sub(r"xsd:(?:double|float|integer|decimal)\s*\(\s*(?P<var>\?\w+)\s*\)", replace_cast, result)

    def replace_var(match: re.Match[str]) -> str:
        var = match.group(0)
        return numeric_expr_for_var(var, subjects, area_vars) or f"__UNBOUND_{var.lstrip('?')}__"

    result = re.sub(r"\?\w+", replace_var, result)
    return result


def distance_expr(
    left_arg: str,
    right_arg: str,
    arg_subject: Dict[str, str],
    subjects: Dict[str, SubjectSpec],
    label_srids: Dict[str, int],
) -> Optional[str]:
    left = arg_subject.get(left_arg)
    right = arg_subject.get(right_arg)
    if not left or not right:
        return None
    left_geom = geom_expr(
        left,
        subjects,
        label_srids,
        transform=subject_srids(left, subjects, label_srids) != {4326},
    )
    right_geom = geom_expr(
        right,
        subjects,
        label_srids,
        transform=subject_srids(right, subjects, label_srids) != {4326},
    )
    return f"ST_DistanceSphere(ST_Centroid({left_geom}), ST_Centroid({right_geom}))"


def replace_distance_expression(
    expr: str,
    arg_subject: Dict[str, str],
    subjects: Dict[str, SubjectSpec],
    label_srids: Dict[str, int],
) -> str:
    def repl(match: re.Match[str]) -> str:
        value = distance_expr(match.group("a"), match.group("b"), arg_subject, subjects, label_srids)
        return value or match.group(0)

    return DISTANCE_EXPR_RE.sub(repl, expr)


def parse_select_clause(query: str) -> Tuple[str, str]:
    match = SELECT_RE.search(query)
    if not match:
        return ("ASK", "") if query.upper().startswith("ASK") else ("SELECT", "")
    return match.group("form").upper(), match.group("select").strip()


def selected_vars(select_clause: str) -> List[str]:
    return re.findall(r"(?<!AS\s)(\?\w+)(?!\s*\))", select_clause, flags=re.IGNORECASE)


def projection_groups(select_clause: str) -> List[str]:
    groups: List[str] = []
    depth = 0
    start: Optional[int] = None
    for index, char in enumerate(select_clause):
        if char == "(":
            if depth == 0:
                start = index
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(select_clause[start + 1 : index].strip())
                start = None
    return groups


def strip_outer_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        for index, char in enumerate(expr):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(expr) - 1:
                    balanced = False
                    break
        if not balanced:
            break
        expr = expr[1:-1].strip()
    return expr


def trim_unbalanced_trailing_parens(expr: str) -> str:
    balance = 0
    for char in expr:
        if char == "(":
            balance += 1
        elif char == ")":
            balance -= 1

    while balance < 0 and expr.rstrip().endswith(")"):
        expr = expr.rstrip()[:-1].rstrip()
        balance += 1
    return expr


def normalize_age_numeric_aggregates(expr: str) -> str:
    result: List[str] = []
    index = 0

    for match in re.finditer(r"\b(?P<fn>sum|avg|max|min)\s*\(", expr, re.IGNORECASE):
        open_index = match.end() - 1
        if open_index < index:
            continue

        depth = 0
        close_index = None
        for cursor in range(open_index, len(expr)):
            char = expr[cursor]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    close_index = cursor
                    break

        if close_index is None:
            continue

        arg = expr[open_index + 1 : close_index].strip()
        result.append(expr[index : match.start()])
        if re.match(r"ST_(?:Area|Length|Distance|DistanceSphere)\s*\(", arg, re.IGNORECASE):
            result.append(f"{match.group('fn')}(toFloat({arg}))")
        else:
            result.append(expr[match.start() : close_index + 1])
        index = close_index + 1

    if not result:
        return expr

    result.append(expr[index:])
    return "".join(result)


def output_alias(alias: str) -> str:
    return alias.lstrip("?")


def build_projection(
    query: str,
    subjects: Dict[str, SubjectSpec],
    wkt_subject: Dict[str, str],
    arg_subject: Dict[str, str],
    area_vars: Dict[str, str],
    label_srids: Dict[str, int],
) -> Tuple[List[str], List[str], List[str], List[str]]:
    form, select_clause = parse_select_clause(query)
    if form == "ASK":
        return ["count(*) > 0 AS answer"], [], ["answer agtype"], []

    return_exprs: List[str] = []
    projected_aliases: List[str] = []
    notes: List[str] = []

    for match in COUNT_RE.finditer(select_clause):
        var = match.group("var")
        expr = value_expr_for_var(var, subjects, area_vars)
        if expr is None:
            notes.append(f"unbound count variable {var}")
            continue
        distinct = "DISTINCT " if match.group("distinct") else ""
        alias = output_alias(match.group("alias"))
        return_exprs.append(f"count({distinct}{expr}) AS {alias}")
        projected_aliases.append(alias)

    for match in AGG_RE.finditer(select_clause):
        fn = match.group("fn").lower()
        expr = replace_numeric_expression(match.group("expr"), wkt_subject, subjects, area_vars)
        alias = output_alias(match.group("alias"))
        return_exprs.append(f"{normalize_age_numeric_aggregates(f'{fn}({expr})')} AS {alias}")
        projected_aliases.append(alias)

    for match in PROJECT_AREA_RE.finditer(select_clause):
        expr = area_expr(match.group("wkt"), wkt_subject, subjects)
        if not expr:
            notes.append(f"unbound area projection {match.group('wkt')}")
            continue
        alias = output_alias(match.group("alias"))
        if alias not in projected_aliases:
            return_exprs.append(f"{expr} AS {alias}")
            projected_aliases.append(alias)

    if not return_exprs and "strdf:area" in select_clause:
        area_match = AREA_RE.search(select_clause)
        if area_match:
            expr = area_expr(area_match.group("wkt"), wkt_subject, subjects)
            alias_match = re.search(r"AS\s+(?P<alias>\?\w+)", select_clause, re.IGNORECASE)
            alias = output_alias(alias_match.group("alias")) if alias_match else "area"
            if expr:
                return_exprs.append(f"{expr} AS {alias}")
                projected_aliases.append(alias)

    for group in projection_groups(select_clause):
        match = re.match(r"(?P<expr>.*)\s+AS\s+(?P<alias>\?\w+)\s*$", group, re.IGNORECASE)
        if not match:
            continue
        alias = output_alias(match.group("alias"))
        if alias in projected_aliases:
            continue
        expr = strip_outer_parens(match.group("expr"))
        expr = replace_distance_expression(expr, arg_subject, subjects, label_srids)
        expr = replace_numeric_expression(expr, wkt_subject, subjects, area_vars)
        expr = trim_unbalanced_trailing_parens(expr)
        expr = normalize_age_numeric_aggregates(expr)
        if "__UNBOUND_" in expr:
            notes.append(f"unbound projected expression for {alias}: {expr}")
            continue
        return_exprs.append(f"{expr} AS {alias}")
        projected_aliases.append(alias)

    selected_insert_index = 0
    for var in selected_vars(select_clause):
        alias = output_alias(var)
        if alias in projected_aliases:
            continue
        expr = value_expr_for_var(var, subjects, area_vars)
        if expr:
            return_exprs.insert(selected_insert_index, f"{expr} AS {alias}")
            projected_aliases.insert(selected_insert_index, alias)
            selected_insert_index += 1

    if not return_exprs:
        notes.append("could not build SELECT projection")

    signatures = [f"{alias} agtype" for alias in projected_aliases]
    return return_exprs, projected_aliases, signatures, notes


def order_clause(
    query: str,
    wkt_subject: Dict[str, str],
    arg_subject: Dict[str, str],
    subjects: Dict[str, SubjectSpec],
    area_vars: Dict[str, str],
    label_srids: Dict[str, int],
    projected_aliases: Sequence[str],
) -> Tuple[List[str], List[str]]:
    match = ORDER_BY_RE.search(query)
    if not match:
        return [], []
    expr = match.group("expr").strip()

    count_order = re.match(
        r"(?P<dir>DESC|ASC)?\s*\(?\s*COUNT\s*\(\s*(?P<var>\?\w+)\s*\)\s*\)?",
        expr,
        re.IGNORECASE,
    )
    if count_order:
        value = value_expr_for_var(count_order.group("var"), subjects, area_vars)
        direction = " DESC" if (count_order.group("dir") or "").upper() == "DESC" else " ASC"
        if GROUP_BY_RE.search(query):
            return [f"ORDER BY order_count{direction}"], []
        if value:
            return [f"ORDER BY count({value}){direction}"], []

    direction = ""
    desc = re.match(r"DESC\s*\(\s*(?P<body>.*?)\s*\)", expr, re.IGNORECASE)
    asc = re.match(r"ASC\s*\(\s*(?P<body>.*?)\s*\)", expr, re.IGNORECASE)
    if desc:
        expr = desc.group("body")
        direction = " DESC"
    elif asc:
        expr = asc.group("body")
        direction = " ASC"
    else:
        direction = " ASC"

    if expr.startswith("?") and expr.lstrip("?") in projected_aliases:
        return [f"ORDER BY {expr.lstrip('?')}{direction}"], []
    if expr.startswith("?"):
        value = value_expr_for_var(expr, subjects, area_vars)
        if value:
            return [f"ORDER BY {value}{direction}"], []
    if expr.upper().startswith("COUNT"):
        count_var = re.search(r"COUNT\s*\(\s*(\?\w+)\s*\)", expr, re.IGNORECASE)
        if count_var:
            value = value_expr_for_var(count_var.group(1), subjects, area_vars)
            if value:
                return [f"ORDER BY count({value}){direction}"], []
    expr = replace_distance_expression(expr, arg_subject, subjects, label_srids)
    expr = replace_numeric_expression(expr, wkt_subject, subjects, area_vars)
    return [f"ORDER BY {expr}{direction}"], []


def uri_condition(var: str, uri: str, uri_mode: str) -> str:
    value = cypher_string(uri)
    if uri_mode == "compat":
        return f"({var}.uri = {value} OR {var}.yago_uri = {value})"
    return f"{var}.uri = {value}"


def build_match_lines(subjects: Dict[str, SubjectSpec], uri_mode: str) -> List[str]:
    lines: List[str] = []
    carried: List[str] = []
    for spec in subjects.values():
        if not spec.needs_geom and not spec.properties:
            continue
        if len(spec.labels) == 1:
            lines.append(f"MATCH ({spec.cypher_var}:{spec.labels[0]})")
        else:
            lines.append(f"MATCH ({spec.cypher_var})")
        where_parts: List[str] = []
        if len(spec.labels) > 1:
            labels = ", ".join(cypher_string(label) for label in spec.labels)
            where_parts.append(f"label({spec.cypher_var}) IN [{labels}]")
        if spec.uri:
            where_parts.append(uri_condition(spec.cypher_var, spec.uri, uri_mode))
        if where_parts:
            lines.append("WHERE " + "\n  AND ".join(where_parts))
        carried.append(spec.cypher_var)
        lines.append("WITH " + ", ".join(carried))
    if lines and lines[-1].startswith("WITH "):
        lines.pop()
    return lines


def apply_group_having(
    query: str,
    return_exprs: List[str],
    subjects: Dict[str, SubjectSpec],
    area_vars: Dict[str, str],
) -> Tuple[List[str], List[str], List[str]]:
    group_match = GROUP_BY_RE.search(query)
    having_match = HAVING_RE.search(query)
    if not group_match and not having_match:
        return [], return_exprs, []

    group_vars = group_match.group("vars").split() if group_match else []
    group_cypher: List[str] = []
    notes: List[str] = []
    with_parts: List[str] = []
    order_count_match = re.search(
        r"ORDER\s+BY\s+(?:DESC|ASC)?\s*\(?\s*COUNT\s*\(\s*(?P<var>\?\w+)\s*\)\s*\)?",
        query,
        re.IGNORECASE,
    )

    for var in group_vars:
        token = var
        if token in subjects:
            with_parts.append(subjects[token].cypher_var)
        else:
            expr = value_expr_for_var(token, subjects, area_vars)
            if expr:
                with_parts.append(f"{expr} AS {output_alias(token)}")

    if having_match:
        expr = having_match.group("expr")
        alias = "having_count"
        if expr.upper().startswith("COUNT"):
            var = re.search(r"\?\w+", expr).group(0)
            count_expr = value_expr_for_var(var, subjects, area_vars)
            if not count_expr:
                notes.append(f"unbound HAVING count variable {var}")
            else:
                with_parts.append(f"count({count_expr}) AS {alias}")
                group_cypher.append("WITH " + ", ".join(with_parts))
                group_cypher.append(f"WHERE {alias} {having_match.group('op')} {having_match.group('value')}")
        else:
            var_expr = numeric_expr_for_var(expr, subjects, area_vars) or output_alias(expr)
            group_cypher.append("WITH " + ", ".join(with_parts + [f"{var_expr} AS {alias}"]))
            group_cypher.append(f"WHERE {alias} {having_match.group('op')} {having_match.group('value')}")
    elif order_count_match and group_match:
        count_expr = value_expr_for_var(order_count_match.group("var"), subjects, area_vars)
        if count_expr:
            with_parts.append(f"count({count_expr}) AS order_count")
            group_cypher.append("WITH " + ", ".join(with_parts))
        else:
            notes.append(f"unbound ORDER BY count variable {order_count_match.group('var')}")

    return group_cypher, return_exprs, notes


def cypher_wrapper(cypher: str, signature: str) -> str:
    return (
        "LOAD 'age';\n"
        "SET search_path TO ag_catalog, public;\n"
        f"SELECT * FROM ag_catalog.cypher({sql_literal(GRAPH_NAME)}, $$\n"
        f"{cypher}\n"
        f"$$) AS ({signature});"
    )


def classify_pattern(query: str) -> str:
    if query.upper().startswith("ASK"):
        return "ask"
    if "FILTER NOT EXISTS" in query.upper():
        return "not_exists"
    if re.search(r"\bGROUP\s+BY\b", query, re.IGNORECASE):
        return "group"
    if re.search(r"\bORDER\s+BY\b", query, re.IGNORECASE):
        return "rank"
    if re.search(r"\bCOUNT\s*\(", query, re.IGNORECASE):
        return "count"
    if "strdf:area" in query:
        return "area"
    return "select"


def manual_review_pattern_conversion(row: QuestionRow, uri_mode: str) -> Optional[Conversion]:
    """Emit reviewed conversions for patterns tested outside the generic parser.

    These keep the SPARQL shape intact:
    * FILTER NOT EXISTS becomes OPTIONAL MATCH + count(candidate) = 0.
    * Nested area percentage becomes sum(toFloat(ST_Area(...))) / ST_Area(...).
    """

    def wrapped(cypher: str, signature: str, pattern: str, reason: str = "reviewed pattern") -> Conversion:
        return Conversion("converted", reason, pattern, cypher, cypher_wrapper(cypher, signature), signature)

    greece = uri_condition("greece", YAGO_BASE + "Greece", uri_mode)
    england = uri_condition("england", YAGO_BASE + "England", uri_mode)
    us = uri_condition("us", YAGO_BASE + "United_States", uri_mode)
    united_kingdom = uri_condition("united_kingdom", YAGO_BASE + "United_Kingdom", uri_mode)
    wales = uri_condition("wales", YAGO_BASE + "Wales", uri_mode)
    greater_london = uri_condition("greater_london", YAGO_BASE + "Greater_London", uri_mode)
    norfolk = uri_condition("norfolk", YAGO_BASE + "Norfolk", uri_mode)
    scotland = uri_condition("scotland", YAGO_BASE + "Scotland", uri_mode)
    liverpool = uri_condition("liverpool", YAGO_BASE + "Liverpool", uri_mode)
    belfast_city = uri_condition(
        "belfast",
        YAGO_BASE + "geoentity_City_of_Belfast_3333223",
        uri_mode,
    )
    thrace = uri_condition("target", YAGO_BASE + "geoentity_Eastern_Macedonia_and_Thrace_6697803", uri_mode)

    manual: Dict[str, Tuple[str, str, str]] = {
        "143": (
            f"""MATCH (united_kingdom)
WHERE {united_kingdom}
WITH united_kingdom
MATCH (forest:OSM_forest)
WITH united_kingdom, forest
WHERE ST_Within(forest.geom, united_kingdom.geom)
WITH forest, ST_Area(united_kingdom.geom) AS ukArea
WITH forest, ukArea, sum(toFloat(ST_Area(forest.geom))) / ukArea AS woodland
RETURN woodland AS woodland""",
            "woodland agtype",
            "group",
        ),
        "576": (
            f"""MATCH (county:OS_County)
MATCH (wales)
WHERE {wales}
WITH county, wales
MATCH (village:OSM_village)
WHERE ST_Within(county.geom, wales.geom)
  AND ST_Within(village.geom, county.geom)
WITH county, count(village.uri) AS villages
ORDER BY villages ASC
LIMIT 1
RETURN county.uri AS county""",
            "county agtype",
            "group",
        ),
        "759": (
            f"""MATCH (forest:OSM_forest)
MATCH (belfast)
WHERE {belfast_city}
WITH forest, ST_DistanceSphere(ST_Centroid(forest.geom), ST_Centroid(belfast.geom)) AS distance
ORDER BY distance ASC
LIMIT 1
RETURN forest.uri AS forest""",
            "forest agtype",
            "rank",
        ),
        "776": (
            f"""MATCH (greater_london)
WHERE {greater_london}
WITH greater_london
MATCH (district:OS_District)
WHERE ST_Touches(district.geom, greater_london.geom)
WITH district, ST_Area(district.geom) AS area
ORDER BY area DESC
LIMIT 1
RETURN district.uri AS district""",
            "district agtype",
            "rank",
        ),
        "796": (
            f"""MATCH (norfolk)
WHERE {norfolk}
WITH norfolk
MATCH (l:OSM_forest)
WHERE ST_Within(l.geom, norfolk.geom)
WITH DISTINCT l, ST_Area(l.geom) AS area
ORDER BY area DESC
LIMIT 1
RETURN l.uri AS l""",
            "l agtype",
            "rank",
        ),
        "817": (
            f"""MATCH (city:OSM_city)
MATCH (united_kingdom)
WHERE {united_kingdom}
WITH city, united_kingdom
MATCH (liverpool)
WHERE {liverpool}
WITH city, united_kingdom, liverpool
WHERE ST_Contains(united_kingdom.geom, city.geom)
WITH liverpool, max(toFloat(ST_Area(city.geom))) AS max_city_area
RETURN toFloat(ST_Area(liverpool.geom)) > max_city_area AS answer""",
            "answer agtype",
            "ask",
        ),
        "840": (
            f"""MATCH (greater_london)
WHERE {greater_london}
WITH greater_london
MATCH (district:OS_District)
WHERE ST_Touches(district.geom, greater_london.geom)
WITH district, ST_Area(district.geom) AS area
ORDER BY area ASC
LIMIT 1
RETURN district.uri AS district""",
            "district agtype",
            "rank",
        ),
        "853": (
            f"""MATCH (united_kingdom)
WHERE {united_kingdom}
WITH united_kingdom
MATCH (island:OSM_island)
WHERE ST_Within(island.geom, united_kingdom.geom)
WITH island, ST_Area(island.geom) AS area
ORDER BY area DESC
LIMIT 1
RETURN island.uri AS island""",
            "island agtype",
            "rank",
        ),
        "856": (
            f"""MATCH (scotland)
WHERE {scotland}
WITH scotland
MATCH (canal:OSM_canal)
WHERE ST_Within(canal.geom, scotland.geom)
WITH canal, ST_Area(canal.geom) AS area
ORDER BY area DESC
LIMIT 3
RETURN canal.uri AS canal""",
            "canal agtype",
            "rank",
        ),
        "857": (
            f"""MATCH (england)
WHERE {england}
WITH england
MATCH (canal:OSM_canal)
WHERE ST_Within(canal.geom, england.geom)
WITH canal, ST_Area(canal.geom) AS area
ORDER BY area DESC
LIMIT 1
RETURN canal.uri AS canal""",
            "canal agtype",
            "rank",
        ),
        "878": (
            """MATCH (county:OS_County)
MATCH (lake:OSM_lake)
WHERE ST_Within(lake.geom, county.geom)
WITH county, count(lake.uri) AS order_count
ORDER BY order_count DESC
LIMIT 1
RETURN county.uri AS county""",
            "county agtype",
            "group",
        ),
        "882": (
            """MATCH (m:OS_County)
MATCH (l:OSM_forest)
WHERE ST_Within(l.geom, m.geom)
WITH m, count(l.uri) AS forest_count
ORDER BY forest_count DESC
LIMIT 10
RETURN m.uri AS m, forest_count AS count""",
            "m agtype, count agtype",
            "group",
        ),
        "450": (
            """MATCH (island:OSM_island)
MATCH (forest:OSM_forest)
WHERE ST_Within(forest.geom, island.geom)
OPTIONAL MATCH (lake:OSM_lake)
WHERE ST_Within(lake.geom, island.geom)
WITH island, count(lake) AS lake_count
WHERE lake_count = 0
RETURN DISTINCT island.uri AS island""",
            "island agtype",
            "not_exists",
        ),
        "451": (
            """MATCH (county)
WHERE label(county) IN ['OS_County', 'OSI_County_Council', 'OSI_City_and_County_Council']
OPTIONAL MATCH (forest:OSM_forest)
WHERE ST_Contains(county.geom, forest.geom)
WITH county, count(forest) AS forest_count
WHERE forest_count = 0
RETURN DISTINCT county.uri AS county""",
            "county agtype",
            "not_exists",
        ),
        "452": (
            f"""MATCH (us)
WHERE {us}
WITH us
MATCH (state:GADM_2ndOrder_AdministrativeUnit)
WHERE ST_Within(state.geom, us.geom)
OPTIONAL MATCH (lake:OSM_lake)
WHERE ST_Within(lake.geom, state.geom)
WITH state, count(lake) AS lake_count
WHERE lake_count = 0
RETURN state.uri AS state""",
            "state agtype",
            "not_exists",
        ),
        "453": (
            f"""MATCH (us)
WHERE {us}
WITH us
MATCH (state:GADM_2ndOrder_AdministrativeUnit)
WHERE ST_Within(state.geom, us.geom)
OPTIONAL MATCH (lake:OSM_lake)
WHERE ST_Within(lake.geom, state.geom)
WITH state, count(lake) AS lake_count
WHERE lake_count = 0
RETURN state.uri AS state""",
            "state agtype",
            "not_exists",
        ),
        "588": (
            f"""MATCH (greece)
WHERE {greece}
WITH greece
MATCH (region:GAG_Region)
WHERE ST_Within(ST_Transform(region.geom, 4326), greece.geom)
OPTIONAL MATCH (lake:OSM_lake)
WHERE ST_Contains(ST_Transform(region.geom, 4326), lake.geom)
WITH region, count(lake) AS lake_count
WHERE lake_count = 0
RETURN DISTINCT region.uri AS region""",
            "region agtype",
            "not_exists",
        ),
        "589": (
            f"""MATCH (greece)
WHERE {greece}
WITH greece
MATCH (mun:GAG_Municipality)
WHERE ST_Within(ST_Transform(mun.geom, 4326), greece.geom)
OPTIONAL MATCH (park:OSM_park)
WHERE ST_Contains(ST_Transform(mun.geom, 4326), park.geom)
WITH mun, count(park) AS park_count
WHERE park_count = 0
RETURN DISTINCT mun.uri AS mun""",
            "mun agtype",
            "not_exists",
        ),
        "590": (
            """MATCH (county)
WHERE label(county) IN ['OSI_County_Council', 'OSI_City_and_County_Council']
OPTIONAL MATCH (forest:OSM_forest)
WHERE ST_Contains(county.geom, forest.geom)
WITH county, count(forest) AS forest_count
WHERE forest_count = 0
RETURN DISTINCT county.uri AS county""",
            "county agtype",
            "not_exists",
        ),
        "753": (
            """MATCH (island:OSM_island)
OPTIONAL MATCH (town:OSM_town)
WHERE ST_Within(town.geom, island.geom)
WITH island, count(town) AS town_count
WHERE town_count = 0
OPTIONAL MATCH (city:OSM_city)
WHERE ST_Within(city.geom, island.geom)
WITH island, count(city) AS city_count
WHERE city_count = 0
OPTIONAL MATCH (village:OSM_village)
WHERE ST_Within(village.geom, island.geom)
WITH island, count(village) AS village_count
WHERE village_count = 0
RETURN count(island) AS islands""",
            "islands agtype",
            "not_exists",
        ),
        "755": (
            f"""MATCH (greece)
WHERE {greece}
WITH greece, ST_Area(greece.geom) AS greece_area
MATCH (lake:OSM_lake)
WHERE ST_Within(lake.geom, greece.geom)
WITH sum(toFloat(ST_Area(lake.geom))) AS lake_area_sum, greece_area
RETURN ((lake_area_sum / greece_area) * 100) AS percentage""",
            "percentage agtype",
            "area_percentage",
        ),
        "756": (
            f"""MATCH (target)
WHERE {thrace}
WITH target, ST_Area(target.geom) AS target_area
MATCH (lake:OSM_lake)
WHERE ST_Within(lake.geom, target.geom)
WITH sum(toFloat(ST_Area(lake.geom))) AS lake_area_sum, target_area
RETURN ((lake_area_sum / target_area) * 100) AS percentage""",
            "percentage agtype",
            "area_percentage",
        ),
        "872": (
            f"""MATCH (england)
WHERE {england}
WITH england
MATCH (town:OSM_town)
WITH england, town
MATCH (westernTown:OSM_town)
WHERE ST_Within(town.geom, england.geom)
  AND ST_Within(westernTown.geom, england.geom)
  AND NOT (
    westernTown.uri <> town.uri
    AND ST_XMin(westernTown.geom) > ST_XMax(town.geom)
  )
RETURN DISTINCT town.uri AS town""",
            "town agtype",
            "not_exists",
        ),
    }

    if row.qid not in manual:
        return None
    cypher, signature, pattern = manual[row.qid]
    return wrapped(cypher, signature, pattern)


def manual_exclusion_reason(row: QuestionRow) -> Optional[Conversion]:
    reasons = {
        "276": (
            "unbound/mismatched original SPARQL variable ?wktDublin; original query binds ?dublinWKT",
            "ask",
        ),
        "475": (
            "direct geometry resource y2geor:Geometry_gadm_GBR.1.16_1 is not represented as an LPG vertex in the current physical-geom schema",
            "ask",
        ),
        "584": (
            "spatial function uses geometry-node variable ?mg instead of WKT variable ?mw",
            "select",
        ),
        "612": (
            "base YAGO rdf:type/yago population predicate plus nested anti-join; outside YAGO2Geo-only LPG mapping",
            "not_exists",
        ),
        "748": (
            "non-y2geoo rdf:type and nested UNION inside FILTER NOT EXISTS; outside the tested anti-join pattern",
            "not_exists",
        ),
        "791": (
            "original SPARQL projects unbound ?geoWKT while binding ?geoWKT2; malformed area projection",
            "rank",
        ),
        "792": (
            "original SPARQL projects unbound ?geoWKT while binding ?geoWKT2; malformed area projection",
            "rank",
        ),
        "824": (
            "original SPARQL SELECTs unbound ?island while ranking municipalities by population",
            "rank",
        ),
        "870": (
            "base YAGO population predicate plus extreme-value anti-join; outside YAGO2Geo-only LPG mapping",
            "ask",
        ),
    }
    if row.qid not in reasons:
        return None
    reason, pattern = reasons[row.qid]
    return Conversion("review_needed", reason, pattern)


def convert(
    row: QuestionRow,
    loaded_labels: Set[str],
    label_srids: Dict[str, int],
    uri_srids: Dict[str, Set[int]],
    uri_mode: str,
) -> Conversion:
    query = normalize_query(row.sparql)
    review_reasons: List[str] = []

    manual = manual_review_pattern_conversion(row, uri_mode)
    if manual is not None:
        return manual

    exclusion = manual_exclusion_reason(row)
    if exclusion is not None:
        return exclusion

    if "FILTER NOT EXISTS" in query.upper():
        return Conversion("review_needed", "FILTER NOT EXISTS needs anti-join rewrite", classify_pattern(query))
    if not simple_type_union_only(query):
        return Conversion("review_needed", "non-trivial UNION needs branch-preserving rewrite", classify_pattern(query))
    if NON_Y2GEO_TYPE_RE.search(query):
        return Conversion("review_needed", "non-y2geoo rdf:type appears in SPARQL", classify_pattern(query))

    subjects, wkt_subject, arg_subject, _types, subject_notes = build_subjects(query, loaded_labels, uri_srids)
    review_reasons.extend(subject_notes)
    if not wkt_subject:
        return Conversion("unsupported", "no geo:hasGeometry/geo:asWKT binding found", classify_pattern(query))
    if any(reason.startswith("missing label") for reason in review_reasons):
        return Conversion("review_needed", "; ".join(review_reasons), classify_pattern(query))
    if any(reason.startswith("unsupported base predicate") for reason in review_reasons):
        return Conversion("review_needed", "; ".join(review_reasons), classify_pattern(query))

    area_vars = bind_area_vars(query, wkt_subject, subjects)
    conditions, spatial_notes = spatial_conditions(query, arg_subject, subjects, label_srids)
    conditions.extend(property_conditions(query, subjects))
    conditions.extend(comparison_conditions(query, subjects, area_vars))
    review_reasons.extend(spatial_notes)
    if spatial_notes:
        return Conversion("review_needed", "; ".join(review_reasons), classify_pattern(query))

    return_exprs, projected_aliases, signatures, projection_notes = build_projection(
        query, subjects, wkt_subject, arg_subject, area_vars, label_srids
    )
    review_reasons.extend(projection_notes)
    if projection_notes:
        return Conversion("review_needed", "; ".join(review_reasons), classify_pattern(query))

    lines = build_match_lines(subjects, uri_mode)
    if conditions:
        if lines:
            carried = ", ".join(spec.cypher_var for spec in subjects.values() if spec.needs_geom or spec.properties)
            lines.append(f"WITH {carried}")
        lines.append("WHERE " + "\n  AND ".join(dict.fromkeys(conditions)))

    group_lines, return_exprs, group_notes = apply_group_having(query, return_exprs, subjects, area_vars)
    review_reasons.extend(group_notes)
    lines.extend(group_lines)

    distinct = "DISTINCT " if re.search(r"SELECT\s+DISTINCT", query, re.IGNORECASE) and not group_lines else ""
    lines.append("RETURN " + distinct + ", ".join(return_exprs))

    order_lines, order_notes = order_clause(
        query, wkt_subject, arg_subject, subjects, area_vars, label_srids, projected_aliases
    )
    review_reasons.extend(order_notes)
    lines.extend(order_lines)

    limit_match = LIMIT_RE.search(query)
    if limit_match:
        lines.append(f"LIMIT {limit_match.group('limit')}")

    signature = ", ".join(signatures) if signatures else "result agtype"
    cypher = "\n".join(lines)
    return Conversion(
        "converted",
        "; ".join(reason for reason in review_reasons if reason),
        classify_pattern(query),
        cypher,
        cypher_wrapper(cypher, signature),
        signature,
    )


def read_questions(path: Path) -> List[QuestionRow]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            QuestionRow(
                qid=row["id"],
                category=row["category"],
                question=row["question"],
                sparql=row["sparql"],
                spatial_functions=row["spatial_functions"],
                answer=row.get("answer", ""),
            )
            for row in csv.DictReader(handle)
        ]


def invalid_risk_by_id(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    result: Dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            result[row["id"]] = (
                f"{row.get('invalid_geometry_count', '')} invalid geometries via "
                f"{row.get('invalid_match_types', '')} on {row.get('invalid_labels', '')}"
            )
    return result


FIELDS = [
    "id",
    "category",
    "question",
    "sparql",
    "spatial_functions",
    "status",
    "review_reason",
    "pattern",
    "age_cypher",
    "age_sql_wrapper",
    "return_signature",
    "answer",
    "invalid_geometry_risk",
]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="benchmark/results/geoquestions/invalid_geometry/geoquestions_clean_spatial_function_764.csv",
    )
    parser.add_argument(
        "--invalid-risk",
        default="benchmark/results/geoquestions/invalid_geometry/geoquestions_with_invalid_geometry_risk.csv",
    )
    parser.add_argument(
        "--manifest",
        action="append",
        default=[
            "benchmark/results/load/yago2geo/unified/manifest.json",
            "benchmark/results/load/yago2geo/osm/manifest.json",
            "benchmark/results/load/yago2geo/gadm/manifest.json",
            "benchmark/results/load/yago2geo/greece/manifest.json",
            "benchmark/results/load/yago2geo/ireland/manifest.json",
            "benchmark/results/load/yago2geo/uk/manifest.json",
            "benchmark/results/load/yago2geo/usa/manifest.json",
        ],
        help="YAGO2Geo AGE load manifest; may be repeated",
    )
    parser.add_argument(
        "--vertices-dir",
        action="append",
        default=[
            "benchmark/results/load/yago2geo/unified/vertices",
        ],
        help="AGE load vertices directory for URI-to-SRID inference; may be repeated",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark/results/geoquestions/age_spatial_full",
    )
    parser.add_argument(
        "--uri-mode",
        choices=["compat", "strict"],
        default="compat",
        help="compat matches RDF subjects against uri or yago_uri; strict uses uri only",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_path = ROOT / args.input
    output_dir = ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = read_questions(input_path)
    loaded_labels, label_srids = load_label_srids([ROOT / path for path in args.manifest])
    uri_srids = load_uri_srids(
        [ROOT / path for path in args.vertices_dir],
        collect_needed_uris(rows),
        label_srids,
    )
    risks = invalid_risk_by_id(ROOT / args.invalid_risk)

    output_rows: List[Dict[str, str]] = []
    for row in rows:
        conv = convert(row, loaded_labels, label_srids, uri_srids, args.uri_mode)
        output_rows.append(
            {
                "id": row.qid,
                "category": row.category,
                "question": row.question,
                "sparql": row.sparql,
                "spatial_functions": row.spatial_functions,
                "status": conv.status,
                "review_reason": conv.reason,
                "pattern": conv.pattern,
                "age_cypher": conv.cypher,
                "age_sql_wrapper": conv.sql_wrapper,
                "return_signature": conv.signature,
                "answer": row.answer,
                "invalid_geometry_risk": risks.get(row.qid, ""),
            }
        )

    queries_path = output_dir / "geoquestions_age_spatial_full_queries.csv"
    with queries_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(output_rows)

    review_path = output_dir / "geoquestions_age_spatial_full_review_needed.csv"
    with review_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(row for row in output_rows if row["status"] != "converted")

    pattern_path = output_dir / "geoquestions_age_spatial_full_pattern_summary.csv"
    counts = Counter((row["category"], row["pattern"], row["spatial_functions"], row["status"]) for row in output_rows)
    with pattern_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["category", "pattern", "spatial_functions", "status", "count"])
        writer.writeheader()
        for (category, pattern, functions, status), count in sorted(counts.items()):
            writer.writerow(
                {
                    "category": category,
                    "pattern": pattern,
                    "spatial_functions": functions,
                    "status": status,
                    "count": count,
                }
            )

    status_counts = Counter(row["status"] for row in output_rows)
    print(f"wrote={queries_path}")
    print(f"wrote={review_path}")
    print(f"wrote={pattern_path}")
    print("rows=" + str(len(output_rows)))
    for status, count in sorted(status_counts.items()):
        print(f"{status}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
