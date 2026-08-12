# YAGO2Geo RDF-to-LPG Sample Transformer

This directory contains a small, standalone example of the RDF-to-LPG
transformation used for AGE-Spatial experiments. It is intended as a
reproducible sample, not as a copy of the full YAGO2Geo benchmark data.

The transformer converts YAGO2Geo-style RDF triples into AGE-Spatial load
artifacts:

- `vertices/*.csv`
- `edges/*.csv`
- `load.sql`
- `manifest.json`

Geometry literals connected through `geo:hasGeometry` and `geo:asWKT` are
written to a `geom` CSV column. When `load.sql` is executed with AGE-Spatial,
the spatial CSV loader stores `geom` as a physical PostGIS `geometry` column.

## Files

- `yago2geo_rdf_to_lpg.py`: RDF-to-LPG converter.
- `sample/yago2geo_sample.nt`: small YAGO2Geo-style RDF sample.

## Run the Sample

From the repository root:

```bash
python3 tools/yago2geo-rdf-to-lpg/yago2geo_rdf_to_lpg.py \
  --input tools/yago2geo-rdf-to-lpg/sample/yago2geo_sample.nt \
  --output-dir /tmp/yago2geo_lpg_sample \
  --graph-name yago2geo_sample \
  --overwrite
```

The output directory will contain:

```text
/tmp/yago2geo_lpg_sample/
  vertices/
    GADM_Country.csv
    OS_County.csv
    OSM_forest.csv
  edges/
    WITHIN.csv
  load.sql
  manifest.json
```

## Load into AGE-Spatial

Install PostgreSQL, PostGIS, and AGE-Spatial first. Then execute the generated
SQL file from a PostgreSQL environment that can read the generated CSV paths:

```bash
psql -d your_database -f /tmp/yago2geo_lpg_sample/load.sql
```

If PostgreSQL runs in a different environment, pass `--loader-root` with the
path that the PostgreSQL server can read:

```bash
python3 tools/yago2geo-rdf-to-lpg/yago2geo_rdf_to_lpg.py \
  --input tools/yago2geo-rdf-to-lpg/sample/yago2geo_sample.nt \
  --output-dir /tmp/yago2geo_lpg_sample \
  --loader-root /path/visible/to/postgresql/yago2geo_lpg_sample \
  --graph-name yago2geo_sample \
  --overwrite
```

## Mapping Rules

| RDF pattern | LPG/AGE-Spatial output |
| --- | --- |
| RDF subject URI | Vertex `uri` property |
| `rdf:type` | Vertex label |
| Literal predicate | Vertex property |
| URI-object predicate | Edge |
| `geo:hasGeometry` + `geo:asWKT` | Physical `geom` column |
| `geo:sfWithin`, `geo:sfTouches`, etc. | Materialized spatial relation edge |

## Notes

- The sample file is intentionally small so users can inspect the generated
  CSV files by hand.
- The converter is dependency-free and uses only the Python standard library.
- The converter supports N-Triples and simple line-oriented Turtle triples. For
  complex Turtle syntax, convert RDF to N-Triples first.
- Full YAGO2Geo data is not included in this repository. Download the original
  dataset from the YAGO2Geo source and use the same script with your own input
  files.
