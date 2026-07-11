# AGE-Spatial

**AGE-Spatial** is a PostgreSQL extension that extends
[Apache AGE](https://age.apache.org/) with PostGIS-backed spatial support for
property graphs.  It allows users to store `geometry` and `geography` values as
physical columns on AGE vertex and edge label tables, then call PostGIS spatial
functions directly inside AGE Cypher queries.

```sql
SELECT *
FROM ag_catalog.cypher('yago2geo', $$
    MATCH (county:OS_County), (lake:OSM_lake)
    WHERE ST_Within(lake.geom, county.geom)
    RETURN county.uri, count(lake)
    ORDER BY count(lake) DESC
    LIMIT 5
$$) AS (county agtype, lakes agtype);
```

## Why AGE-Spatial?

Graph databases are a natural fit for knowledge graphs, road networks,
administrative hierarchies, and place-to-place relationships.  Many real graph
workloads, however, also need spatial predicates and measurements:

- Which lakes are inside a county?
- Which roads cross a city boundary?
- Which villages touch both a forest and a lake?
- Which region has the largest area?

Most graph databases provide limited spatial functionality, or require spatial
data to be handled outside the graph query engine.  On the other hand, PostGIS
is mature, expressive, and heavily optimized for spatial computation.  The goal
of AGE-Spatial is to combine both worlds:

- Apache AGE for labeled property graph storage and Cypher queries.
- PostgreSQL for relational execution, indexing, and extension infrastructure.
- PostGIS for geometry/geography types, GiST indexes, and spatial functions.

## Apache AGE Background

Apache AGE is a PostgreSQL extension that adds labeled property graph support to
PostgreSQL.  AGE stores graph data in PostgreSQL tables and exposes graph
queries through openCypher-like syntax:

```sql
CREATE EXTENSION age_spatial;
LOAD 'age';
SET search_path = ag_catalog, public;

SELECT ag_catalog.create_graph('demo');

SELECT *
FROM ag_catalog.cypher('demo', $$
    CREATE (:Place {name: 'A'})-[:ROAD]->(:Place {name: 'B'})
    RETURN 1
$$) AS (result agtype);
```

AGE-Spatial is developed as an AGE fork whose PostgreSQL extension name is
`age_spatial`.  The shared library is still loaded as `age`, while the SQL
extension depends on PostGIS and installs the additional spatial catalog,
loader, and Cypher projection support.

## What AGE-Spatial Adds

AGE-Spatial keeps ordinary graph properties in AGE's `properties agtype` column
and stores spatial values in separate physical columns:

```text
demo."Place"
  id          graphid
  properties  agtype
  geom        geometry(Point, 4326)

demo."ROAD"
  id          graphid
  start_id    graphid
  end_id      graphid
  properties  agtype
  geom        geometry(LineString, 4326)
```

Main additions:

- **Spatial physical columns**
  - Add `geometry` or `geography` columns to AGE vertex/edge label tables.
  - Register spatial metadata in `ag_catalog.ag_spatial_columns`.
  - Optionally create GiST indexes during column registration.

- **PostGIS functions inside Cypher**
  - Registered spatial columns can be accessed as `n.geom`, `r.geom`, etc.
  - PostGIS functions such as `ST_Within`, `ST_Touches`, `ST_Intersects`,
    `ST_Area`, `ST_Distance`, and `ST_AsText` can be used inside Cypher.

- **Spatial-safe CRUD behavior**
  - AGE core columns remain protected.
  - User-managed spatial columns are preserved across Cypher `CREATE` and
    `SET` operations.
  - Spatial column names are reserved from normal property maps to avoid
    ambiguity between `properties->'geom'` and physical column `geom`.

- **Spatial CSV bulk loader**
  - Extended AGE's CSV loader so selected CSV columns can be loaded into
    physical spatial columns while non-spatial columns remain AGE properties.
  - Supports `geometry` and `geography`, SRID validation, metadata validation,
    optional auto-create, optional GiST index creation, and row-level WKT error
    reporting.

- **Compatibility and parser improvements**
  - Registered spatial fields can be projected from Cypher expressions.
  - Direct field access paths were adjusted so physical spatial columns can
    coexist with AGE's existing property access model.
  - Name handling was hardened around AGE catalog objects and spatial metadata.

## Public API

### Register Spatial Columns

```sql
SELECT ag_catalog.add_geometry_column(
    'demo',          -- graph name
    'Place',         -- vertex or edge label
    'geom',          -- physical column name
    'Point',         -- PostGIS geometry subtype
    4326,            -- SRID
    true             -- create GiST index
);

SELECT ag_catalog.add_geography_column(
    'demo',
    'Place',
    'geog',
    'Point',
    4326,
    true
);
```

### Set and Clear Spatial Values

```sql
SELECT ag_catalog.set_geometry_value(
    'demo',
    'Place',
    'geom',
    '844424930131969'::ag_catalog.graphid,
    public.ST_SetSRID(public.ST_MakePoint(127.0, 37.0), 4326)
);

SELECT ag_catalog.clear_spatial_value(
    'demo',
    'Place',
    'geom',
    '844424930131969'::ag_catalog.graphid
);
```

### Drop Spatial Columns

```sql
SELECT ag_catalog.drop_spatial_column('demo', 'Place', 'geom');
```

This drops the registered spatial column, its registered GiST index, and the
metadata row in `ag_catalog.ag_spatial_columns`.

## Spatial CSV Loader

AGE-Spatial adds spatial variants of AGE's label and edge CSV loaders:

```sql
SELECT ag_catalog.load_labels_with_spatial_from_file(
    'demo',
    'Place',
    '/absolute/path/Place.csv',
    true,
    true,
    '{
       "geom": {
         "type": "geometry",
         "geometry_type": "Point",
         "srid": 4326,
         "create_if_missing": true,
         "create_index": true
       }
     }'::jsonb
);
```

The `spatial_spec` argument tells the loader which CSV columns are spatial:

```json
{
  "geom": {
    "type": "geometry",
    "geometry_type": "Geometry",
    "srid": 4326,
    "create_if_missing": true,
    "create_index": true
  }
}
```

Loader behavior:

- The CSV header must contain every spatial column named in `spatial_spec`.
- `type` must be `geometry` or `geography`.
- `srid` defaults to `4326` when omitted.
- Spatial columns are loaded into physical columns.
- All other CSV columns are loaded into AGE `properties`.
- Existing spatial metadata must match graph, label, label kind, column name,
  spatial type, geometry subtype, and SRID.
- A physical spatial column without matching metadata is treated as an error.
- A property key that conflicts with a registered spatial column is rejected.
- Invalid WKT, SRID mismatch, and geometry/geography type mismatch are reported
  with row context.

Edge loading uses the same idea:

```sql
SELECT ag_catalog.load_edges_with_spatial_from_file(
    'demo',
    'ROAD',
    '/absolute/path/ROAD.csv',
    true,
    '{"geom": {"type": "geometry", "geometry_type": "LineString", "srid": 4326}}'::jsonb
);
```

## Query Examples

### Spatial Predicate in Cypher

```sql
SELECT *
FROM ag_catalog.cypher('demo', $$
    MATCH (p:Place), (a:Area)
    WHERE ST_Within(p.geom, a.geom)
    RETURN p.name, a.name
$$) AS (place agtype, area agtype);
```

### Spatial Measurement in Cypher

```sql
SELECT *
FROM ag_catalog.cypher('demo', $$
    MATCH (a:Area)
    RETURN a.name, ST_Area(a.geom) AS area
    ORDER BY area DESC
    LIMIT 10
$$) AS (name agtype, area agtype);
```

### Inspect Registered Spatial Columns

```sql
SELECT graph_name, label_name, label_kind, column_name,
       spatial_type, geometry_type, srid, index_name
FROM ag_catalog.ag_spatial_columns
ORDER BY graph_name, label_name, column_name;
```

## Evaluation Summary

AGE-Spatial was evaluated with the YAGO2Geo geospatial knowledge graph and
GeoQuestions1089.  The original RDF triples were transformed into an AGE
labeled property graph while preserving the RDF-derived labels, URIs,
properties, topological edges, and WKT geometries as physical PostGIS columns.

The first full experimental campaign focused on GeoQuestions1089 questions that
require spatial functions rather than precomputed spatial-relation edges.  A UK
subset was then used for a controlled comparison across AGE-Spatial, Jena,
Virtuoso, and Strabon.

### UK Spatial-Function Benchmark

The table below summarizes the current UK subset run over 222 spatial-function
questions.  Times are milliseconds.  `OK` means the engine returned a result
within the timeout.  `match` means the returned answer matched the GeoQuestions
reference answer under the benchmark normalizer.

| Engine / configuration | Total | OK | Error | Timeout | HTTP error | Match | Mismatch | Not run | Avg OK ms | Median ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| AGE-Spatial with NO uri property index | 222 | 203 | 18 | 1 | 0 | 162 | 41 | 19 | 2220.907 | 633.824 |
| AGE-Spatial with uri property index | 222 | 206 | 15 | 1 | 0 | 165 | 41 | 16 | 1565.199 | 134.586 |
| Jena GeoSPARQL | 222 | 210 | 0 | 12 | 0 | 109 | 101 | 12 | 2522.745 | 224.573 |
| Virtuoso GeoSPARQL / BIF hybrid | 222 | 127 | 0 | 23 | 72 | 62 | 65 | 95 | 4307.255 | 215.166 |
| Strabon | 222 | 168 | 7 | 44 | 3 | 140 | 28 | 54 | 6216.581 | 1223.315 |

Notes:

- URI indexing improves AGE-Spatial lookup-heavy queries because RDF URIs are
  stored as AGE properties, while RDF stores usually maintain subject/object
  indexes internally.
- For the questions that completed successfully in both AGE configurations,
  match/mismatch classification was stable.  Differences in error counts were
  traced to invalid geometries whose `GEOS` exception behavior is
  plan-dependent.
- Some mismatches are caused by RDF-to-LPG modeling differences.  RDF can bind
  multiple geometry resources for the same URI, while the current AGE-Spatial
  LPG representation selects a physical geometry column per label table.
- Some GeoQuestions reference queries use functions or result shapes that are
  engine-sensitive, such as unordered `LIMIT`, invalid geometries, and
  source-dependent boundary predicates.

### Benchmark Assets

The benchmark conversion and execution scripts used for the experiments are
kept under `benchmark/scripts/`.  Large RDF inputs and generated benchmark
artifacts are not required to install or use AGE-Spatial.

## Requirements

The current development and benchmark environment is tested with:

- PostgreSQL `16.4`
- PostGIS `3.6.4`
- Apache AGE base version `1.7.0`
- Linux with PostgreSQL server development headers
- Build tools: `gcc`, `make`, `flex`, `bison`, `readline`, `zlib`

The extension may be portable to other PostgreSQL versions supported by the AGE
base, but PostgreSQL 16 is the tested target for the current implementation.

## Build and Install

Install PostgreSQL and PostGIS first, then build AGE-Spatial with PGXS:

```bash
git clone https://github.com/JiyouShin/age-spatial.git
cd age-spatial

make PG_CONFIG=/path/to/pg_config
sudo make PG_CONFIG=/path/to/pg_config install
```

Create a database and enable the extensions:

```sql
CREATE EXTENSION postgis;
CREATE EXTENSION age_spatial;

LOAD 'age';
SET search_path = ag_catalog, public;
```

For PostgreSQL clients, remember that `LOAD 'age'` is session-local.  It must be
run for each database session that calls `cypher()`.

## Testing

Run the spatial regression test:

```bash
make PG_CONFIG=/path/to/pg_config installcheck REGRESS=age_spatial
```

Run the full extension regression suite:

```bash
make PG_CONFIG=/path/to/pg_config installcheck
```

The AGE-Spatial-specific regression covers:

- geometry/geography columns on vertex and edge labels
- metadata registration in `ag_spatial_columns`
- GiST index creation
- spatial value set/get/clear/drop APIs
- Cypher projection of registered spatial physical columns
- conflict checks between spatial columns and AGE property keys
- preservation of spatial columns across Cypher updates
- metadata cleanup after direct SQL column removal

## Citation

A paper describing AGE-Spatial, the YAGO2Geo transformation, and the
GeoQuestions1089 benchmark evaluation is in preparation.

```bibtex
@article{age_spatial_preprint,
  title   = {AGE-Spatial: Integrating PostGIS Spatial Operations into Property Graph Queries},
  author  = {TBD},
  journal = {TBD},
  year    = {TBD},
  note    = {Manuscript in preparation}
}
```

Paper link: **TBD**

## License

This project is based on Apache AGE and follows the Apache License 2.0 terms
used by the upstream AGE codebase.  See `LICENSE` and `NOTICE` for details.
