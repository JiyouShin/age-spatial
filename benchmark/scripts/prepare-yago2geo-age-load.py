#!/usr/bin/env python3
"""Prepare YAGO2Geo RDF dumps for Apache AGE + age_spatial loading.

The output is intentionally split into:

* vertices/*.csv: consumed by ag_catalog.load_labels_with_spatial_from_file()
* edges/*.csv: consumed by ag_catalog.load_edges_from_file()
* load_<country>.sql: database-side load script.

The generated vertex CSV files include a geom column. The spatial CSV loader
stores that column in a physical PostGIS geometry column and keeps it out of
agtype properties.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_SCRIPT = SCRIPT_DIR / "extract-rdf-schema.py"
spec = importlib.util.spec_from_file_location("extract_rdf_schema", SCHEMA_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load RDF parser from {SCHEMA_SCRIPT}")
schema = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = schema
spec.loader.exec_module(schema)

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
GEOSPARQL_SF_TOUCHES = "http://www.opengis.net/ont/geosparql#sfTouches"
GEOSPARQL_SF_WITHIN = "http://www.opengis.net/ont/geosparql#sfWithin"
GAG_BELONGS_TO = "http://kr.di.uoa.gr/yago2geo/ontology/GAG_BelongsTo"

EDGE_PREDICATES = {
    GEOSPARQL_SF_TOUCHES: "TOUCHES",
    GEOSPARQL_SF_WITHIN: "WITHIN",
    GAG_BELONGS_TO: "BELONGS_TO",
    OWL_SAME_AS: "SAME_AS",
}

VERTEX_BASE_COLUMNS = [
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
]

VERTEX_COLUMNS = VERTEX_BASE_COLUMNS + ["geom"]

EDGE_COLUMNS = [
    "start_id",
    "start_vertex_type",
    "end_id",
    "end_vertex_type",
    "predicate",
    "country",
    "source",
]

RAW_LOAD_COLUMNS = {
    "id",
    "start_id",
    "start_vertex_type",
    "end_id",
    "end_vertex_type",
    "geom",
}

WKT_CACHE_COLUMNS = [
    "geometry_uri",
    "wkt",
    "source_srid",
    "geometry_type",
]

WKT_START_RE = re.compile(
    r"^(POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|"
    r"GEOMETRYCOLLECTION)\s*(Z|M|ZM)?\s*\(",
    re.IGNORECASE,
)
SRID_PREFIX_RE = re.compile(r"^SRID=(\d+);", re.IGNORECASE)
CRS_PREFIX_RE = re.compile(r"^<[^>]*EPSG/0/(\d+)>\s*", re.IGNORECASE)
CRS_SUFFIX_RE = re.compile(
    r";\s*https?://[^ \t\r\n\"]*EPSG/0/(\d+)\s*$", re.IGNORECASE
)


@dataclass
class CountryConfig:
    name: str
    source: str
    inputs: List[str]
    default_srid: int


@dataclass
class Feature:
    uri: str
    rdf_types: Set[str] = field(default_factory=set)
    file_labels: Set[str] = field(default_factory=set)
    props: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    geom_uri: Optional[str] = None
    yago_uri: Optional[str] = None
    label: Optional[str] = None
    id: Optional[int] = None


@dataclass(frozen=True)
class Edge:
    label: str
    subject: str
    obj: str
    predicate: str


COUNTRIES = {
    "usa": CountryConfig(
        name="usa",
        source="US",
        default_srid=4326,
        inputs=[
            "benchmark/data/extracted/yago2geo/usa/extended_counties_geom.nt",
            "benchmark/data/extracted/yago2geo/usa/extended_incorps_updated_geom.nt",
            "benchmark/data/extracted/yago2geo/usa/extended_mcds_updated_geom.nt",
            "benchmark/data/extracted/yago2geo/usa/extended_pp_states_updated_geom.nt",
            "benchmark/data/extracted/yago2geo/usa/extended_pp_unincorps_updated_geom.nt",
        ],
    ),
    "ireland": CountryConfig(
        name="ireland",
        source="OSI",
        default_srid=4326,
        inputs=[
            "benchmark/data/extracted/yago2geo/ireland/OSI_extended.ttl",
            "benchmark/data/extracted/yago2geo/ireland/OSI_new.ttl",
            "benchmark/data/extracted/yago2geo/ireland/OSI_matches.nt",
            "benchmark/data/extracted/yago2geo/ireland/OSI_topological.nt",
        ],
    ),
    "uk": CountryConfig(
        name="uk",
        source="OS_OSNI",
        default_srid=4326,
        inputs=[
            "benchmark/data/extracted/yago2geo/uk/os/OS_extended.ttl",
            "benchmark/data/extracted/yago2geo/uk/os/OS_new.ttl",
            "benchmark/data/extracted/yago2geo/uk/osni/OSNI_extended.ttl",
            "benchmark/data/extracted/yago2geo/uk/osni/OSNI_new.ttl",
            "benchmark/data/extracted/yago2geo/uk/os/OS_matches.nt",
            "benchmark/data/extracted/yago2geo/uk/osni/OSNI_matches.nt",
            "benchmark/data/extracted/yago2geo/uk/os/OS_topological.nt",
            "benchmark/data/extracted/yago2geo/uk/osni/OSNI_topological.nt",
        ],
    ),
    "greece": CountryConfig(
        name="greece",
        source="GAG",
        default_srid=2100,
        inputs=[
            "benchmark/data/extracted/yago2geo/greece/GAG_extended.ttl",
            "benchmark/data/extracted/yago2geo/greece/GAG_new.ttl",
            "benchmark/data/extracted/yago2geo/greece/GAG_matches.nt",
            "benchmark/data/extracted/yago2geo/greece/GAG_topological.nt",
        ],
    ),
}


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def safe_label(value: str) -> str:
    value = value.replace("External/YAGO", "ExternalYAGO")
    value = value.replace("GeoHive/", "GeoHive_")
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "Feature"
    if cleaned[0].isdigit():
        cleaned = "L_" + cleaned
    return cleaned[:63]


def first(values: Iterable[str]) -> str:
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def values_for(feature: Feature, names: Sequence[str]) -> List[str]:
    result: List[str] = []
    for name in names:
        result.extend(feature.props.get(name, []))
    return result


def parse_literal_value(obj: str) -> str:
    if obj.startswith('"'):
        value, _datatype, _lang = schema.parse_literal(obj)
        return value
    return obj


def choose_label(feature: Feature) -> str:
    type_names = sorted(
        name
        for name in feature.rdf_types
        if name not in {"Feature", "Geometry", "SpatialObject"}
    )
    if type_names:
        return safe_label(type_names[0])

    if feature.file_labels:
        preferred = sorted(
            label
            for label in feature.file_labels
            if "Topological" not in label
            and "Matches" not in label
            and label not in {"GAGNew", "GAGExtended", "OSNew", "OSExtended", "OSNINew", "OSNIExtended", "OSINew", "OSIExtended"}
        )
        if preferred:
            return safe_label(preferred[0])

    pattern_group = schema.uri_pattern_group(f"<{feature.uri}>")
    if pattern_group:
        return safe_label(pattern_group)

    if feature.file_labels:
        return safe_label(sorted(feature.file_labels)[0])

    return "Feature"


def source_for_label(config: CountryConfig, label: str) -> str:
    if label.startswith("OSNI_"):
        return "OSNI"
    if label.startswith("OS_"):
        return "OS"
    return config.source


def normalize_number(value: str) -> str:
    value = value.strip()
    if value.lower() in {"nan", "null", "none"}:
        return ""
    return value


def format_agtype_csv_value(column: str, value: str) -> str:
    if value == "" or column in RAW_LOAD_COLUMNS:
        return value

    try:
        json.loads(value)
    except json.JSONDecodeError:
        return json.dumps(value, ensure_ascii=False)

    return value


def vertex_row(config: CountryConfig, feature: Feature) -> Dict[str, str]:
    rdf_type = "|".join(sorted(feature.rdf_types))
    source_id = first(
        values_for(
            feature,
            [
                "hasGAG_ID",
                "GAG_ID",
                "hasOSI_ID",
                "hasOS_ID",
                "hasOSNI_ID",
                "GNIS_ID",
                "GLOBALID",
            ],
        )
    )
    name = first(
        values_for(
            feature,
            [
                "hasGAG_Name",
                "GAG_Name",
                "hasOSI_Name",
                "hasOS_Name",
                "hasOSNI_Name",
                "GNIS_NAME",
                "hasONSI_Name",
            ],
        )
    )
    population = normalize_number(
        first(values_for(feature, ["hasGAG_Population", "GAG_Population", "POPULATION"]))
    )
    area_sqkm = normalize_number(
        first(values_for(feature, ["AREASQKM", "hasOSNI_AreaSqKM"]))
    )

    return {
        "id": str(feature.id),
        "uri": feature.uri,
        "country": config.name,
        "source": source_for_label(config, feature.label or ""),
        "feature_type": feature.label or "",
        "rdf_type": rdf_type,
        "source_id": source_id,
        "name": name,
        "population": population,
        "area_sqkm": area_sqkm,
        "state_name": first(values_for(feature, ["STATE_NAME"])),
        "state_fips": first(values_for(feature, ["STATE_FIPS"])),
        "county_fips": first(values_for(feature, ["COUNTY_FIP", "STCO_FIPSC"])),
        "place_fips": first(values_for(feature, ["PLACE_FIPS"])),
        "gnis_id": first(values_for(feature, ["GNIS_ID"])),
        "gnis_name": first(values_for(feature, ["GNIS_NAME"])),
        "yago_uri": feature.yago_uri or "",
    }


def normalize_wkt(raw_value: str, default_srid: int) -> Tuple[str, int, str]:
    value = raw_value.strip()
    srid = default_srid

    prefix = SRID_PREFIX_RE.match(value)
    if prefix:
        srid = int(prefix.group(1))
        value = value[prefix.end() :].lstrip()

    crs_prefix = CRS_PREFIX_RE.match(value)
    if crs_prefix:
        srid = int(crs_prefix.group(1))
        value = value[crs_prefix.end() :].lstrip()

    suffix = CRS_SUFFIX_RE.search(value)
    if suffix:
        srid = int(suffix.group(1))
        value = value[: suffix.start()].rstrip()

    match = WKT_START_RE.match(value)
    geometry_type = "Geometry"
    if match:
        geometry_type = schema.WKT_TYPE_NAMES.get(match.group(1).upper(), match.group(1))

    return value, srid, geometry_type


def iter_country_files(config: CountryConfig, root: Path) -> List[Path]:
    files: List[Path] = []
    for raw in config.inputs:
        path = root / raw
        if not path.exists():
            raise FileNotFoundError(path)
        files.append(path)
    return files


def collect_country(
    config: CountryConfig,
    root: Path,
    include_same_as: bool,
    wkt_cache_path: Path,
) -> Tuple[Dict[str, Feature], Dict[str, List[str]], Set[Edge], List[Path]]:
    features: Dict[str, Feature] = {}
    geometry_to_features: Dict[str, List[str]] = defaultdict(list)
    edges: Set[Edge] = set()
    files = iter_country_files(config, root)

    def get_feature(uri: str) -> Feature:
        if uri not in features:
            features[uri] = Feature(uri=uri)
        return features[uri]

    wkt_cache_path.parent.mkdir(parents=True, exist_ok=True)
    with wkt_cache_path.open("w", newline="", encoding="utf-8") as wkt_handle:
        wkt_writer = csv.DictWriter(wkt_handle, fieldnames=WKT_CACHE_COLUMNS)
        wkt_writer.writeheader()

        for path in files:
            file_label = schema.file_label(path)
            for _line_no, subject, predicate, obj in schema.iter_rdf_triples(path):
                subject_uri = schema.strip_uri(subject)
                pred_uri = schema.strip_uri(predicate)
                pred_name = schema.local_name(predicate)
                kind = schema.object_kind(obj)
                subject_is_geometry = schema.local_name(subject).startswith("Geometry_")

                if pred_uri == schema.GEOSPARQL_AS_WKT:
                    if kind == "literal":
                        try:
                            literal = parse_literal_value(obj)
                            wkt, source_srid, geometry_type = normalize_wkt(
                                literal, config.default_srid
                            )
                            wkt_writer.writerow(
                                {
                                    "geometry_uri": subject_uri,
                                    "wkt": wkt,
                                    "source_srid": str(source_srid),
                                    "geometry_type": geometry_type,
                                }
                            )
                        except ValueError:
                            pass
                    continue

                if pred_uri == OWL_SAME_AS and not include_same_as:
                    continue

                if not subject_is_geometry:
                    feature = get_feature(subject_uri)
                    feature.file_labels.add(file_label)
                else:
                    feature = None

                if pred_uri == schema.RDF_TYPE and kind == "uri" and feature is not None:
                    feature.rdf_types.add(schema.local_name(obj))
                    continue

                if pred_uri == schema.GEOSPARQL_HAS_GEOMETRY and kind == "uri" and feature is not None:
                    geom_uri = schema.strip_uri(obj)
                    feature.geom_uri = geom_uri
                    geometry_to_features[geom_uri].append(subject_uri)
                    continue

                if pred_uri == OWL_SAME_AS and kind == "uri":
                    obj_uri = schema.strip_uri(obj)
                    if feature is not None and "yago-knowledge.org/resource/" in obj_uri:
                        feature.yago_uri = obj_uri
                    if include_same_as:
                        get_feature(obj_uri)
                        edges.add(Edge("SAME_AS", subject_uri, obj_uri, pred_name))
                    continue

                if pred_uri in EDGE_PREDICATES and kind == "uri":
                    obj_uri = schema.strip_uri(obj)
                    get_feature(subject_uri)
                    get_feature(obj_uri)
                    edges.add(Edge(EDGE_PREDICATES[pred_uri], subject_uri, obj_uri, pred_name))
                    continue

                if kind == "literal" and feature is not None and not schema.is_geometry_literal_predicate(pred_uri, pred_name):
                    try:
                        feature.props[pred_name].append(parse_literal_value(obj))
                    except ValueError:
                        feature.props[pred_name].append(obj)

    return features, geometry_to_features, edges, files


def assign_ids(features: Dict[str, Feature]) -> Dict[str, List[Feature]]:
    by_label: Dict[str, List[Feature]] = defaultdict(list)
    for feature in features.values():
        feature.label = choose_label(feature)
        by_label[feature.label].append(feature)

    for label, label_features in by_label.items():
        label_features.sort(key=lambda item: item.uri)
        for idx, feature in enumerate(label_features, 1):
            feature.id = idx

    return dict(sorted(by_label.items()))


def write_csv(path: Path, rows: Iterable[Dict[str, str]], fieldnames: Sequence[str]) -> int:
    count = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: format_agtype_csv_value(field, row.get(field, ""))
                    for field in fieldnames
                }
            )
            count += 1
    return count


def write_vertices(
    config: CountryConfig,
    output_dir: Path,
    by_label: Dict[str, List[Feature]],
    spatial_values: Dict[Tuple[str, int], str],
    spatial_labels: Set[str],
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    vertices_dir = output_dir / "vertices"
    for label, label_features in by_label.items():
        def rows_for_label() -> Iterable[Dict[str, str]]:
            for feature in label_features:
                row = vertex_row(config, feature)
                if label in spatial_labels and feature.id is not None:
                    row["geom"] = spatial_values.get((label, feature.id), "")
                yield row

        rows = rows_for_label()
        fieldnames = VERTEX_COLUMNS if label in spatial_labels else VERTEX_BASE_COLUMNS
        counts[label] = write_csv(vertices_dir / f"{label}.csv", rows, fieldnames)
    return counts


def write_edges(
    config: CountryConfig,
    output_dir: Path,
    features: Dict[str, Feature],
    edges: Set[Edge],
) -> Dict[str, int]:
    edge_rows: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for edge in sorted(edges, key=lambda item: (item.label, item.subject, item.obj)):
        start = features[edge.subject]
        end = features[edge.obj]
        if start.id is None or end.id is None or start.label is None or end.label is None:
            continue
        edge_rows[edge.label].append(
            {
                "start_id": str(start.id),
                "start_vertex_type": start.label,
                "end_id": str(end.id),
                "end_vertex_type": end.label,
                "predicate": edge.predicate,
                "country": config.name,
                "source": config.source,
            }
        )

    counts: Dict[str, int] = {}
    edges_dir = output_dir / "edges"
    for label, rows in sorted(edge_rows.items()):
        counts[label] = write_csv(edges_dir / f"{label}.csv", rows, EDGE_COLUMNS)
    return counts


def build_spatial_vertex_values(
    config: CountryConfig,
    features: Dict[str, Feature],
    geometry_to_features: Dict[str, List[str]],
    wkt_cache_path: Path,
    by_label: Dict[str, List[Feature]],
    fallback_srid: Optional[int],
) -> Tuple[Dict[Tuple[str, int], str], int, int, Dict[str, int], Dict[str, Set[int]], Dict[str, int]]:
    spatial_values: Dict[Tuple[str, int], str] = {}
    input_count = 0
    duplicate_count = 0
    counts_by_label: Dict[str, int] = defaultdict(int)
    source_srids_by_label: Dict[str, Set[int]] = defaultdict(set)
    loader_srids_by_label: Dict[str, int] = {}

    with wkt_cache_path.open("r", newline="", encoding="utf-8") as cache_handle:
        reader = csv.DictReader(cache_handle)
        for cache_row in reader:
            geom_uri = cache_row["geometry_uri"]
            feature_uris = geometry_to_features.get(geom_uri, [])
            if not feature_uris:
                continue
            for feature_uri in feature_uris:
                feature = features.get(feature_uri)
                if feature is None or feature.id is None or feature.label is None:
                    continue
                input_count += 1
                source_srid = int(cache_row["source_srid"])
                source_srids_by_label[feature.label].add(source_srid)

                key = (feature.label, feature.id)
                if key in spatial_values:
                    duplicate_count += 1
                    continue

                spatial_values[key] = f"SRID={source_srid};{cache_row['wkt']}"
                counts_by_label[feature.label] += 1

    for label in by_label:
        label_srids = source_srids_by_label.get(label, set())
        if len(label_srids) > 1:
            raise ValueError(
                f"label {config.name}.{label} has mixed source SRIDs "
                f"{sorted(label_srids)}; the spatial CSV loader needs one SRID "
                "per physical geom column"
            )
        if label_srids:
            loader_srids_by_label[label] = next(iter(label_srids))
        else:
            loader_srids_by_label[label] = fallback_srid or config.default_srid

    return (
        spatial_values,
        input_count,
        duplicate_count,
        dict(counts_by_label),
        dict(source_srids_by_label),
        loader_srids_by_label,
    )


def label_exists_sql(graph_name: str, label: str, kind: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
        f"WHERE g.name = {sql_literal(graph_name)} "
        f"AND l.name = {sql_literal(label)} AND l.kind = {sql_literal(kind)})"
    )


def write_load_sql(
    output_dir: Path,
    config: CountryConfig,
    graph_name: str,
    vertex_counts: Dict[str, int],
    edge_counts: Dict[str, int],
    spatial_labels: Set[str],
    loader_srids_by_label: Dict[str, int],
    loader_root: str,
) -> Path:
    lines: List[str] = []
    q_graph = sql_literal(graph_name)

    lines.extend(
        [
            "LOAD 'age';",
            "SET search_path TO ag_catalog, public;",
            "",
            "-- This script is non-destructive. Drop the graph manually first if you want a clean reload:",
            f"-- SELECT ag_catalog.drop_graph({q_graph}, true);",
            "",
            "DO $$",
            "BEGIN",
            f"    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = {q_graph}) THEN",
            f"        PERFORM ag_catalog.create_graph({q_graph});",
            "    END IF;",
            "END",
            "$$;",
            "",
        ]
    )

    for label in sorted(vertex_counts):
        q_label = sql_literal(label)
        lines.extend(
            [
                "DO $$",
                "BEGIN",
                f"    IF NOT {label_exists_sql(graph_name, label, 'v')} THEN",
                f"        PERFORM ag_catalog.create_vlabel({q_graph}, {q_label});",
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
                f"        PERFORM ag_catalog.create_elabel({q_graph}, {q_label});",
                "    END IF;",
                "END",
                "$$;",
            ]
        )
    lines.append("")

    for label in sorted(vertex_counts):
        relative = f"{loader_root}/vertices/{label}.csv"
        if label not in spatial_labels:
            lines.append(
                "SELECT ag_catalog.load_labels_from_file("
                f"{q_graph}, {sql_literal(label)}, {sql_literal(relative)}, true, true);"
            )
            continue

        spatial_spec = json.dumps(
            {
                "geom": {
                    "type": "geometry",
                    "srid": loader_srids_by_label[label],
                    "geometry_type": "Geometry",
                    "create_if_missing": True,
                    "create_index": True,
                }
            },
            sort_keys=True,
        )
        lines.append(
            "SELECT ag_catalog.load_labels_with_spatial_from_file("
            f"{q_graph}, {sql_literal(label)}, {sql_literal(relative)}, "
            f"true, true, {sql_literal(spatial_spec)}::jsonb);"
        )
    lines.append("")

    for label in sorted(edge_counts):
        relative = f"{loader_root}/edges/{label}.csv"
        lines.append(
            "SELECT ag_catalog.load_edges_from_file("
            f"{q_graph}, {sql_literal(label)}, {sql_literal(relative)}, true);"
        )
    lines.append("")

    lines.extend(
        [
            "ANALYZE;",
            "",
            "SELECT label_name, column_name, spatial_type, geometry_type, srid, index_name",
            "FROM ag_catalog.ag_spatial_columns",
            f"WHERE graph_name = {q_graph}",
            "ORDER BY label_name, column_name;",
        ]
    )

    path = output_dir / f"load_{config.name}.sql"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def prepare_country(
    config: CountryConfig,
    root: Path,
    output_base: Path,
    fallback_srid: Optional[int],
    include_same_as: bool,
) -> Dict[str, object]:
    output_dir = output_base / config.name
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    wkt_cache_path = output_dir / "_geometry_literals.csv"

    features, geometry_to_features, edges, files = collect_country(
        config, root, include_same_as=include_same_as, wkt_cache_path=wkt_cache_path
    )
    by_label = assign_ids(features)
    (
        spatial_values,
        spatial_input_rows,
        spatial_duplicate_rows_skipped,
        spatial_counts,
        source_srids,
        loader_srids_by_label,
    ) = build_spatial_vertex_values(
        config,
        features,
        geometry_to_features,
        wkt_cache_path,
        by_label,
        fallback_srid,
    )
    spatial_labels = set(spatial_counts)
    vertex_counts = write_vertices(
        config, output_dir, by_label, spatial_values, spatial_labels
    )
    edge_counts = write_edges(config, output_dir, features, edges)
    wkt_cache_path.unlink(missing_ok=True)
    graph_name = f"yago2geo_{config.name}"
    loader_root = f"yago2geo/{config.name}"
    sql_path = write_load_sql(
        output_dir,
        config,
        graph_name,
        vertex_counts,
        edge_counts,
        spatial_labels,
        loader_srids_by_label,
        loader_root,
    )

    manifest = {
        "country": config.name,
        "graph_name": graph_name,
        "fallback_srid": fallback_srid or config.default_srid,
        "include_same_as": include_same_as,
        "inputs": [str(path.relative_to(root)) for path in files],
        "vertex_counts": vertex_counts,
        "edge_counts": edge_counts,
        "spatial_input_rows": spatial_input_rows,
        "spatial_vertex_count": sum(spatial_counts.values()),
        "spatial_duplicate_rows_skipped": spatial_duplicate_rows_skipped,
        "spatial_counts": spatial_counts,
        "source_srids_by_label": {
            label: sorted(values) for label, values in source_srids.items()
        },
        "loader_srids_by_label": loader_srids_by_label,
        "load_sql": str(sql_path.relative_to(root)),
        "copy_to_container": f"/tmp/age/{loader_root}",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def write_master_sql(output_base: Path, manifests: Sequence[Dict[str, object]]) -> None:
    manifest_by_country = {
        str(manifest["country"]): manifest for manifest in manifests
    }
    for country in COUNTRIES:
        if country in manifest_by_country:
            continue
        manifest_path = output_base / country / "manifest.json"
        if manifest_path.exists():
            manifest_by_country[country] = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )

    lines: List[str] = [
        "-- Run country files individually after copying CSVs into /tmp/age/yago2geo.",
        "-- Example: benchmark/scripts/load-yago2geo-age.sh greece",
        "",
    ]
    for country in COUNTRIES:
        manifest = manifest_by_country.get(country)
        if manifest is None:
            continue
        lines.append(f"\\i /tmp/age/yago2geo/{manifest['country']}/load_{manifest['country']}.sql")
    (output_base / "load_all.sql").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare YAGO2Geo RDF dumps as AGE CSV load artifacts."
    )
    parser.add_argument(
        "countries",
        nargs="*",
        choices=sorted(COUNTRIES) + ["all"],
        default=["all"],
        help="countries to prepare",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="benchmark/results/load/yago2geo",
        help="output directory for AGE load artifacts",
    )
    parser.add_argument(
        "--fallback-srid",
        type=int,
        default=None,
        help="SRID used for labels with no geometry; defaults to the country source SRID",
    )
    parser.add_argument(
        "--target-srid",
        type=int,
        default=None,
        help="deprecated alias for --fallback-srid; no coordinate transform is performed",
    )
    parser.add_argument(
        "--include-same-as",
        action="store_true",
        help="also generate SAME_AS edges; off by default to keep the spatial graph compact",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path.cwd()
    output_base = root / args.output_dir
    output_base.mkdir(parents=True, exist_ok=True)

    requested = list(args.countries)
    if "all" in requested:
        requested = list(COUNTRIES)

    fallback_srid = args.fallback_srid
    if fallback_srid is None and args.target_srid is not None:
        fallback_srid = args.target_srid

    manifests = []
    for country in requested:
        manifest = prepare_country(
            COUNTRIES[country],
            root,
            output_base,
            fallback_srid=fallback_srid,
            include_same_as=args.include_same_as,
        )
        manifests.append(manifest)
        print(
            f"{country}: vertices={sum(manifest['vertex_counts'].values())} "
            f"edges={sum(manifest['edge_counts'].values())} "
            f"spatial_vertices={manifest['spatial_vertex_count']} "
            f"duplicate_spatial_rows_skipped={manifest['spatial_duplicate_rows_skipped']}"
        )

    write_master_sql(output_base, manifests)
    print(f"Wrote AGE load artifacts to {output_base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
