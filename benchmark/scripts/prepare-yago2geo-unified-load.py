#!/usr/bin/env python3
"""Combine country YAGO2Geo AGE load artifacts into one graph load.

The per-country preparation step writes one graph per country. GeoQuestions1089
is written against a single YAGO2Geo RDF graph, so this script creates a
unified AGE load directory that targets graph `yago2geo`.

The unified CSVs intentionally drop loader-context columns that are not present
in the source RDF triples (`country`, `source`, and the redundant
`feature_type`). IDs are reassigned per AGE label so edge CSVs still point to
the correct vertices after multiple countries are combined.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


COUNTRIES = ("usa", "ireland", "uk", "greece")
DROP_VERTEX_COLUMNS = {"country", "source", "feature_type"}
DROP_EDGE_COLUMNS = {"country", "source"}
RAW_LOAD_COLUMNS = {
    "id",
    "start_id",
    "start_vertex_type",
    "end_id",
    "end_vertex_type",
    "geom",
}


try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def format_agtype_csv_value(column: str, value: str) -> str:
    if value == "" or column in RAW_LOAD_COLUMNS:
        return value

    try:
        json.loads(value)
    except json.JSONDecodeError:
        return json.dumps(value, ensure_ascii=False)

    return value


def label_exists_sql(graph_name: str, label: str, kind: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
        f"WHERE g.name = {sql_literal(graph_name)} "
        f"AND l.name = {sql_literal(label)} AND l.kind = {sql_literal(kind)})"
    )


def read_manifest(source_dir: Path, country: str) -> Dict[str, object]:
    path = source_dir / country / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"missing {path}; run prepare-yago2geo-age-load.py {country} first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def output_fields(input_fields: Sequence[str], drop_columns: set[str]) -> List[str]:
    return [field for field in input_fields if field not in drop_columns]


def open_writer(
    path: Path,
    fieldnames: Sequence[str],
    known_fieldnames: Dict[str, List[str]],
) -> Tuple[object, csv.DictWriter]:
    key = str(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if key in known_fieldnames:
        if known_fieldnames[key] != list(fieldnames):
            raise ValueError(
                f"inconsistent header for {path}: "
                f"{known_fieldnames[key]} vs {list(fieldnames)}"
            )
        handle = path.open("a", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        return handle, writer

    known_fieldnames[key] = list(fieldnames)
    handle = path.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    return handle, writer


def combine_vertices(
    source_dir: Path,
    output_dir: Path,
    countries: Sequence[str],
    manifests: Dict[str, Dict[str, object]],
) -> Tuple[Dict[str, int], Dict[Tuple[str, str], int], Dict[str, int]]:
    vertex_counts: Dict[str, int] = {}
    id_offsets: Dict[Tuple[str, str], int] = {}
    loader_srids_by_label: Dict[str, int] = {}
    known_headers: Dict[str, List[str]] = {}

    for country in countries:
        manifest = manifests[country]
        country_dir = source_dir / country / "vertices"
        for path in sorted(country_dir.glob("*.csv")):
            label = path.stem
            offset = vertex_counts.get(label, 0)
            id_offsets[(country, label)] = offset

            with path.open(newline="", encoding="utf-8") as src:
                reader = csv.DictReader(src)
                if reader.fieldnames is None:
                    continue
                fieldnames = output_fields(reader.fieldnames, DROP_VERTEX_COLUMNS)
                out_path = output_dir / "vertices" / path.name
                handle, writer = open_writer(out_path, fieldnames, known_headers)
                with handle:
                    count = 0
                    for row in reader:
                        row["id"] = str(int(row["id"]) + offset)
                        out_row = {
                            field: format_agtype_csv_value(field, row.get(field, ""))
                            for field in fieldnames
                        }
                        writer.writerow(out_row)
                        count += 1

            vertex_counts[label] = offset + count

            if "geom" in fieldnames:
                srid_map = manifest.get("loader_srids_by_label", {})
                if label not in srid_map:
                    raise ValueError(f"spatial label {country}.{label} has no SRID")
                srid = int(srid_map[label])
                previous = loader_srids_by_label.get(label)
                if previous is not None and previous != srid:
                    raise ValueError(
                        f"label {label} has mixed SRIDs in unified graph: "
                        f"{previous} and {srid}"
                    )
                loader_srids_by_label[label] = srid

    return vertex_counts, id_offsets, loader_srids_by_label


def combine_edges(
    source_dir: Path,
    output_dir: Path,
    countries: Sequence[str],
    id_offsets: Dict[Tuple[str, str], int],
) -> Dict[str, int]:
    edge_counts: Dict[str, int] = {}
    known_headers: Dict[str, List[str]] = {}

    for country in countries:
        edge_dir = source_dir / country / "edges"
        if not edge_dir.exists():
            continue
        for path in sorted(edge_dir.glob("*.csv")):
            label = path.stem
            with path.open(newline="", encoding="utf-8") as src:
                reader = csv.DictReader(src)
                if reader.fieldnames is None:
                    continue
                fieldnames = output_fields(reader.fieldnames, DROP_EDGE_COLUMNS)
                out_path = output_dir / "edges" / path.name
                handle, writer = open_writer(out_path, fieldnames, known_headers)
                with handle:
                    count = 0
                    for row in reader:
                        start_label = row["start_vertex_type"]
                        end_label = row["end_vertex_type"]
                        row["start_id"] = str(
                            int(row["start_id"]) + id_offsets[(country, start_label)]
                        )
                        row["end_id"] = str(
                            int(row["end_id"]) + id_offsets[(country, end_label)]
                        )
                        out_row = {
                            field: format_agtype_csv_value(field, row.get(field, ""))
                            for field in fieldnames
                        }
                        writer.writerow(out_row)
                        count += 1
            edge_counts[label] = edge_counts.get(label, 0) + count

    return edge_counts


def write_load_sql(
    output_dir: Path,
    graph_name: str,
    loader_root: str,
    vertex_counts: Dict[str, int],
    edge_counts: Dict[str, int],
    loader_srids_by_label: Dict[str, int],
) -> Path:
    q_graph = sql_literal(graph_name)
    lines: List[str] = [
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
        if label not in loader_srids_by_label:
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

    path = output_dir / "load_unified.sql"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine per-country YAGO2Geo AGE load artifacts into graph yago2geo."
    )
    parser.add_argument(
        "--source-dir",
        default="benchmark/results/load/yago2geo",
        help="directory containing country AGE load artifacts",
    )
    parser.add_argument(
        "--output-name",
        default="unified",
        help="subdirectory name written under --source-dir",
    )
    parser.add_argument(
        "--graph-name",
        default="yago2geo",
        help="target AGE graph name",
    )
    parser.add_argument(
        "--countries",
        nargs="+",
        default=list(COUNTRIES),
        choices=list(COUNTRIES),
        help="country artifacts to combine",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path.cwd()
    source_dir = root / args.source_dir
    output_dir = source_dir / args.output_name
    loader_root = f"yago2geo/{args.output_name}"

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifests = {
        country: read_manifest(source_dir, country) for country in args.countries
    }
    vertex_counts, id_offsets, loader_srids_by_label = combine_vertices(
        source_dir, output_dir, args.countries, manifests
    )
    edge_counts = combine_edges(source_dir, output_dir, args.countries, id_offsets)
    sql_path = write_load_sql(
        output_dir,
        args.graph_name,
        loader_root,
        vertex_counts,
        edge_counts,
        loader_srids_by_label,
    )

    manifest = {
        "graph_name": args.graph_name,
        "source_countries": list(args.countries),
        "dropped_vertex_columns": sorted(DROP_VERTEX_COLUMNS),
        "dropped_edge_columns": sorted(DROP_EDGE_COLUMNS),
        "vertex_counts": vertex_counts,
        "edge_counts": edge_counts,
        "loader_srids_by_label": loader_srids_by_label,
        "load_sql": str(sql_path.relative_to(root)),
        "copy_to_container": f"/tmp/age/{loader_root}",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"{args.output_name}: graph={args.graph_name} "
        f"vertices={sum(vertex_counts.values())} "
        f"edges={sum(edge_counts.values())} "
        f"spatial_labels={len(loader_srids_by_label)}"
    )
    print(f"Wrote unified AGE load artifacts to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
