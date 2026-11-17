#!/usr/bin/env python3
"""Prepare a UK-only YAGO2Geo benchmark subset for AGE and RDF engines.

The subset is driven by the UK-only GeoQuestions rows already classified in
geoquestions_age_spatial_full_uk_questions.csv.  It contains:

* all UK administrative vertices from the existing UK AGE load artifacts;
* OSM/GADM labels used by UK-only questions, filtered conservatively by the
  United Kingdom bounding box;
* directly referenced URI vertices from UK-only questions when present.

It writes two synchronized artifacts:

* AGE CSV load files and load_yago2geo_uk_subset.sql;
* RDF N-Triples that preserve the query-visible RDF shape:
  subject rdf:type label; subject geo:hasGeometry geometry; geometry geo:asWKT.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


ROOT = Path(__file__).resolve().parents[2]
PSQL = ROOT / "benchmark/scripts/psql.sh"
YAGO = "http://yago-knowledge.org/resource/"
Y2GEOO = "http://kr.di.uoa.gr/yago2geo/ontology/"
Y2GEOR = "http://kr.di.uoa.gr/yago2geo/resource/"
GEO = "http://www.opengis.net/ont/geosparql#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD = "http://www.w3.org/2001/XMLSchema#"

UK_BBOX = (-13.8, 49.7, 2.0, 61.7)
UK_ADMIN_PREFIXES = ("OS_", "OSNI_")
ALWAYS_UK_LABELS = {"ExternalYAGO"}
PROPERTY_TO_PREDICATE = {
    "name": "hasOSM_Name",
    "population": "hasPopulation",
    "area_sqkm": "hasArea",
    "gadm_description": "hasGADM_Description",
    "gadm_level": "hasGADM_NationalLevel",
    "gadm_upper_level_unit": "hasGADM_UpperLevelUnit",
    "source_id": "hasSourceId",
}

TYPE_RE = re.compile(r"rdf:type\s+y2geoo:([A-Za-z0-9_]+)", re.IGNORECASE)
QNAME_RE = re.compile(r"\b(yago|y2geor):([^\s\{\};,\)\]]+)")
IRI_RE = re.compile(r"<(https?://[^>]+)>")
NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")


def strip_csv_value(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def expand_qname(prefix: str, local: str) -> str:
    local = local.strip().rstrip(" .")
    if prefix == "yago":
        return YAGO + local
    return Y2GEOR + local


def used_labels_and_uris(questions_path: Path) -> Tuple[Set[str], Set[str], List[Dict[str, str]]]:
    rows: List[Dict[str, str]] = []
    labels: Set[str] = set()
    uris: Set[str] = set()
    with questions_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") != "converted" or row.get("uk_only_candidate") != "true":
                continue
            rows.append(row)
            sparql = row["sparql"]
            labels.update(TYPE_RE.findall(sparql))
            uris.update(IRI_RE.findall(sparql))
            for prefix, local in QNAME_RE.findall(sparql):
                uris.add(expand_qname(prefix, local))
    return labels, uris, rows


def is_uk_admin_label(label: str) -> bool:
    return label.startswith(UK_ADMIN_PREFIXES) or label in ALWAYS_UK_LABELS


def label_source(label: str) -> Optional[str]:
    if is_uk_admin_label(label):
        return "uk"
    if label.startswith("OSM_"):
        return "osm"
    if label.startswith("GADM_"):
        return "gadm"
    return None


def source_vertices_dir(source: str) -> Path:
    if source == "uk":
        return ROOT / "benchmark/results/load/yago2geo/uk/vertices"
    if source == "osm":
        return ROOT / "benchmark/results/load/yago2geo/osm/vertices"
    if source == "gadm":
        return ROOT / "benchmark/results/load/yago2geo/gadm/vertices"
    raise ValueError(source)


def parse_wkt_bbox(geom: str) -> Optional[Tuple[float, float, float, float]]:
    geom = strip_csv_value(geom)
    if geom.startswith("SRID="):
        _, _, geom = geom.partition(";")
    values = [float(match.group(0)) for match in NUMBER_RE.finditer(geom)]
    if len(values) < 2:
        return None
    xs = values[0::2]
    ys = values[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_intersects(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def include_row(label: str, row: Dict[str, str], explicit_uris: Set[str]) -> bool:
    uri = strip_csv_value(row.get("uri", ""))
    yago_uri = strip_csv_value(row.get("yago_uri", ""))
    if uri in explicit_uris or yago_uri in explicit_uris:
        return True
    if is_uk_admin_label(label):
        return True
    bbox = parse_wkt_bbox(row.get("geom", ""))
    return bbox is not None and bbox_intersects(bbox, UK_BBOX)


def read_label_rows(
    label: str,
    explicit_uris: Set[str],
    bbox_labels: Set[str],
) -> Tuple[List[str], List[Dict[str, str]]]:
    source = label_source(label)
    if source is None:
        return [], []
    path = source_vertices_dir(source) / f"{label}.csv"
    if not path.exists():
        fieldnames: List[str] = []
    else:
        with path.open(newline="", encoding="utf-8") as handle:
            fieldnames = csv.DictReader(handle).fieldnames or []

    if not fieldnames:
        return [], []

    result: List[Dict[str, str]] = []
    if is_uk_admin_label(label):
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                result.append(row)
        return fieldnames, result

    return fieldnames, db_export_rows(label, explicit_uris, use_bbox=label in bbox_labels)


def write_age_vertices(
    labels: Sequence[str],
    explicit_uris: Set[str],
    out_dir: Path,
    bbox_labels: Set[str],
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, List[str]]]:
    vertices_dir = out_dir / "vertices"
    vertices_dir.mkdir(parents=True, exist_ok=True)
    counts: Dict[str, int] = {}
    original_counts: Dict[str, int] = {}
    fieldnames_by_label: Dict[str, List[str]] = {}
    for label in labels:
        fieldnames, rows = read_label_rows(label, explicit_uris, bbox_labels)
        if not fieldnames:
            continue
        original_counts[label] = len(rows)
        output_rows: List[Dict[str, str]] = []
        seen: Set[str] = set()
        for row in rows:
            uri = strip_csv_value(row.get("uri", ""))
            if uri in seen:
                continue
            seen.add(uri)
            new_row = dict(row)
            new_row["id"] = str(len(output_rows) + 1)
            output_rows.append(new_row)
        if not output_rows:
            continue
        path = vertices_dir / f"{label}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)
        counts[label] = len(output_rows)
        fieldnames_by_label[label] = fieldnames
    return counts, original_counts, fieldnames_by_label


def nq_uri(uri: str) -> str:
    return "<" + uri.replace(" ", "%20") + ">"


def nt_literal(value: str, datatype: Optional[str] = None) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if datatype:
        return f'"{escaped}"^^<{datatype}>'
    return f'"{escaped}"'


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cypher_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def run_psql_copy(sql: str) -> List[Dict[str, str]]:
    proc = subprocess.run(
        [str(PSQL), "-X", "-q", "-P", "footer=off", "-c", sql],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    if not proc.stdout.strip():
        return []
    return list(csv.DictReader(proc.stdout.splitlines()))


def explicit_labels_from_age(explicit_uris: Set[str]) -> Set[str]:
    if not explicit_uris:
        return set()
    values = ", ".join(cypher_string(uri) for uri in sorted(explicit_uris))
    sql = (
        "LOAD 'age'; SET search_path TO ag_catalog, public; "
        "COPY (SELECT label::text AS label FROM ag_catalog.cypher('yago2geo', $$ "
        f"MATCH (n) WHERE n.uri IN [{values}] OR n.yago_uri IN [{values}] "
        "RETURN DISTINCT label(n) AS label "
        "$$) AS (label agtype)) TO STDOUT WITH CSV HEADER"
    )
    labels: Set[str] = set()
    for row in run_psql_copy(sql):
        label = strip_csv_value(row["label"])
        if label:
            labels.add(label)
    return labels


def db_export_rows(label: str, explicit_uris: Set[str], use_bbox: bool) -> List[Dict[str, str]]:
    path = source_vertices_dir(label_source(label) or "") / f"{label}.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            fieldnames = csv.DictReader(handle).fieldnames or []
    else:
        fieldnames = [
            "id",
            "uri",
            "country",
            "source",
            "feature_type",
            "rdf_type",
            "source_id",
            "name",
            "population",
            "area_sqkm",
            "state_name",
            "state_fips",
            "county_fips",
            "place_fips",
            "gnis_id",
            "gnis_name",
            "yago_uri",
            "gadm_description",
            "gadm_level",
            "gadm_upper_level_unit",
            "geom",
        ]

    where_parts: List[str] = []
    if use_bbox:
        where_parts.append("geom && ST_MakeEnvelope(-13.8, 49.7, 2.0, 61.7, 4326)")
    else:
        uri_conditions = []
        for uri in sorted(explicit_uris):
            needle_uri = json.dumps({"uri": uri})[1:-1]
            needle_yago = json.dumps({"yago_uri": uri})[1:-1]
            uri_conditions.append(f"properties::text LIKE {sql_literal('%' + needle_uri + '%')}")
            uri_conditions.append(f"properties::text LIKE {sql_literal('%' + needle_yago + '%')}")
        if uri_conditions:
            where_parts.append("(" + " OR ".join(uri_conditions) + ")")
    if not where_parts:
        return []
    where_sql = " OR ".join(where_parts)
    sql = (
        "COPY ("
        "SELECT properties::text AS properties, ST_AsEWKT(geom) AS geom "
        f"FROM yago2geo.{quote_ident(label)} "
        f"WHERE {where_sql} "
        "ORDER BY id"
        ") TO STDOUT WITH CSV HEADER"
    )
    exported = run_psql_copy(sql)
    rows: List[Dict[str, str]] = []
    for item in exported:
        props = json.loads(item["properties"])
        row = {field: "" for field in fieldnames}
        for field in fieldnames:
            if field == "geom":
                row[field] = item["geom"]
            else:
                value = props.get(field)
                row[field] = "" if value is None else str(value)
        rows.append(row)
    return rows


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def srid_and_wkt(geom: str) -> Tuple[int, str]:
    geom = strip_csv_value(geom)
    if geom.startswith("SRID="):
        srid_text, _, wkt = geom.partition(";")
        return int(srid_text.split("=", 1)[1]), wkt
    return 4326, geom


def rdf_wkt_literal(geom: str) -> str:
    srid, wkt = srid_and_wkt(geom)
    if srid == 4326:
        text = wkt
    else:
        text = f"<http://www.opengis.net/def/crs/EPSG/0/{srid}> {wkt}"
    return nt_literal(text, GEO + "wktLiteral")


def write_rdf(labels: Sequence[str], age_dir: Path, rdf_dir: Path) -> Dict[str, int]:
    rdf_dir.mkdir(parents=True, exist_ok=True)
    triple_counts: Dict[str, int] = {}
    for label in labels:
        csv_path = age_dir / "vertices" / f"{label}.csv"
        if not csv_path.exists():
            continue
        nt_path = rdf_dir / f"{label}.nt"
        triples = 0
        with csv_path.open(newline="", encoding="utf-8") as src, nt_path.open("w", encoding="utf-8") as out:
            reader = csv.DictReader(src)
            for row in reader:
                uri = strip_csv_value(row.get("uri", ""))
                geom = row.get("geom", "")
                if not uri or not geom:
                    continue
                subject = nq_uri(uri)
                geom_uri = nq_uri(f"{uri}#geometry")
                out.write(f"{subject} {nq_uri(RDF_TYPE)} {nq_uri(Y2GEOO + label)} .\n")
                out.write(f"{subject} {nq_uri(GEO + 'hasGeometry')} {geom_uri} .\n")
                out.write(f"{geom_uri} {nq_uri(GEO + 'asWKT')} {rdf_wkt_literal(geom)} .\n")
                triples += 3
                yago_uri = strip_csv_value(row.get("yago_uri", ""))
                if yago_uri and yago_uri != uri:
                    out.write(f"{subject} {nq_uri('http://www.w3.org/2002/07/owl#sameAs')} {nq_uri(yago_uri)} .\n")
                    triples += 1
                for col, pred in PROPERTY_TO_PREDICATE.items():
                    value = strip_csv_value(row.get(col, ""))
                    if not value:
                        continue
                    datatype = XSD + "double" if col in {"population", "area_sqkm", "gadm_level"} else None
                    out.write(f"{subject} {nq_uri(Y2GEOO + pred)} {nt_literal(value, datatype)} .\n")
                    triples += 1
        triple_counts[label] = triples
    return triple_counts


def spatial_spec() -> str:
    return json.dumps(
        {
            "geom": {
                "type": "geometry",
                "geometry_type": "Geometry",
                "srid": 4326,
                "create_if_missing": True,
                "create_index": True,
            }
        },
        sort_keys=True,
    )


def write_age_load_sql(labels: Sequence[str], out_dir: Path, graph_name: str) -> Path:
    rel_base = f"yago2geo_uk_subset"
    sql_path = out_dir / "load_yago2geo_uk_subset.sql"
    spec = spatial_spec().replace("'", "''")
    lines = [
        "LOAD 'age';",
        "SET search_path TO ag_catalog, public;",
        "",
        f"-- Destructive only for this subset graph.",
        f"SELECT ag_catalog.drop_graph('{graph_name}', true) WHERE EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = '{graph_name}');",
        f"DELETE FROM ag_catalog.ag_spatial_columns WHERE graph_name = '{graph_name}';",
        f"SELECT ag_catalog.create_graph('{graph_name}');",
        "",
    ]
    for label in labels:
        if not (out_dir / "vertices" / f"{label}.csv").exists():
            continue
        lines.append(f"SELECT ag_catalog.create_vlabel('{graph_name}', '{label}');")
    lines.append("")
    for label in labels:
        if not (out_dir / "vertices" / f"{label}.csv").exists():
            continue
        lines.append(
            "SELECT ag_catalog.load_labels_with_spatial_from_file("
            f"'{graph_name}', '{label}', '{rel_base}/vertices/{label}.csv', true, true, "
            f"'{spec}'::jsonb);"
        )
    lines.extend(
        [
            "",
            "ANALYZE;",
            "",
            "SELECT label_name, column_name, spatial_type, geometry_type, srid, index_name",
            "FROM ag_catalog.ag_spatial_columns",
            f"WHERE graph_name = '{graph_name}'",
            "ORDER BY label_name, column_name;",
        ]
    )
    sql_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sql_path


def write_manifest(
    out_dir: Path,
    rdf_dir: Path,
    labels: Sequence[str],
    rows: List[Dict[str, str]],
    counts: Dict[str, int],
    rdf_triples: Dict[str, int],
    graph_name: str,
) -> None:
    manifest = {
        "name": "yago2geo_uk_subset",
        "graph_name": graph_name,
        "question_count": len(rows),
        "labels": list(labels),
        "vertex_counts": counts,
        "rdf_triple_counts": rdf_triples,
        "total_vertices": sum(counts.values()),
        "total_rdf_triples": sum(rdf_triples.values()),
        "age_load_sql": str((out_dir / "load_yago2geo_uk_subset.sql").relative_to(ROOT)),
        "rdf_dir": str(rdf_dir.relative_to(ROOT)),
        "notes": [
            "UK administrative labels are copied from existing UK AGE load artifacts.",
            "OSM/GADM labels used by UK-only questions are filtered by conservative UK bbox.",
            "RDF geometry resources are deterministic URI#geometry resources.",
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--questions",
        default="benchmark/results/geoquestions/age_spatial_full/geoquestions_age_spatial_full_uk_only_questions.csv",
    )
    parser.add_argument(
        "--age-output",
        default="benchmark/results/load/yago2geo_uk_subset",
    )
    parser.add_argument(
        "--rdf-output",
        default="benchmark/results/rdfdb/uk_subset_input",
    )
    parser.add_argument("--graph-name", default="yago2geo_uk_subset")
    return parser.parse_args(argv)


def reset_outputs(age_dir: Path, rdf_dir: Path) -> None:
    for path in (age_dir / "vertices", rdf_dir):
        if path.exists():
            shutil.rmtree(path)
    for path in (age_dir / "load_yago2geo_uk_subset.sql", age_dir / "manifest.json"):
        if path.exists():
            path.unlink()
    (age_dir / "vertices").mkdir(parents=True, exist_ok=True)
    rdf_dir.mkdir(parents=True, exist_ok=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    questions_path = ROOT / args.questions
    age_dir = ROOT / args.age_output
    rdf_dir = ROOT / args.rdf_output
    age_dir.mkdir(parents=True, exist_ok=True)
    rdf_dir.mkdir(parents=True, exist_ok=True)
    reset_outputs(age_dir, rdf_dir)

    used_labels, explicit_uris, question_rows = used_labels_and_uris(questions_path)
    required_labels = sorted(label for label in used_labels if label_source(label) is not None)
    required_labels.extend(
        label for label in explicit_labels_from_age(explicit_uris) if label_source(label) is not None
    )
    # Include all UK administrative labels so country/region anchors and common
    # UK labels are available even when only referenced as fixed URIs.
    uk_vertices = source_vertices_dir("uk")
    for path in uk_vertices.glob("*.csv"):
        required_labels.append(path.stem)
    required_labels = sorted(set(required_labels))

    bbox_labels = {label for label in used_labels if label_source(label) in {"osm", "gadm"}}
    counts, original_counts, _fields = write_age_vertices(required_labels, explicit_uris, age_dir, bbox_labels)
    rdf_triples = write_rdf(required_labels, age_dir, rdf_dir)
    write_age_load_sql(required_labels, age_dir, args.graph_name)
    write_manifest(age_dir, rdf_dir, required_labels, question_rows, counts, rdf_triples, args.graph_name)

    print(f"questions={len(question_rows)}")
    print(f"labels={len(required_labels)}")
    print(f"vertices={sum(counts.values())}")
    print(f"rdf_triples={sum(rdf_triples.values())}")
    print(f"wrote_age={age_dir}")
    print(f"wrote_rdf={rdf_dir}")
    for label, count in sorted(counts.items()):
        print(f"{label}: {count}")
    missing = sorted(label for label in required_labels if label not in counts)
    if missing:
        print("missing_labels=" + ",".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
