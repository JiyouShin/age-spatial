\timing on

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS age_spatial;

LOAD 'age';
SET search_path = ag_catalog, public;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM ag_catalog.ag_graph
        WHERE name = 'spatial_bench_smoke'
    ) THEN
        PERFORM ag_catalog.drop_graph('spatial_bench_smoke', true);
    END IF;
END
$$;

SELECT ag_catalog.create_graph('spatial_bench_smoke');
SELECT ag_catalog.create_vlabel('spatial_bench_smoke', 'Anchor');
SELECT ag_catalog.create_vlabel('spatial_bench_smoke', 'Place');

SELECT ag_catalog.add_geometry_column(
    'spatial_bench_smoke',
    'Anchor',
    'geom',
    'Point',
    4326,
    true
);

SELECT ag_catalog.add_geometry_column(
    'spatial_bench_smoke',
    'Place',
    'geom',
    'Point',
    4326,
    true
);

SELECT *
FROM ag_catalog.cypher('spatial_bench_smoke'::name, $$
    CREATE
        (:Anchor {name: 'origin'}),
        (:Place {name: 'near'}),
        (:Place {name: 'far'})
    RETURN count(*)
$$::cstring)
AS (created agtype);

SELECT ag_catalog.set_spatial_value(
    'spatial_bench_smoke',
    'Anchor',
    'geom',
    id,
    public.ST_SetSRID(public.ST_MakePoint(127.0000, 37.0000), 4326)
)
FROM spatial_bench_smoke."Anchor"
WHERE properties::text LIKE '%origin%';

SELECT ag_catalog.set_spatial_value(
    'spatial_bench_smoke',
    'Place',
    'geom',
    id,
    public.ST_SetSRID(public.ST_MakePoint(127.0004, 37.0004), 4326)
)
FROM spatial_bench_smoke."Place"
WHERE properties::text LIKE '%near%';

SELECT ag_catalog.set_spatial_value(
    'spatial_bench_smoke',
    'Place',
    'geom',
    id,
    public.ST_SetSRID(public.ST_MakePoint(128.0000, 38.0000), 4326)
)
FROM spatial_bench_smoke."Place"
WHERE properties::text LIKE '%far%';

SELECT
    schemaname,
    tablename,
    indexname
FROM pg_indexes
WHERE schemaname = 'spatial_bench_smoke'
  AND indexname LIKE '%geom%'
ORDER BY indexname;

SELECT *
FROM ag_catalog.cypher('spatial_bench_smoke'::name, $$
    RETURN ST_AsText(ST_SetSRID(ST_MakePoint(127.0, 37.0), 4326))
$$::cstring)
AS (wkt agtype);

SELECT *
FROM ag_catalog.cypher('spatial_bench_smoke'::name, $$
    MATCH (a:Anchor), (p:Place)
    WHERE p.geom && ST_Expand(a.geom, 0.01)
      AND ST_DWithin(a.geom, p.geom, 0.01)
    RETURN
        a.name,
        p.name,
        ST_AsText(a.geom),
        ST_AsText(p.geom)
$$::cstring)
AS (
    anchor_name agtype,
    place_name agtype,
    anchor_geom agtype,
    place_geom agtype
);

SET enable_seqscan = off;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT id
FROM spatial_bench_smoke."Place"
WHERE geom && public.ST_MakeEnvelope(126.99, 36.99, 127.01, 37.01, 4326);

RESET enable_seqscan;
