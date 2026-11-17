#!/usr/bin/env python3
"""Build Virtuoso virtrdf:Geometry RDF files for the UK subset.

Input triples use GeoSPARQL:

    <feature> geo:hasGeometry <feature#geometry> .
    <feature#geometry> geo:asWKT "...WKT..."^^geo:wktLiteral .

Virtuoso's native RDF geometry index is populated when geometry values are
stored as virtrdf:Geometry literals. This script preserves the original graph
and writes a derived graph input:

    <feature> wgs84_pos:geometry "...WKT..."^^virtrdf:Geometry .
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "benchmark/results/rdfdb/uk_subset_input"
DEFAULT_OUTPUT = ROOT / "benchmark/results/rdfdb/uk_subset_virtgeom_input"

HAS_GEOM = "http://www.opengis.net/ont/geosparql#hasGeometry"
AS_WKT = "http://www.opengis.net/ont/geosparql#asWKT"
WKT_LITERAL = "http://www.opengis.net/ont/geosparql#wktLiteral"
VIRT_GEOM = "http://www.openlinksw.com/schemas/virtrdf#Geometry"
WGS_GEOMETRY = "http://www.w3.org/2003/01/geo/wgs84_pos#geometry"

HAS_GEOM_RE = re.compile(
    rf"^(<[^>]+>)\s+<{re.escape(HAS_GEOM)}>\s+(<[^>]+>)\s+\.\s*$"
)
AS_WKT_RE = re.compile(
    rf"^(<[^>]+>)\s+<{re.escape(AS_WKT)}>\s+(\".*\")\^\^<{re.escape(WKT_LITERAL)}>\s+\.\s*$"
)


def convert_file(src: Path, dst: Path) -> int:
    geom_to_feature: dict[str, str] = {}
    pending_wkt: dict[str, str] = {}
    written = 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("r", encoding="utf-8", errors="replace") as inp, dst.open(
        "w", encoding="utf-8"
    ) as out:
        for line in inp:
            match = HAS_GEOM_RE.match(line)
            if match:
                feature, geom = match.group(1), match.group(2)
                geom_to_feature[geom] = feature
                if geom in pending_wkt:
                    wkt = pending_wkt.pop(geom)
                    out.write(
                        f"{feature} <{WGS_GEOMETRY}> {wkt}^^<{VIRT_GEOM}> .\n"
                    )
                    written += 1
                continue

            match = AS_WKT_RE.match(line)
            if match:
                geom, wkt = match.group(1), match.group(2)
                feature = geom_to_feature.get(geom)
                if feature is None:
                    pending_wkt[geom] = wkt
                else:
                    out.write(f"{feature} <{WGS_GEOMETRY}> {wkt}^^<{VIRT_GEOM}> .\n")
                    written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.input.exists():
        raise SystemExit(f"Input directory does not exist: {args.input}")

    args.output.mkdir(parents=True, exist_ok=True)
    total = 0
    for src in sorted(args.input.glob("*.nt")):
        dst = args.output / src.name
        count = convert_file(src, dst)
        total += count
        print(f"{src.name}: {count}")

    print(f"total: {total}")
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
