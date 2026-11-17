#!/usr/bin/env python3
"""Run index-oriented AGE Cypher variants for representative GeoQuestions.

This keeps the same 25 representative questions as
run-geoquestions-age-spatial-function-representatives.py, but rewrites the AGE
Cypher side to make raw geometry GiST usage more visible where possible:

* add explicit bbox predicates, e.g. a.geom && b.geom
* avoid ST_Transform() on the indexed side when source SRIDs are known
* use known labels for fixed anchor entities when the optimized query depends
  on physical geometry column typing

It intentionally does not use ST_IsValid() or ST_MakeValid(), so invalid source
geometries can still produce GEOS topology errors.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[2]
BASE_SCRIPT = ROOT / "benchmark" / "scripts" / (
    "run-geoquestions-age-spatial-function-representatives.py"
)

spec = importlib.util.spec_from_file_location("age_spatial_reps", BASE_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load {BASE_SCRIPT}")
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)


YAGO = base.YAGO


def u(var: str, suffix: str) -> str:
    return base.uri(var, YAGO + suffix)


def g(var: str) -> str:
    return base.geom(var)


def bbox(left: str, right: str) -> str:
    return f"{g(left)} && {g(right)}"


def ask_two_indexed(
    left_label: str,
    left_uri_suffix: str,
    right_label: str,
    right_uri_suffix: str,
    predicate: str,
    include_bbox: bool = False,
) -> str:
    lines = [
        f"MATCH (a:{left_label})",
        f"WHERE {u('a', left_uri_suffix)}",
        "WITH a",
        f"MATCH (b:{right_label})",
        f"WHERE {u('b', right_uri_suffix)}",
        "WITH a, b",
    ]
    if include_bbox:
        lines.append(f"WHERE {bbox('a', 'b')} AND {predicate}")
    else:
        lines.append(f"WHERE {predicate}")
    lines.append("RETURN count(*) > 0 AS answer")
    return "\n".join(lines)


def indexed_representatives():
    reps = {rep.qid: rep for rep in base.representatives()}
    R = base.Representative

    reps[6] = R(
        6,
        "\n".join(
            [
                "MATCH (n:OSI_City_Council)",
                f"WHERE {u('n', 'County_Galway')}",
                "RETURN ST_Area(n.geom) AS area",
            ]
        ),
        "area agtype",
    )

    reps[144] = R(
        144,
        ask_two_indexed(
            "GAG_Region",
            "Crete",
            "GAG_Region",
            "Thessaly",
            f"ST_YMax({g('a')}) < ST_YMin({g('b')})",
        ),
        "answer agtype",
    )

    reps[145] = R(
        145,
        ask_two_indexed(
            "OS_County",
            "Worcestershire",
            "OS_County",
            "Oxfordshire",
            f"ST_XMax({g('a')}) < ST_XMin({g('b')})",
        ),
        "answer agtype",
    )

    reps[146] = R(
        146,
        ask_two_indexed(
            "OSI_County_Council",
            "County_Cavan",
            "OSI_County_Council",
            "County_Mayo",
            f"ST_XMin({g('a')}) > ST_XMax({g('b')})",
        ),
        "answer agtype",
    )

    reps[149] = R(
        149,
        ask_two_indexed(
            "OSI_County_Council",
            "County_Mayo",
            "OSI_County_Council",
            "County_Sligo",
            f"ST_Touches({g('a')}, {g('b')})",
            include_bbox=True,
        ),
        "answer agtype",
    )

    reps[150] = R(
        150,
        ask_two_indexed(
            "OSI_City_Council",
            "Dublin",
            "OSI_County_Council",
            "Kilkenny",
            f"ST_YMin({g('a')}) > ST_YMax({g('b')})",
        ),
        "answer agtype",
    )

    reps[153] = R(
        153,
        ask_two_indexed(
            "OS_County",
            "Oxfordshire",
            "OS_County",
            "Kent",
            f"ST_Distance({base.geom_m('a')}, {base.geom_m('b')}) > 100000",
        ),
        "answer agtype",
    )

    reps[154] = R(
        154,
        ask_two_indexed(
            "OSM_lake",
            "Lough_Leane",
            "OSI_County_Council",
            "County_Kerry",
            f"ST_Within({g('a')}, {g('b')})",
            include_bbox=True,
        ),
        "answer agtype",
    )

    reps[157] = R(
        157,
        ask_two_indexed(
            "OSI_County_Council",
            "County_Galway",
            "OSI_Townland",
            "Claregalway",
            f"ST_Contains({g('a')}, {g('b')})",
            include_bbox=True,
        ),
        "answer agtype",
    )

    reps[278] = R(
        278,
        "\n".join(
            [
                "MATCH (region:GAG_Region)",
                f"WHERE {u('region', 'Epirus_(region)')}",
                "WITH ST_Transform(region.geom, 4326) AS region_geom",
                "MATCH (lake:OSM_lake)",
                "WHERE lake.geom && region_geom",
                "  AND ST_Within(lake.geom, region_geom)",
                "RETURN DISTINCT lake.uri AS lake",
            ]
        ),
        "lake agtype",
    )

    reps[280] = R(
        280,
        "\n".join(
            [
                "MATCH (anchor:OSI_County_Council)",
                f"WHERE {u('anchor', 'County_Mayo')}",
                "WITH anchor",
                "MATCH (county:OSI_County_Council)",
                f"WHERE {bbox('county', 'anchor')}",
                f"  AND ST_Touches({g('anchor')}, {g('county')})",
                "RETURN DISTINCT county.uri AS county",
                "UNION",
                "MATCH (anchor:OSI_County_Council)",
                f"WHERE {u('anchor', 'County_Mayo')}",
                "WITH anchor",
                "MATCH (county:OSI_City_and_County_Council)",
                f"WHERE {bbox('county', 'anchor')}",
                f"  AND ST_Touches({g('anchor')}, {g('county')})",
                "RETURN DISTINCT county.uri AS county",
            ]
        ),
        "county agtype",
    )

    reps[285] = R(
        285,
        "\n".join(
            [
                "MATCH (doolin:OSI_Townland)",
                f"WHERE {u('doolin', 'Doolin')}",
                "WITH doolin",
                "MATCH (bay:OSM_bay)",
                "WHERE bay.geom && ST_Expand(doolin.geom, 0.08)",
                f"  AND ST_Distance({base.geom_m('doolin')}, {base.geom_m('bay')}) < 5000",
                "RETURN DISTINCT bay.uri AS bay",
            ]
        ),
        "bay agtype",
    )

    reps[302] = R(
        302,
        "\n".join(
            [
                "MATCH (x:GAG_RegionalUnit)",
                f"WHERE {u('x', 'geoentity_Perifereiakí_Enótita_Kentrikoú_Toméa_Athinón_8200482')}",
                "WITH x",
                "MATCH (y:GAG_Municipality)",
                f"WHERE {bbox('y', 'x')}",
                f"  AND ST_Contains({base.geom_4326('x')}, {base.geom_4326('y')})",
                "RETURN DISTINCT y.uri AS y",
            ]
        ),
        "y agtype",
    )

    reps[435] = R(
        435,
        "\n".join(
            [
                "MATCH (x:OSM_stream), (y:OSM_city)",
                f"WHERE {bbox('x', 'y')}",
                f"  AND ST_Crosses({g('x')}, {g('y')})",
                "RETURN DISTINCT x.uri AS x",
            ]
        ),
        "x agtype",
    )

    reps[436] = R(
        436,
        "\n".join(
            [
                "MATCH (m:OSI_Barony), (l:OSM_forest), (townland:OSI_Townland)",
                f"WHERE {bbox('l', 'm')}",
                f"  AND ST_Within({g('l')}, {g('m')})",
                f"  AND {g('l')} && {g('townland')}",
                f"  AND ST_Intersects({g('l')}, {g('townland')})",
                "RETURN DISTINCT l.uri AS l",
            ]
        ),
        "l agtype",
    )

    reps[438] = R(
        438,
        "\n".join(
            [
                "MATCH (town:OSM_town), (beach:OSM_beach)",
                f"WHERE {bbox('beach', 'town')}",
                f"  AND ST_Within({g('beach')}, {g('town')})",
                "RETURN DISTINCT town.uri AS town",
            ]
        ),
        "town agtype",
    )

    reps[443] = R(
        443,
        "\n".join(
            [
                "MATCH (lake:OSM_lake), (village:OSM_village), (forest:OSM_forest)",
                f"WHERE {bbox('village', 'lake')}",
                f"  AND {g('forest')} && {g('lake')}",
                f"  AND ST_Touches({g('village')}, {g('lake')})",
                f"  AND ST_Touches({g('forest')}, {g('lake')})",
                "RETURN count(*) > 0 AS answer",
            ]
        ),
        "answer agtype",
    )

    reps[455] = R(
        455,
        "\n".join(
            [
                "MATCH (country:GADM_1stOrder_AdministrativeUnit)",
                f"WHERE {u('country', 'Republic_of_Ireland')}",
                "WITH country",
                "MATCH (vill:OSM_lake), (forest:OSM_forest)",
                f"WHERE {bbox('forest', 'country')}",
                f"  AND ST_Within({g('forest')}, {g('country')})",
                f"  AND {g('vill')} && {g('forest')}",
                f"  AND ST_Intersects({g('vill')}, {g('forest')})",
                "RETURN DISTINCT vill.uri AS vill",
            ]
        ),
        "vill agtype",
    )

    reps[469] = R(
        469,
        "\n".join(
            [
                "MATCH (suffolk:OS_County)",
                f"WHERE {u('suffolk', 'Suffolk')}",
                "WITH suffolk",
                "MATCH (district:OS_District)",
                f"WHERE {bbox('district', 'suffolk')}",
                f"  AND ST_Within({g('district')}, {g('suffolk')})",
                "WITH district",
                "MATCH (forest:OSM_forest)",
                f"WHERE {bbox('forest', 'district')}",
                f"  AND ST_Within({g('forest')}, {g('district')})",
                "RETURN DISTINCT district.uri AS district",
            ]
        ),
        "district agtype",
    )

    reps[484] = R(
        484,
        "\n".join(
            [
                "MATCH (england:GADM_2ndOrder_AdministrativeUnit)",
                f"WHERE {u('england', 'England')}",
                "WITH england",
                "MATCH (scotland:GADM_2ndOrder_AdministrativeUnit)",
                f"WHERE {u('scotland', 'Scotland')}",
                "WITH england, scotland",
                "MATCH (d:OS_UnitaryAuthority)",
                f"WHERE {bbox('d', 'england')}",
                f"  AND ST_Within({g('d')}, {g('england')})",
                f"  AND {g('d')} && {g('scotland')}",
                f"  AND ST_Touches({g('d')}, {g('scotland')})",
                "RETURN DISTINCT d.uri AS d",
            ]
        ),
        "d agtype",
    )

    reps[594] = R(
        594,
        "\n".join(
            [
                "MATCH (region:GAG_Region)",
                f"WHERE {u('region', 'Central_Macedonia')}",
                "WITH region",
                "MATCH (municipality:GAG_Municipality)",
                "WHERE municipality.population > 50000",
                f"  AND {bbox('municipality', 'region')}",
                f"  AND ST_Within({g('municipality')}, {g('region')})",
                "RETURN DISTINCT municipality.uri AS municipality",
            ]
        ),
        "municipality agtype",
    )

    reps[614] = R(
        614,
        "\n".join(
            [
                "MATCH (england:GADM_2ndOrder_AdministrativeUnit)",
                f"WHERE {u('england', 'England')}",
                "WITH england",
                "MATCH (x:OSM_forest)",
                f"WHERE {bbox('x', 'england')}",
                f"  AND ST_Within({g('x')}, {g('england')})",
                "RETURN count(DISTINCT x) AS forests",
            ]
        ),
        "forests agtype",
    )

    reps[627] = R(
        627,
        "\n".join(
            [
                "MATCH (village:OSM_village), (lake:OSM_lake)",
                f"WHERE {bbox('lake', 'village')}",
                f"  AND ST_Touches({g('lake')}, {g('village')})",
                "RETURN count(DISTINCT village) AS villages",
            ]
        ),
        "villages agtype",
    )

    reps[769] = R(
        769,
        "\n".join(
            [
                "MATCH (england:GADM_2ndOrder_AdministrativeUnit)",
                f"WHERE {u('england', 'England')}",
                "WITH england",
                "MATCH (forest:OSM_forest)",
                f"WHERE {bbox('forest', 'england')}",
                f"  AND ST_Within({g('forest')}, {g('england')})",
                "RETURN DISTINCT forest.uri AS forest, ST_Area(forest.geom) AS totalArea",
                "ORDER BY totalArea DESC",
                "LIMIT 1",
            ]
        ),
        "forest agtype, totalArea agtype",
    )

    reps[873] = R(
        873,
        "\n".join(
            [
                "MATCH (england:GADM_2ndOrder_AdministrativeUnit)",
                f"WHERE {u('england', 'England')}",
                "WITH england",
                "MATCH (district:OS_District)",
                f"WHERE {bbox('district', 'england')}",
                f"  AND ST_Within({g('district')}, {g('england')})",
                "WITH district",
                "MATCH (lake:OSM_lake)",
                f"WHERE {bbox('lake', 'district')}",
                f"  AND ST_Within({g('lake')}, {g('district')})",
                "WITH district, count(DISTINCT lake) AS lake_count",
                "RETURN district.uri AS district",
                "ORDER BY lake_count DESC",
                "LIMIT 1",
            ]
        ),
        "district agtype",
    )

    # q484 becomes much slower when bbox predicates are added while keeping
    # England/Scotland as label-free fixed URI anchors. Keep the original
    # SPARQL-faithful AGE query for this outlier in the spatial-index-only run.
    reps[484] = next(rep for rep in base.representatives() if rep.qid == 484)

    return [reps[rep.qid] for rep in base.representatives()]


def indexed_representatives():
    """Return spatial-index-only variants.

    This override intentionally preserves the original label structure:
    labels that came from RDF rdf:type remain, but fixed URI anchors that had
    no rdf:type in the SPARQL are kept label-free. The only optimization added
    here is spatial-index-oriented filtering, mainly explicit bbox predicates.
    """

    reps = {rep.qid: rep for rep in base.representatives()}
    R = base.Representative

    reps[278] = R(
        278,
        "\n".join(
            [
                "MATCH (region)",
                f"WHERE {u('region', 'Epirus_(region)')}",
                "WITH ST_Transform(region.geom, 4326) AS region_geom",
                "MATCH (lake:OSM_lake)",
                "WHERE lake.geom && region_geom",
                "  AND ST_Within(lake.geom, region_geom)",
                "RETURN DISTINCT lake.uri AS lake",
            ]
        ),
        "lake agtype",
    )

    reps[280] = R(
        280,
        "\n".join(
            [
                "MATCH (anchor)",
                f"WHERE {u('anchor', 'County_Mayo')}",
                "WITH anchor.geom AS anchor_geom",
                "MATCH (county:OSI_County_Council)",
                "WHERE county.geom && anchor_geom",
                "  AND ST_Touches(anchor_geom, county.geom)",
                "RETURN DISTINCT county.uri AS county",
                "UNION",
                "MATCH (anchor)",
                f"WHERE {u('anchor', 'County_Mayo')}",
                "WITH anchor.geom AS anchor_geom",
                "MATCH (county:OSI_City_and_County_Council)",
                "WHERE county.geom && anchor_geom",
                "  AND ST_Touches(anchor_geom, county.geom)",
                "RETURN DISTINCT county.uri AS county",
            ]
        ),
        "county agtype",
    )

    reps[285] = R(
        285,
        "\n".join(
            [
                "MATCH (doolin)",
                f"WHERE {u('doolin', 'Doolin')}",
                "WITH doolin, ST_Expand(doolin.geom, 0.08) AS doolin_bbox",
                "MATCH (bay:OSM_bay)",
                "WHERE bay.geom && doolin_bbox",
                f"  AND ST_Distance({base.geom_m('doolin')}, {base.geom_m('bay')}) < 5000",
                "RETURN DISTINCT bay.uri AS bay",
            ]
        ),
        "bay agtype",
    )

    reps[302] = R(
        302,
        "\n".join(
            [
                "MATCH (x)",
                f"WHERE {u('x', 'geoentity_Perifereiakí_Enótita_Kentrikoú_Toméa_Athinón_8200482')}",
                "WITH x.geom AS x_geom, ST_Transform(x.geom, 4326) AS x_geom_4326",
                "MATCH (y:GAG_Municipality)",
                "WHERE y.geom && x_geom",
                "  AND ST_Contains(x_geom_4326, ST_Transform(y.geom, 4326))",
                "RETURN DISTINCT y.uri AS y",
            ]
        ),
        "y agtype",
    )

    reps[435] = R(
        435,
        "\n".join(
            [
                "MATCH (x:OSM_stream), (y:OSM_city)",
                f"WHERE {bbox('x', 'y')}",
                f"  AND ST_Crosses({g('x')}, {g('y')})",
                "RETURN DISTINCT x.uri AS x",
            ]
        ),
        "x agtype",
    )

    reps[436] = R(
        436,
        "\n".join(
            [
                "MATCH (m:OSI_Barony), (l:OSM_forest), (townland:OSI_Townland)",
                f"WHERE {bbox('l', 'm')}",
                f"  AND ST_Within({g('l')}, {g('m')})",
                f"  AND {g('l')} && {g('townland')}",
                f"  AND ST_Intersects({g('l')}, {g('townland')})",
                "RETURN DISTINCT l.uri AS l",
            ]
        ),
        "l agtype",
    )

    reps[438] = R(
        438,
        "\n".join(
            [
                "MATCH (town:OSM_town), (beach:OSM_beach)",
                f"WHERE {bbox('beach', 'town')}",
                f"  AND ST_Within({g('beach')}, {g('town')})",
                "RETURN DISTINCT town.uri AS town",
            ]
        ),
        "town agtype",
    )

    reps[443] = R(
        443,
        "\n".join(
            [
                "MATCH (lake:OSM_lake), (village:OSM_village), (forest:OSM_forest)",
                f"WHERE {bbox('village', 'lake')}",
                f"  AND {g('forest')} && {g('lake')}",
                f"  AND ST_Touches({g('village')}, {g('lake')})",
                f"  AND ST_Touches({g('forest')}, {g('lake')})",
                "RETURN count(*) > 0 AS answer",
            ]
        ),
        "answer agtype",
    )

    reps[455] = R(
        455,
        "\n".join(
            [
                "MATCH (country)",
                f"WHERE {u('country', 'Republic_of_Ireland')}",
                "WITH country.geom AS country_geom",
                "MATCH (vill:OSM_lake), (forest:OSM_forest)",
                "WHERE forest.geom && country_geom",
                "  AND ST_Within(forest.geom, country_geom)",
                "  AND vill.geom && forest.geom",
                "  AND ST_Intersects(vill.geom, forest.geom)",
                "RETURN DISTINCT vill.uri AS vill",
            ]
        ),
        "vill agtype",
    )

    reps[469] = R(
        469,
        "\n".join(
            [
                "MATCH (suffolk:OS_County)",
                f"WHERE {u('suffolk', 'Suffolk')}",
                "WITH suffolk",
                "MATCH (district:OS_District)",
                f"WHERE {bbox('district', 'suffolk')}",
                f"  AND ST_Within({g('district')}, {g('suffolk')})",
                "WITH district",
                "MATCH (forest:OSM_forest)",
                f"WHERE {bbox('forest', 'district')}",
                f"  AND ST_Within({g('forest')}, {g('district')})",
                "RETURN DISTINCT district.uri AS district",
            ]
        ),
        "district agtype",
    )

    # q484 is kept as the original AGE query in this spatial-index-only run.
    # Adding bbox predicates while preserving label-free England/Scotland anchors
    # produced an unstable/worse plan.
    reps[484] = next(rep for rep in base.representatives() if rep.qid == 484)

    reps[594] = R(
        594,
        "\n".join(
            [
                "MATCH (region)",
                f"WHERE {u('region', 'Central_Macedonia')}",
                "WITH region.geom AS region_geom, ST_Transform(region.geom, 4326) AS region_geom_4326",
                "MATCH (municipality:GAG_Municipality)",
                "WHERE municipality.population > 50000",
                "  AND municipality.geom && region_geom",
                "  AND ST_Within(ST_Transform(municipality.geom, 4326), region_geom_4326)",
                "RETURN DISTINCT municipality.uri AS municipality",
            ]
        ),
        "municipality agtype",
    )

    reps[614] = R(
        614,
        "\n".join(
            [
                "MATCH (england)",
                f"WHERE {u('england', 'England')}",
                "WITH england.geom AS england_geom",
                "MATCH (x:OSM_forest)",
                "WHERE x.geom && england_geom",
                "  AND ST_Within(x.geom, england_geom)",
                "RETURN count(DISTINCT x) AS forests",
            ]
        ),
        "forests agtype",
    )

    reps[627] = R(
        627,
        "\n".join(
            [
                "MATCH (village:OSM_village), (lake:OSM_lake)",
                f"WHERE {bbox('lake', 'village')}",
                f"  AND ST_Touches({g('lake')}, {g('village')})",
                "RETURN count(DISTINCT village) AS villages",
            ]
        ),
        "villages agtype",
    )

    reps[769] = R(
        769,
        "\n".join(
            [
                "MATCH (england)",
                f"WHERE {u('england', 'England')}",
                "WITH england.geom AS england_geom",
                "MATCH (forest:OSM_forest)",
                "WHERE forest.geom && england_geom",
                "  AND ST_Within(forest.geom, england_geom)",
                "RETURN DISTINCT forest.uri AS forest, ST_Area(forest.geom) AS totalArea",
                "ORDER BY totalArea DESC",
                "LIMIT 1",
            ]
        ),
        "forest agtype, totalArea agtype",
    )

    reps[873] = R(
        873,
        "\n".join(
            [
                "MATCH (england)",
                f"WHERE {u('england', 'England')}",
                "WITH england.geom AS england_geom",
                "MATCH (district:OS_District)",
                "WHERE district.geom && england_geom",
                "  AND ST_Within(district.geom, england_geom)",
                "WITH district",
                "MATCH (lake:OSM_lake)",
                f"WHERE {bbox('lake', 'district')}",
                f"  AND ST_Within({g('lake')}, {g('district')})",
                "WITH district, count(DISTINCT lake) AS lake_count",
                "RETURN district.uri AS district",
                "ORDER BY lake_count DESC",
                "LIMIT 1",
            ]
        ),
        "district agtype",
    )

    return [reps[rep.qid] for rep in base.representatives()]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run index-oriented representative GeoQuestions AGE queries."
    )
    parser.add_argument(
        "--geoquestions",
        default="benchmark/queries/GeoQuestions1089.json",
    )
    parser.add_argument(
        "--answers",
        default="benchmark/queries/GeoQuestions1089Answers.json",
    )
    parser.add_argument(
        "--output",
        default=(
            "benchmark/results/geoquestions/age_spatial_function_representative/"
            "geoquestions_age_spatial_function_representative_with_spatial_index_results.csv"
        ),
    )
    parser.add_argument("--timeout", type=int, default=90)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    questions = base.load_questions(ROOT / args.geoquestions)
    answers = base.load_answers(ROOT / args.answers)
    reps = indexed_representatives()

    output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=base.OUTPUT_FIELDS)
        writer.writeheader()
        for index, rep in enumerate(reps, 1):
            question = questions[rep.qid]
            age_query = base.sql_wrapper(rep.cypher, rep.signature)
            result, elapsed = base.run_sql(age_query, rep.qid, args.timeout)
            writer.writerow(
                {
                    "category": question.get("Category", ""),
                    "자연어질문": question.get("Question", ""),
                    "SPARQL 쿼리": " ".join(question.get("Query", "").split()),
                    "AGE쿼리": age_query,
                    "원래 정답": answers.get(rep.qid, ""),
                    "age 쿼리 수행결과 정답": result,
                    "실행시간": f"{elapsed:.3f}",
                }
            )
            print(
                f"[{index}/{len(reps)}] q{rep.qid} {question.get('Category', '')} "
                f"{elapsed:.1f}ms",
                flush=True,
            )

    print(f"wrote={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
