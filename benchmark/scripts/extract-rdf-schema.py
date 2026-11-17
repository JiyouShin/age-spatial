#!/usr/bin/env python3
"""Profile RDF files and extract schema-like shapes.

The profiler is intentionally dependency-free so it can run in benchmark
containers or fresh research machines. It streams .nt/.ttl files, groups
subjects by RDF type or file-derived candidate labels, and reports predicate
coverage, object kinds, literal type guesses, URI edge predicates, and repeated
subject shapes.
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


RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
GEOSPARQL_HAS_GEOMETRY = "http://www.opengis.net/ont/geosparql#hasGeometry"
GEOSPARQL_AS_WKT = "http://www.opengis.net/ont/geosparql#asWKT"
PREFIX_RE = re.compile(r"^@prefix\s+([^:]*):\s+<([^>]+)>\s*\.\s*$")
BASE_RE = re.compile(r"^@base\s+<([^>]+)>\s*\.\s*$")

WKT_RE = re.compile(
    r"^(SRID=\d+;)?"
    r"(<[^>]+>\s*)?"
    r"(POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|"
    r"GEOMETRYCOLLECTION)\s*(Z|M|ZM)?\s*\(",
    re.IGNORECASE,
)
WKT_TYPE_NAMES = {
    "POINT": "Point",
    "LINESTRING": "LineString",
    "POLYGON": "Polygon",
    "MULTIPOINT": "MultiPoint",
    "MULTILINESTRING": "MultiLineString",
    "MULTIPOLYGON": "MultiPolygon",
    "GEOMETRYCOLLECTION": "GeometryCollection",
}
INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(
    r"^[+-]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[eE][+-]?\d+)?$"
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


@dataclass
class PredicateStats:
    triple_count: int = 0
    subject_count: int = 0
    object_kinds: Counter = field(default_factory=Counter)
    value_types: Counter = field(default_factory=Counter)
    datatypes: Counter = field(default_factory=Counter)
    languages: Counter = field(default_factory=Counter)
    files: Counter = field(default_factory=Counter)
    examples: List[Dict[str, str]] = field(default_factory=list)
    distinct_values: Set[str] = field(default_factory=set)
    distinct_capped: bool = False


@dataclass
class EdgeStats:
    triple_count: int = 0
    files: Counter = field(default_factory=Counter)
    src_groups: Counter = field(default_factory=Counter)
    dst_groups: Counter = field(default_factory=Counter)
    examples: List[Dict[str, str]] = field(default_factory=list)


def parse_term(line: str, pos: int) -> Tuple[str, int]:
    if pos >= len(line):
        raise ValueError("unexpected end of line")
    if line[pos] == "<":
        end = line.find(">", pos + 1)
        if end < 0:
            raise ValueError("unterminated URI")
        return line[pos : end + 1], end + 1
    if line.startswith("_:", pos):
        end = pos + 2
        while end < len(line) and not line[end].isspace():
            end += 1
        return line[pos:end], end
    raise ValueError("expected URI or blank node")


def skip_ws(line: str, pos: int) -> int:
    while pos < len(line) and line[pos].isspace():
        pos += 1
    return pos


def parse_nt_line(line: str) -> Tuple[str, str, str]:
    line = line.strip()
    if not line or line.startswith("#"):
        raise ValueError("empty/comment line")

    subject, pos = parse_term(line, 0)
    pos = skip_ws(line, pos)
    predicate, pos = parse_term(line, pos)
    pos = skip_ws(line, pos)

    if line.endswith(" ."):
        obj = line[pos:-2].rstrip()
    elif line.endswith("."):
        obj = line[pos:-1].rstrip()
    else:
        raise ValueError("line does not end with '.'")
    if not obj:
        raise ValueError("missing object")
    return subject, predicate, obj


def expand_qname(token: str, prefixes: Dict[str, str]) -> str:
    if token == "a":
        return f"<{RDF_TYPE}>"
    if token.startswith(("<", '"', "_:")):
        return token
    if ":" not in token:
        return token
    prefix, local = token.split(":", 1)
    if prefix in prefixes:
        return f"<{prefixes[prefix]}{local}>"
    return token


def tokenize_turtle(block: str, prefixes: Dict[str, str]) -> List[str]:
    tokens: List[str] = []
    pos = 0
    length = len(block)

    while pos < length:
        ch = block[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch == "#":
            while pos < length and block[pos] not in "\r\n":
                pos += 1
            continue
        if ch in ";,.":
            tokens.append(ch)
            pos += 1
            continue
        if ch == "<":
            end = block.find(">", pos + 1)
            if end < 0:
                raise ValueError("unterminated URI")
            tokens.append(block[pos : end + 1])
            pos = end + 1
            continue
        if block.startswith("_:", pos):
            end = pos + 2
            while end < length and not block[end].isspace() and block[end] not in ";,.":
                end += 1
            tokens.append(block[pos:end])
            pos = end
            continue
        if ch == '"':
            start = pos
            pos += 1
            escaped = False
            while pos < length:
                current = block[pos]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    pos += 1
                    break
                pos += 1

            if pos < length and block[pos] == "@":
                pos += 1
                while pos < length and (block[pos].isalnum() or block[pos] in "-_"):
                    pos += 1
            elif block.startswith("^^", pos):
                pos += 2
                if pos < length and block[pos] == "<":
                    end = block.find(">", pos + 1)
                    if end < 0:
                        raise ValueError("unterminated datatype URI")
                    pos = end + 1
                else:
                    while pos < length and not block[pos].isspace() and block[pos] not in ";,.":
                        pos += 1
            tokens.append(block[start:pos])
            continue

        end = pos
        while end < length and not block[end].isspace() and block[end] not in ";,.":
            end += 1
        tokens.append(expand_qname(block[pos:end], prefixes))
        pos = end

    return tokens


def parse_turtle_block(block: str, prefixes: Dict[str, str]) -> List[Tuple[str, str, str]]:
    tokens = tokenize_turtle(block, prefixes)
    if not tokens:
        return []
    if tokens[0] in {"@prefix", "@base"}:
        return []

    triples: List[Tuple[str, str, str]] = []
    subject = expand_qname(tokens[0], prefixes)
    pos = 1

    while pos < len(tokens):
        if tokens[pos] == ".":
            break
        predicate = expand_qname(tokens[pos], prefixes)
        pos += 1

        while pos < len(tokens):
            obj = expand_qname(tokens[pos], prefixes)
            if obj in {";", ",", "."}:
                raise ValueError("missing object in Turtle block")
            triples.append((subject, predicate, obj))
            pos += 1

            if pos >= len(tokens):
                break
            if tokens[pos] == ",":
                pos += 1
                continue
            break

        if pos >= len(tokens):
            break
        if tokens[pos] == ";":
            pos += 1
            if pos < len(tokens) and tokens[pos] == ".":
                break
            continue
        if tokens[pos] == ".":
            break
        raise ValueError(f"unexpected token in Turtle block: {tokens[pos]}")

    return triples


def iter_nt_triples(path: Path) -> Iterable[Tuple[int, str, str, str]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            subject, predicate, obj = parse_nt_line(line)
            yield line_no, subject, predicate, obj


def iter_turtle_triples(path: Path) -> Iterable[Tuple[int, str, str, str]]:
    prefixes: Dict[str, str] = {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "owl": "http://www.w3.org/2002/07/owl#",
        "geo": "http://www.opengis.net/ont/geosparql#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
    }
    block_lines: List[str] = []
    block_start = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            prefix_match = PREFIX_RE.match(stripped)
            if prefix_match:
                prefixes[prefix_match.group(1)] = prefix_match.group(2)
                continue
            if BASE_RE.match(stripped):
                continue
            if stripped.startswith("#"):
                continue

            if not block_lines:
                block_start = line_no
            block_lines.append(line)

            if stripped.endswith("."):
                block = "".join(block_lines)
                block_lines = []
                for subject, predicate, obj in parse_turtle_block(block, prefixes):
                    yield block_start, subject, predicate, obj

        if block_lines:
            raise ValueError(f"unterminated Turtle block starting at line {block_start}")


def iter_rdf_triples(path: Path) -> Iterable[Tuple[int, str, str, str]]:
    if path.suffix.lower() in {".ttl", ".turtle"}:
        yield from iter_turtle_triples(path)
    else:
        yield from iter_nt_triples(path)


def strip_uri(term: str) -> str:
    if term.startswith("<") and term.endswith(">"):
        return term[1:-1]
    return term


def local_name(term: str) -> str:
    value = strip_uri(term)
    if value.startswith("_:"):
        return value[2:]
    slash = max(value.rfind("/"), value.rfind("#"))
    return value[slash + 1 :] if slash >= 0 else value


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def parse_literal(token: str) -> Tuple[str, Optional[str], Optional[str]]:
    if not token.startswith('"'):
        raise ValueError("literal does not start with quote")
    chars: List[str] = []
    escaped = False
    pos = 1
    while pos < len(token):
        ch = token[pos]
        if escaped:
            if ch == "t":
                chars.append("\t")
            elif ch == "n":
                chars.append("\n")
            elif ch == "r":
                chars.append("\r")
            else:
                chars.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            pos += 1
            break
        else:
            chars.append(ch)
        pos += 1
    suffix = token[pos:].strip()
    lang = None
    datatype = None
    if suffix.startswith("@"):
        lang = suffix[1:]
    elif suffix.startswith("^^"):
        datatype = strip_uri(suffix[2:])
    return "".join(chars), datatype, lang


def object_kind(token: str) -> str:
    if token.startswith("<"):
        return "uri"
    if token.startswith("_:"):
        return "blank"
    if token.startswith('"'):
        return "literal"
    if (
        INT_RE.match(token)
        or FLOAT_RE.match(token)
        or token.lower() in {"true", "false"}
    ):
        return "literal"
    return "other"


def guess_literal_type(value: str) -> str:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return "boolean"
    if lowered in {"nan", "null", "none", ""}:
        return "nullish"
    match = WKT_RE.match(value)
    if match:
        return "wkt:" + WKT_TYPE_NAMES.get(match.group(3).upper(), match.group(3))
    if INT_RE.match(value):
        return "integer"
    if FLOAT_RE.match(value):
        return "float"
    if DATETIME_RE.match(value):
        return "datetime"
    if DATE_RE.match(value):
        return "date"
    return "text"


def is_geometry_literal_predicate(pred_uri: str, pred_name: str) -> bool:
    return pred_name in {"the_geom", "asWKT"} or pred_uri == GEOSPARQL_AS_WKT


def guess_wkt_object_type(token: str) -> str:
    preview = token[1:200] if token.startswith('"') else token[:200]
    match = WKT_RE.match(preview)
    if not match:
        return "wkt"
    return "wkt:" + WKT_TYPE_NAMES.get(match.group(3).upper(), match.group(3))


def file_label(path: Path) -> str:
    stem = path.stem
    if stem.startswith("extended_"):
        stem = stem[len("extended_") :]
    stem = stem.replace("_updated", "").replace("_geom", "")
    known = {
        "counties": "County",
        "incorps": "IncorporatedPlace",
        "mcds": "MinorCivilDivision",
        "pp_states": "State",
        "pp_unincorps": "UnincorporatedPlace",
    }
    if stem in known:
        return known[stem]
    return "".join(part[:1].upper() + part[1:] for part in stem.split("_") if part)


def pascal_label(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def uri_pattern_group(term: str) -> Optional[str]:
    uri = strip_uri(term)
    name = local_name(term)
    if name.startswith("Geometry_"):
        return None
    if "data.geohive.ie/resource/" in uri:
        tail = uri.split("data.geohive.ie/resource/", 1)[1]
        family = tail.split("/", 1)[0]
        label = pascal_label(family)
        return f"GeoHive/{label}" if label else "GeoHive/Resource"
    if "kr.di.uoa.gr/yago2geo/resource/osientity_" in uri:
        return "OsiEntity"
    if "kr.di.uoa.gr/yago2geo/resource/osentity_" in uri:
        return "OsEntity"
    if "kr.di.uoa.gr/yago2geo/resource/osnientity_" in uri:
        return "OsniEntity"
    if "kr.di.uoa.gr/yago2geo/resource/gagentity_" in uri:
        return "GagEntity"
    if "kr.di.uoa.gr/yago2geo/resource/geoentity_" in uri:
        return "Yago2GeoEntity"
    if "yago-knowledge.org/resource/" in uri:
        return "External/YAGO"
    return None


def subject_group(path: Path, subject: str, rdf_types: Dict[str, Set[str]]) -> str:
    name = local_name(subject)
    type_names = sorted(
        type_name
        for type_name in rdf_types.get(subject, set())
        if type_name not in {"Feature", "SpatialObject"}
    )
    if type_names:
        return "+".join(type_names)
    if name.startswith("Geometry_"):
        return f"Geometry/{file_label(path)}"
    pattern_group = uri_pattern_group(subject)
    if pattern_group:
        return pattern_group
    return file_label(path)


def json_counter(counter: Counter) -> Dict[str, int]:
    return {str(k): int(v) for k, v in counter.most_common()}


def add_example(
    examples: List[Dict[str, str]],
    max_examples: int,
    subject: str,
    predicate: str,
    obj: str,
    file_name: str,
    line_no: int,
    sample_limit: int,
) -> None:
    if len(examples) >= max_examples:
        return
    examples.append(
        {
            "file": file_name,
            "line": str(line_no),
            "subject": truncate(subject, sample_limit),
            "predicate": truncate(predicate, sample_limit),
            "object": truncate(obj, sample_limit),
        }
    )


def iter_input_files(paths: Sequence[str]) -> List[Path]:
    files: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for pattern in ("*.nt", "*.ttl", "*.turtle"):
                files.extend(sorted(path.glob(pattern)))
        else:
            matches = sorted(Path().glob(raw)) if any(ch in raw for ch in "*?[") else [path]
            files.extend(matches)
    seen: Set[Path] = set()
    unique: List[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def write_csv(path: Path, rows: Iterable[Dict[str, object]], fieldnames: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract schema-like profiles from RDF .nt/.ttl files."
    )
    parser.add_argument("inputs", nargs="+", help=".nt/.ttl files, globs, or directories")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="benchmark/results/schema/yago2geo",
        help="directory for schema report files",
    )
    parser.add_argument("--max-examples", type=int, default=3)
    parser.add_argument("--sample-limit", type=int, default=220)
    parser.add_argument("--distinct-limit", type=int, default=1000)
    parser.add_argument("--shape-limit", type=int, default=30)
    args = parser.parse_args(argv)

    input_files = iter_input_files(args.inputs)
    if not input_files:
        print("No input files found.", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    file_stats: Dict[str, Counter] = defaultdict(Counter)
    group_subject_predicates: Dict[str, Dict[str, Set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    group_subject_triples: Dict[str, Counter] = defaultdict(Counter)
    group_predicate_subjects: Dict[str, Dict[str, Set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    group_predicate_stats: Dict[str, Dict[str, PredicateStats]] = defaultdict(
        lambda: defaultdict(PredicateStats)
    )
    global_predicate_stats: Dict[str, PredicateStats] = defaultdict(PredicateStats)
    subject_groups: Dict[str, Set[str]] = defaultdict(set)
    rdf_types: Dict[str, Set[str]] = defaultdict(set)
    uri_edges: List[Tuple[str, str, str, str, int, str]] = []
    parse_errors: List[Dict[str, object]] = []

    for path in input_files:
        file_name = str(path)
        label = file_label(path)
        file_stats[file_name]["input_label_candidates"] += 1
        try:
            triples = iter_rdf_triples(path)
            for line_no, subject, predicate, obj in triples:
                pred_uri = strip_uri(predicate)
                pred_name = local_name(predicate)
                kind = object_kind(obj)
                if pred_uri == RDF_TYPE and kind == "uri":
                    rdf_types[subject].add(local_name(obj))
                group = subject_group(path, subject, rdf_types)

                file_stats[file_name]["triples"] += 1
                file_stats[file_name][f"group:{group}"] += 1
                file_stats[file_name][f"object_kind:{kind}"] += 1
                file_stats[file_name][f"label_candidate:{label}"] += 1

                subject_groups[subject].add(group)
                group_subject_predicates[group][subject].add(pred_name)
                group_subject_triples[group][subject] += 1
                group_predicate_subjects[group][pred_name].add(subject)

                for stats in (
                    group_predicate_stats[group][pred_name],
                    global_predicate_stats[pred_name],
                ):
                    stats.triple_count += 1
                    stats.object_kinds[kind] += 1
                    stats.files[file_name] += 1
                    add_example(
                        stats.examples,
                        args.max_examples,
                        subject,
                        predicate,
                        obj,
                        file_name,
                        line_no,
                        args.sample_limit,
                    )

                if kind == "literal":
                    if is_geometry_literal_predicate(pred_uri, pred_name):
                        literal, datatype, lang = "", None, None
                        value_type = guess_wkt_object_type(obj)
                    else:
                        try:
                            literal, datatype, lang = parse_literal(obj)
                        except ValueError:
                            literal, datatype, lang = obj, None, None
                        value_type = guess_literal_type(literal)
                    value_sample = literal
                    for stats in (
                        group_predicate_stats[group][pred_name],
                        global_predicate_stats[pred_name],
                    ):
                        stats.value_types[value_type] += 1
                        if datatype:
                            stats.datatypes[datatype] += 1
                        if lang:
                            stats.languages[lang] += 1
                        if value_type.startswith("wkt"):
                            stats.distinct_capped = True
                            stats.distinct_values.clear()
                        elif not stats.distinct_capped:
                            if len(stats.distinct_values) < args.distinct_limit:
                                stats.distinct_values.add(value_sample)
                            else:
                                stats.distinct_capped = True
                                stats.distinct_values.clear()
                elif kind == "uri":
                    obj_uri = strip_uri(obj)
                    if pred_uri == RDF_TYPE:
                        rdf_types[subject].add(local_name(obj))
                    uri_edges.append((subject, pred_name, obj, file_name, line_no, group))
                    for stats in (
                        group_predicate_stats[group][pred_name],
                        global_predicate_stats[pred_name],
                    ):
                        stats.value_types["uri"] += 1
                        if not stats.distinct_capped:
                            if len(stats.distinct_values) < args.distinct_limit:
                                stats.distinct_values.add(obj_uri)
                            else:
                                stats.distinct_capped = True
                                stats.distinct_values.clear()
                else:
                    for stats in (
                        group_predicate_stats[group][pred_name],
                        global_predicate_stats[pred_name],
                    ):
                        stats.value_types[kind] += 1
        except (OSError, ValueError) as exc:
            parse_errors.append({"file": file_name, "line": 0, "error": str(exc)})
            file_stats[file_name]["parse_errors"] += 1
            continue

    for group, predicates in group_predicate_subjects.items():
        for pred_name, subjects in predicates.items():
            group_predicate_stats[group][pred_name].subject_count = len(subjects)
    all_predicate_subjects: Dict[str, Set[str]] = defaultdict(set)
    for group, predicates in group_predicate_subjects.items():
        for pred_name, subjects in predicates.items():
            all_predicate_subjects[pred_name].update(subjects)
    for pred_name, subjects in all_predicate_subjects.items():
        global_predicate_stats[pred_name].subject_count = len(subjects)

    edge_stats: Dict[str, EdgeStats] = defaultdict(EdgeStats)
    for subject, pred_name, obj, file_name, line_no, src_group in uri_edges:
        dst_groups = subject_groups.get(obj)
        if not dst_groups:
            dst_groups = {uri_pattern_group(obj) or "ExternalResource"}
        stats = edge_stats[pred_name]
        stats.triple_count += 1
        stats.files[file_name] += 1
        stats.src_groups[src_group] += 1
        for dst_group in dst_groups:
            stats.dst_groups[dst_group] += 1
        add_example(
            stats.examples,
            args.max_examples,
            subject,
            pred_name,
            obj,
            file_name,
            line_no,
            args.sample_limit,
        )

    group_rows: List[Dict[str, object]] = []
    for group, subjects in sorted(group_subject_predicates.items()):
        triple_count = sum(group_subject_triples[group].values())
        shape_counter = Counter(
            tuple(sorted(predicates)) for predicates in subjects.values()
        )
        group_rows.append(
            {
                "group": group,
                "subjects": len(subjects),
                "triples": triple_count,
                "predicate_count": len(group_predicate_stats[group]),
                "shape_count": len(shape_counter),
                "top_shape_subjects": shape_counter.most_common(1)[0][1]
                if shape_counter
                else 0,
            }
        )

    predicate_rows: List[Dict[str, object]] = []
    for group, stats_by_predicate in sorted(group_predicate_stats.items()):
        group_subject_count = len(group_subject_predicates[group])
        for pred_name, stats in sorted(stats_by_predicate.items()):
            coverage = (
                stats.subject_count / group_subject_count if group_subject_count else 0
            )
            predicate_rows.append(
                {
                    "group": group,
                    "predicate": pred_name,
                    "triple_count": stats.triple_count,
                    "subject_count": stats.subject_count,
                    "coverage": f"{coverage:.6f}",
                    "object_kinds": json.dumps(json_counter(stats.object_kinds)),
                    "value_types": json.dumps(json_counter(stats.value_types)),
                    "distinct_values": ">" + str(args.distinct_limit)
                    if stats.distinct_capped
                    else str(len(stats.distinct_values)),
                    "example_object": stats.examples[0]["object"]
                    if stats.examples
                    else "",
                }
            )

    global_predicate_rows: List[Dict[str, object]] = []
    for pred_name, stats in sorted(global_predicate_stats.items()):
        global_predicate_rows.append(
            {
                "predicate": pred_name,
                "triple_count": stats.triple_count,
                "subject_count": stats.subject_count,
                "object_kinds": json.dumps(json_counter(stats.object_kinds)),
                "value_types": json.dumps(json_counter(stats.value_types)),
                "files": json.dumps(json_counter(stats.files)),
                "distinct_values": ">" + str(args.distinct_limit)
                if stats.distinct_capped
                else str(len(stats.distinct_values)),
                "example_object": stats.examples[0]["object"] if stats.examples else "",
            }
        )

    edge_rows: List[Dict[str, object]] = []
    for pred_name, stats in sorted(edge_stats.items()):
        edge_rows.append(
            {
                "predicate": pred_name,
                "triple_count": stats.triple_count,
                "src_groups": json.dumps(json_counter(stats.src_groups)),
                "dst_groups": json.dumps(json_counter(stats.dst_groups)),
                "files": json.dumps(json_counter(stats.files)),
                "example_subject": stats.examples[0]["subject"] if stats.examples else "",
                "example_object": stats.examples[0]["object"] if stats.examples else "",
            }
        )

    shape_rows: List[Dict[str, object]] = []
    for group, subjects in sorted(group_subject_predicates.items()):
        shape_counter = Counter(
            tuple(sorted(predicates)) for predicates in subjects.values()
        )
        samples_by_shape: Dict[Tuple[str, ...], List[str]] = defaultdict(list)
        for subject, predicates in subjects.items():
            shape = tuple(sorted(predicates))
            if len(samples_by_shape[shape]) < args.max_examples:
                samples_by_shape[shape].append(subject)
        for rank, (shape, count) in enumerate(
            shape_counter.most_common(args.shape_limit), 1
        ):
            shape_rows.append(
                {
                    "group": group,
                    "rank": rank,
                    "subject_count": count,
                    "predicate_count": len(shape),
                    "predicates": "|".join(shape),
                    "sample_subjects": "|".join(
                        truncate(s, args.sample_limit) for s in samples_by_shape[shape]
                    ),
                }
            )

    write_csv(
        output_dir / "groups.csv",
        group_rows,
        ["group", "subjects", "triples", "predicate_count", "shape_count", "top_shape_subjects"],
    )
    write_csv(
        output_dir / "predicates_by_group.csv",
        predicate_rows,
        [
            "group",
            "predicate",
            "triple_count",
            "subject_count",
            "coverage",
            "object_kinds",
            "value_types",
            "distinct_values",
            "example_object",
        ],
    )
    write_csv(
        output_dir / "predicates_global.csv",
        global_predicate_rows,
        [
            "predicate",
            "triple_count",
            "subject_count",
            "object_kinds",
            "value_types",
            "files",
            "distinct_values",
            "example_object",
        ],
    )
    write_csv(
        output_dir / "uri_edges.csv",
        edge_rows,
        [
            "predicate",
            "triple_count",
            "src_groups",
            "dst_groups",
            "files",
            "example_subject",
            "example_object",
        ],
    )
    write_csv(
        output_dir / "shapes.csv",
        shape_rows,
        ["group", "rank", "subject_count", "predicate_count", "predicates", "sample_subjects"],
    )

    summary = {
        "input_files": [str(path) for path in input_files],
        "files": {file_name: json_counter(stats) for file_name, stats in file_stats.items()},
        "groups": group_rows,
        "uri_edges": edge_rows,
        "rdf_type_subjects": len(rdf_types),
        "parse_errors": parse_errors,
    }
    with (output_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    report_lines: List[str] = []
    report_lines.append("# RDF Schema Profile")
    report_lines.append("")
    report_lines.append("## Inputs")
    report_lines.append("")
    report_lines.append("| file | triples | parse_errors |")
    report_lines.append("| --- | ---: | ---: |")
    for file_name, stats in sorted(file_stats.items()):
        report_lines.append(
            f"| `{file_name}` | {stats['triples']} | {stats['parse_errors']} |"
        )
    report_lines.append("")
    report_lines.append("## Candidate Groups")
    report_lines.append("")
    report_lines.append("| group | subjects | triples | predicates | shapes | top shape subjects |")
    report_lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in group_rows:
        report_lines.append(
            f"| `{row['group']}` | {row['subjects']} | {row['triples']} | "
            f"{row['predicate_count']} | {row['shape_count']} | {row['top_shape_subjects']} |"
        )
    report_lines.append("")
    report_lines.append("## URI Edge Predicates")
    report_lines.append("")
    if edge_rows:
        report_lines.append("| predicate | triples | src groups | dst groups |")
        report_lines.append("| --- | ---: | --- | --- |")
        for row in edge_rows:
            report_lines.append(
                f"| `{row['predicate']}` | {row['triple_count']} | "
                f"`{row['src_groups']}` | `{row['dst_groups']}` |"
            )
    else:
        report_lines.append("No URI-object predicates found.")
    report_lines.append("")
    report_lines.append("## Predicate Coverage By Group")
    report_lines.append("")
    for group in sorted(group_predicate_stats):
        report_lines.append(f"### {group}")
        report_lines.append("")
        report_lines.append(
            "| predicate | coverage | subjects | triples | object kinds | value types | example |"
        )
        report_lines.append("| --- | ---: | ---: | ---: | --- | --- | --- |")
        group_subject_count = len(group_subject_predicates[group])
        stats_rows = []
        for pred_name, stats in group_predicate_stats[group].items():
            coverage = (
                stats.subject_count / group_subject_count if group_subject_count else 0
            )
            stats_rows.append((coverage, pred_name, stats))
        for coverage, pred_name, stats in sorted(stats_rows, key=lambda r: (-r[0], r[1])):
            example = stats.examples[0]["object"].replace("|", "\\|") if stats.examples else ""
            report_lines.append(
                f"| `{pred_name}` | {coverage:.2%} | {stats.subject_count} | "
                f"{stats.triple_count} | `{json.dumps(json_counter(stats.object_kinds))}` | "
                f"`{json.dumps(json_counter(stats.value_types))}` | `{example}` |"
            )
        report_lines.append("")
    report_lines.append("## Top Shapes")
    report_lines.append("")
    for group in sorted(group_subject_predicates):
        report_lines.append(f"### {group}")
        report_lines.append("")
        report_lines.append("| rank | subjects | predicate count | predicates |")
        report_lines.append("| ---: | ---: | ---: | --- |")
        for row in [r for r in shape_rows if r["group"] == group]:
            predicates = truncate(str(row["predicates"]).replace("|", ", "), 500)
            report_lines.append(
                f"| {row['rank']} | {row['subject_count']} | "
                f"{row['predicate_count']} | `{predicates}` |"
            )
        report_lines.append("")

    (output_dir / "schema_report.md").write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )

    print(f"Wrote schema profile to {output_dir}")
    print(f"Groups: {len(group_rows)}")
    print(f"URI edge predicates: {len(edge_rows)}")
    print(f"Parse errors: {len(parse_errors)} shown, see summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
