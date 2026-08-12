#!/usr/bin/env python3
"""Convert a small YAGO2Geo-style RDF file into AGE-Spatial load artifacts.

The tool is intentionally lightweight and dependency-free. It reads N-Triples
or simple line-oriented Turtle triples, extracts RDF entities, literals,
GeoSPARQL WKT geometries, and URI-object predicates, and writes:

* vertices/<Label>.csv
* edges/<EDGE_LABEL>.csv
* load.sql
* manifest.json

The generated vertex CSV files can be loaded with
ag_catalog.load_labels_with_spatial_from_file(), so WKT geometries become
physical PostGIS geometry columns in AGE-Spatial.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional, Sequence, Set, Tuple


RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
GEO_HAS_GEOMETRY = "http://www.opengis.net/ont/geosparql#hasGeometry"
GEO_AS_WKT = "http://www.opengis.net/ont/geosparql#asWKT"
GEO_SF_WITHIN = "http://www.opengis.net/ont/geosparql#sfWithin"
GEO_SF_TOUCHES = "http://www.opengis.net/ont/geosparql#sfTouches"
GEO_SF_CONTAINS = "http://www.opengis.net/ont/geosparql#sfContains"
GEO_SF_INTERSECTS = "http://www.opengis.net/ont/geosparql#sfIntersects"
GEO_SF_CROSSES = "http://www.opengis.net/ont/geosparql#sfCrosses"

EDGE_LABELS = {
    GEO_SF_WITHIN: "WITHIN",
    GEO_SF_TOUCHES: "TOUCHES",
    GEO_SF_CONTAINS: "CONTAINS",
    GEO_SF_INTERSECTS: "INTERSECTS",
    GEO_SF_CROSSES: "CROSSES",
    OWL_SAME_AS: "SAME_AS",
}

RAW_CSV_COLUMNS = {
    "id",
    "start_id",
    "start_vertex_type",
    "end_id",
    "end_vertex_type",
    "geom",
}

PREFIX_RE = re.compile(r"^@prefix\s+([^:]*):\s+<([^>]+)>\s*\.\s*$")
BASE_RE = re.compile(r"^@base\s+<([^>]+)>\s*\.\s*$")
SRID_PREFIX_RE = re.compile(r"^SRID=(\d+);", re.IGNORECASE)
CRS_PREFIX_RE = re.compile(r"^<[^>]*EPSG/0/(\d+)>\s*", re.IGNORECASE)
WKT_RE = re.compile(
    r"^(POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|"
    r"GEOMETRYCOLLECTION)\s*(Z|M|ZM)?\s*\(",
    re.IGNORECASE,
)


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


@dataclass
class Feature:
    uri: str
    rdf_types: Set[str] = field(default_factory=set)
    props: DefaultDict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    geom_uris: List[str] = field(default_factory=list)
    label: str = "Feature"
    id: int = 0


@dataclass(frozen=True)
class Edge:
    label: str
    subject: str
    obj: str
    predicate_uri: str


def safe_name(value: str, default: str = "Feature") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = default
    if cleaned[0].isdigit():
        cleaned = "L_" + cleaned
    return cleaned[:63]


def local_name(uri_or_qname: str) -> str:
    value = uri_or_qname.strip("<>")
    if "#" in value:
        return value.rsplit("#", 1)[1]
    if "/" in value:
        return value.rstrip("/").rsplit("/", 1)[1]
    if ":" in value:
        return value.rsplit(":", 1)[1]
    return value


def strip_uri(token: str) -> str:
    token = token.strip()
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    return token


def expand_qname(token: str, prefixes: Dict[str, str], base: Optional[str]) -> str:
    token = token.strip()
    if token == "a":
        return RDF_TYPE
    if token.startswith("<") and token.endswith(">"):
        value = token[1:-1]
        if base and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            return base + value
        return value
    if ":" in token:
        prefix, name = token.split(":", 1)
        if prefix in prefixes:
            return prefixes[prefix] + name
    return token


def parse_uri_or_qname(line: str, pos: int) -> Tuple[str, int]:
    pos = skip_ws(line, pos)
    if pos >= len(line):
        raise ValueError("unexpected end of line")
    if line[pos] == "<":
        end = line.find(">", pos + 1)
        if end < 0:
            raise ValueError("unterminated URI")
        return line[pos : end + 1], end + 1
    end = pos
    while end < len(line) and not line[end].isspace():
        end += 1
    return line[pos:end], end


def skip_ws(line: str, pos: int) -> int:
    while pos < len(line) and line[pos].isspace():
        pos += 1
    return pos


def split_triple(line: str) -> Optional[Tuple[str, str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("@prefix") or line.startswith("@base"):
        return None
    if not line.endswith("."):
        raise ValueError("triple line does not end with '.'")

    subject, pos = parse_uri_or_qname(line, 0)
    predicate, pos = parse_uri_or_qname(line, pos)
    pos = skip_ws(line, pos)
    obj = line[pos:-1].strip()
    if not obj:
        raise ValueError("missing object")
    return subject, predicate, obj


def unescape_literal(text: str) -> str:
    return bytes(text, "utf-8").decode("unicode_escape")


def parse_literal(token: str) -> str:
    token = token.strip()
    if not token.startswith('"'):
        raise ValueError(f"not a literal: {token}")
    escaped = False
    chars: List[str] = []
    for idx in range(1, len(token)):
        char = token[idx]
        if escaped:
            chars.append("\\" + char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return unescape_literal("".join(chars))
        chars.append(char)
    raise ValueError(f"unterminated literal: {token}")


def object_kind(token: str) -> str:
    token = token.strip()
    if token.startswith("<") or re.match(r"^[A-Za-z_][\w-]*:", token):
        return "uri"
    if token.startswith('"'):
        return "literal"
    if token.startswith("_:"):
        return "bnode"
    return "other"


def iter_rdf_triples(paths: Sequence[Path]) -> Iterable[Tuple[Path, int, str, str, str]]:
    prefixes: Dict[str, str] = {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "owl": "http://www.w3.org/2002/07/owl#",
        "geo": "http://www.opengis.net/ont/geosparql#",
        "yago": "http://yago-knowledge.org/resource/",
        "y2geoo": "http://kr.di.uoa.gr/yago2geo/ontology/",
    }
    base: Optional[str] = None

    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line_no, raw_line in enumerate(handle, 1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                prefix_match = PREFIX_RE.match(line)
                if prefix_match:
                    prefixes[prefix_match.group(1)] = prefix_match.group(2)
                    continue
                base_match = BASE_RE.match(line)
                if base_match:
                    base = base_match.group(1)
                    continue
                try:
                    triple = split_triple(line)
                    if triple is None:
                        continue
                    subject, predicate, obj = triple
                    yield (
                        path,
                        line_no,
                        expand_qname(subject, prefixes, base),
                        expand_qname(predicate, prefixes, base),
                        expand_object(obj, prefixes, base),
                    )
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_no}: {exc}") from exc


def expand_object(token: str, prefixes: Dict[str, str], base: Optional[str]) -> str:
    token = token.strip()
    if token.startswith('"'):
        return token
    if token.startswith("_:"):
        return token
    return expand_qname(token, prefixes, base)


def normalize_wkt_literal(literal: str, fallback_srid: int) -> Tuple[str, int]:
    value = literal.strip()
    srid = fallback_srid

    match = SRID_PREFIX_RE.match(value)
    if match:
        srid = int(match.group(1))
        value = value[match.end() :].strip()

    match = CRS_PREFIX_RE.match(value)
    if match:
        srid = int(match.group(1))
        value = value[match.end() :].strip()

    if not WKT_RE.match(value):
        raise ValueError(f"not a supported WKT literal: {literal[:80]}")
    return value, srid


def get_feature(features: Dict[str, Feature], uri: str) -> Feature:
    if uri not in features:
        features[uri] = Feature(uri=uri)
    return features[uri]


def edge_label(predicate_uri: str) -> str:
    return EDGE_LABELS.get(predicate_uri, safe_name(local_name(predicate_uri).upper(), "EDGE"))


def collect_graph(
    paths: Sequence[Path],
    fallback_srid: int,
    include_same_as: bool,
) -> Tuple[Dict[str, Feature], Dict[str, Tuple[str, int]], Set[Edge]]:
    features: Dict[str, Feature] = {}
    geometries: Dict[str, Tuple[str, int]] = {}
    edges: Set[Edge] = set()

    for _path, _line_no, subject, predicate, obj in iter_rdf_triples(paths):
        kind = object_kind(obj)

        if predicate == GEO_AS_WKT and kind == "literal":
            literal = parse_literal(obj)
            geometries[subject] = normalize_wkt_literal(literal, fallback_srid)
            continue

        if predicate == RDF_TYPE and kind == "uri":
            get_feature(features, subject).rdf_types.add(safe_name(local_name(obj)))
            continue

        if predicate == GEO_HAS_GEOMETRY and kind == "uri":
            get_feature(features, subject).geom_uris.append(strip_uri(obj))
            continue

        if predicate == OWL_SAME_AS and not include_same_as:
            if kind == "uri" and "yago-knowledge.org/resource/" in strip_uri(obj):
                get_feature(features, subject).props["yago_uri"].append(strip_uri(obj))
            continue

        if kind == "uri":
            obj_uri = strip_uri(obj)
            get_feature(features, subject)
            get_feature(features, obj_uri)
            edges.add(Edge(edge_label(predicate), subject, obj_uri, predicate))
            continue

        if kind == "literal":
            value = parse_literal(obj)
            get_feature(features, subject).props[safe_name(local_name(predicate), "property")].append(value)

    return features, geometries, edges


def assign_labels_and_ids(features: Dict[str, Feature]) -> Dict[str, List[Feature]]:
    by_label: Dict[str, List[Feature]] = defaultdict(list)
    for feature in features.values():
        if feature.rdf_types:
            feature.label = sorted(feature.rdf_types)[0]
        else:
            feature.label = "Feature"
        by_label[feature.label].append(feature)

    for label, rows in by_label.items():
        rows.sort(key=lambda item: item.uri)
        for idx, feature in enumerate(rows, 1):
            feature.id = idx
    return dict(sorted(by_label.items()))


def format_csv_value(column: str, value: str) -> str:
    if column in RAW_CSV_COLUMNS or value == "":
        return value
    return json.dumps(value, ensure_ascii=False)


def geometry_value(
    feature: Feature,
    geometries: Dict[str, Tuple[str, int]],
) -> Tuple[str, Optional[int]]:
    for geom_uri in feature.geom_uris:
        if geom_uri in geometries:
            wkt, srid = geometries[geom_uri]
            return f"SRID={srid};{wkt}", srid
    return "", None


def write_vertices(
    output_dir: Path,
    by_label: Dict[str, List[Feature]],
    geometries: Dict[str, Tuple[str, int]],
) -> Tuple[Dict[str, int], Dict[str, int]]:
    counts: Dict[str, int] = {}
    srids_by_label: Dict[str, int] = {}
    vertices_dir = output_dir / "vertices"
    vertices_dir.mkdir(parents=True, exist_ok=True)

    for label, features in by_label.items():
        prop_columns = sorted({prop for feature in features for prop in feature.props})
        has_geometry = False
        seen_srids: Set[int] = set()
        rows: List[Dict[str, str]] = []
        for feature in features:
            geom_value, srid = geometry_value(feature, geometries)
            if srid is not None:
                has_geometry = True
                seen_srids.add(srid)
            row: Dict[str, str] = {
                "id": str(feature.id),
                "uri": feature.uri,
                "rdf_type": "|".join(sorted(feature.rdf_types)),
            }
            for column in prop_columns:
                row[column] = "|".join(feature.props.get(column, []))
            if has_geometry or geom_value:
                row["geom"] = geom_value
            rows.append(row)

        if len(seen_srids) > 1:
            raise ValueError(
                f"label {label} has mixed SRIDs {sorted(seen_srids)}; "
                "split the label or normalize the source geometries first"
            )
        if seen_srids:
            srids_by_label[label] = next(iter(seen_srids))

        fieldnames = ["id", "uri", "rdf_type"] + prop_columns
        if has_geometry:
            fieldnames.append("geom")
        with (vertices_dir / f"{label}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {field: format_csv_value(field, row.get(field, "")) for field in fieldnames}
                )
        counts[label] = len(rows)

    return counts, srids_by_label


def write_edges(
    output_dir: Path,
    features: Dict[str, Feature],
    edges: Set[Edge],
) -> Dict[str, int]:
    edge_rows: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for edge in sorted(edges, key=lambda item: (item.label, item.subject, item.obj)):
        start = features[edge.subject]
        end = features[edge.obj]
        edge_rows[edge.label].append(
            {
                "start_id": str(start.id),
                "start_vertex_type": start.label,
                "end_id": str(end.id),
                "end_vertex_type": end.label,
                "predicate": local_name(edge.predicate_uri),
            }
        )

    counts: Dict[str, int] = {}
    edges_dir = output_dir / "edges"
    edges_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "start_id",
        "start_vertex_type",
        "end_id",
        "end_vertex_type",
        "predicate",
    ]
    for label, rows in sorted(edge_rows.items()):
        with (edges_dir / f"{label}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {field: format_csv_value(field, row.get(field, "")) for field in fieldnames}
                )
        counts[label] = len(rows)
    return counts


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def label_exists_sql(graph_name: str, label: str, kind: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
        f"WHERE g.name = {sql_literal(graph_name)} "
        f"AND l.name = {sql_literal(label)} AND l.kind = {sql_literal(kind)})"
    )


def write_load_sql(
    output_dir: Path,
    graph_name: str,
    vertex_counts: Dict[str, int],
    edge_counts: Dict[str, int],
    spatial_srids: Dict[str, int],
    loader_root: str,
    create_index: bool,
) -> Path:
    lines: List[str] = [
        "LOAD 'age';",
        "SET search_path TO ag_catalog, public;",
        "",
        f"SELECT ag_catalog.create_graph({sql_literal(graph_name)})",
        f"WHERE NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = {sql_literal(graph_name)});",
        "",
    ]

    for label in sorted(vertex_counts):
        q_label = sql_literal(label)
        lines.extend(
            [
                "DO $$",
                "BEGIN",
                f"    IF NOT {label_exists_sql(graph_name, label, 'v')} THEN",
                f"        PERFORM ag_catalog.create_vlabel({sql_literal(graph_name)}, {q_label});",
                "    END IF;",
                "END",
                "$$;",
            ]
        )
    lines.append("")

    for label in sorted(edge_counts):
        q_label = sql_literal(label)
        lines.extend(
            [
                "DO $$",
                "BEGIN",
                f"    IF NOT {label_exists_sql(graph_name, label, 'e')} THEN",
                f"        PERFORM ag_catalog.create_elabel({sql_literal(graph_name)}, {q_label});",
                "    END IF;",
                "END",
                "$$;",
            ]
        )
    lines.append("")

    for label in sorted(vertex_counts):
        path = str(Path(loader_root) / "vertices" / f"{label}.csv")
        if label not in spatial_srids:
            lines.append(
                "SELECT ag_catalog.load_labels_from_file("
                f"{sql_literal(graph_name)}, {sql_literal(label)}, {sql_literal(path)}, true, true);"
            )
            continue

        spatial_spec = json.dumps(
            {
                "geom": {
                    "type": "geometry",
                    "geometry_type": "Geometry",
                    "srid": spatial_srids[label],
                    "create_if_missing": True,
                    "create_index": create_index,
                }
            },
            sort_keys=True,
        )
        lines.append(
            "SELECT ag_catalog.load_labels_with_spatial_from_file("
            f"{sql_literal(graph_name)}, {sql_literal(label)}, {sql_literal(path)}, "
            f"true, true, {sql_literal(spatial_spec)}::jsonb);"
        )
    lines.append("")

    for label in sorted(edge_counts):
        path = str(Path(loader_root) / "edges" / f"{label}.csv")
        lines.append(
            "SELECT ag_catalog.load_edges_from_file("
            f"{sql_literal(graph_name)}, {sql_literal(label)}, {sql_literal(path)}, true);"
        )
    lines.extend(["", "ANALYZE;", ""])

    load_sql = output_dir / "load.sql"
    load_sql.write_text("\n".join(lines), encoding="utf-8")
    return load_sql


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert YAGO2Geo-style RDF into AGE-Spatial CSV load artifacts."
    )
    parser.add_argument(
        "--input",
        nargs="+",
        required=True,
        help="input RDF files in N-Triples or simple line-oriented Turtle format",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="directory where vertices, edges, load.sql, and manifest.json are written",
    )
    parser.add_argument(
        "--graph-name",
        default="yago2geo_sample",
        help="target AGE graph name used in load.sql",
    )
    parser.add_argument(
        "--loader-root",
        default=None,
        help="path to the generated output directory as seen by the PostgreSQL server",
    )
    parser.add_argument(
        "--fallback-srid",
        type=int,
        default=4326,
        help="SRID used when WKT literals do not contain an explicit SRID",
    )
    parser.add_argument(
        "--include-same-as",
        action="store_true",
        help="generate SAME_AS edges instead of storing YAGO sameAs as a property",
    )
    parser.add_argument(
        "--no-create-index",
        action="store_true",
        help="do not create GiST spatial indexes in generated load.sql",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="remove the output directory before writing new artifacts",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_paths = [Path(value).resolve() for value in args.input]
    for path in input_paths:
        if not path.exists():
            raise FileNotFoundError(path)

    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and args.overwrite:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    features, geometries, edges = collect_graph(
        input_paths,
        fallback_srid=args.fallback_srid,
        include_same_as=args.include_same_as,
    )
    by_label = assign_labels_and_ids(features)
    vertex_counts, spatial_srids = write_vertices(output_dir, by_label, geometries)
    edge_counts = write_edges(output_dir, features, edges)
    loader_root = args.loader_root or str(output_dir)
    load_sql = write_load_sql(
        output_dir,
        args.graph_name,
        vertex_counts,
        edge_counts,
        spatial_srids,
        loader_root,
        create_index=not args.no_create_index,
    )

    manifest = {
        "graph_name": args.graph_name,
        "inputs": [str(path) for path in input_paths],
        "output_dir": str(output_dir),
        "loader_root": loader_root,
        "vertex_counts": vertex_counts,
        "edge_counts": edge_counts,
        "spatial_labels": sorted(spatial_srids),
        "spatial_srids": spatial_srids,
        "load_sql": str(load_sql),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"vertices={sum(vertex_counts.values())}")
    print(f"edges={sum(edge_counts.values())}")
    print(f"spatial_labels={len(spatial_srids)}")
    print(f"wrote={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
