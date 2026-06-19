/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

LOAD 'age';
SET search_path TO ag_catalog, public;

SELECT create_graph('spatial_api_test');
SELECT create_vlabel('spatial_api_test', 'Place');
SELECT create_vlabel('spatial_api_test', 'ConflictPlace');
SELECT create_elabel('spatial_api_test', 'ROAD');

-- A spatial physical column must not reuse an existing top-level property key.
SELECT *
FROM cypher('spatial_api_test', $$
    CREATE (:ConflictPlace {geom: 'legacy'})
    RETURN count(*)
$$) AS (created agtype);

SELECT add_geometry_column('spatial_api_test', 'ConflictPlace', 'geom', 'Point', 4326, true);

-- Register spatial columns on both vertex and edge label tables.
SELECT add_geometry_column('spatial_api_test', 'Place', 'geom', 'Point', 4326, true);
SELECT add_geography_column('spatial_api_test', 'Place', 'geog', 'Point', 4326, true);
SELECT add_geometry_column('spatial_api_test', 'ROAD', 'geom', 'LineString', 4326, true);
SELECT add_geography_column('spatial_api_test', 'ROAD', 'geog', 'LineString', 4326, true);

-- This column is dropped through normal SQL first, then cleaned up through the
-- age_spatial API to validate both paths.
SELECT add_geometry_column('spatial_api_test', 'Place', 'sql_drop_geom', 'Point', 4326, true);

SELECT label_name, column_name, spatial_type, geometry_type, srid,
       index_name IS NOT NULL AS has_index
FROM ag_spatial_columns
WHERE graph_name = 'spatial_api_test'
ORDER BY label_name, column_name;

CREATE TEMP TABLE spatial_api_ids(kind text PRIMARY KEY, id graphid);

WITH created AS (
    SELECT *
    FROM cypher('spatial_api_test', $$
        CREATE (a:Place {name: 'A'})-[r:ROAD {name: 'A to B'}]->(b:Place {name: 'B'})
        RETURN id(a), id(b), id(r)
    $$) AS (a_id agtype, b_id agtype, road_id agtype)
)
INSERT INTO spatial_api_ids(kind, id)
SELECT 'place_a', (a_id::text)::graphid FROM created
UNION ALL
SELECT 'place_b', (b_id::text)::graphid FROM created
UNION ALL
SELECT 'road_ab', (road_id::text)::graphid FROM created;

SELECT set_spatial_value(
               'spatial_api_test',
               'Place',
               'geom',
               id,
               public.ST_SetSRID(public.ST_MakePoint(127.0, 37.0), 4326)
       )
FROM spatial_api_ids
WHERE kind = 'place_a';

SELECT set_geometry_value(
    'spatial_api_test',
    'Place',
    'geom',
    id,
    public.ST_SetSRID(public.ST_MakePoint(127.1, 37.1), 4326)
)
FROM spatial_api_ids
WHERE kind = 'place_b';

SELECT set_spatial_value(
               'spatial_api_test',
               'Place',
               'geog',
               id,
               public.ST_SetSRID(public.ST_MakePoint(127.0, 37.0), 4326)::public.geography
       )
FROM spatial_api_ids
WHERE kind = 'place_a';

SELECT set_geography_value(
    'spatial_api_test',
    'Place',
    'geog',
    id,
    public.ST_SetSRID(public.ST_MakePoint(127.1, 37.1), 4326)::public.geography
)
FROM spatial_api_ids
WHERE kind = 'place_b';

SELECT set_geometry_value(
    'spatial_api_test',
    'ROAD',
    'geom',
    id,
    public.ST_SetSRID(
        public.ST_MakeLine(
            public.ST_MakePoint(127.0, 37.0),
            public.ST_MakePoint(127.1, 37.1)
        ),
        4326
    )
)
FROM spatial_api_ids
WHERE kind = 'road_ab';

SELECT set_geography_value(
    'spatial_api_test',
    'ROAD',
    'geog',
    id,
    public.ST_SetSRID(
        public.ST_MakeLine(
            public.ST_MakePoint(127.0, 37.0),
            public.ST_MakePoint(127.1, 37.1)
        ),
        4326
    )::public.geography
)
FROM spatial_api_ids
WHERE kind = 'road_ab';

SELECT (properties::text)::jsonb ->> 'name' AS name,
       public.ST_AsText(geom) AS geom_wkt,
       public.ST_AsText(geog::public.geometry) AS geog_wkt,
       public.ST_SRID(geom) AS geom_srid,
       public.ST_SRID(geog::public.geometry) AS geog_srid
FROM spatial_api_test."Place"
ORDER BY name;

SELECT count(*) AS road_count,
       count(geom) AS road_geom_count,
       count(geog) AS road_geog_count,
       string_agg(public.ST_AsText(geom), ',' ORDER BY id) AS road_geom_wkt
FROM spatial_api_test."ROAD";

-- Cypher should be able to project registered spatial physical columns.
SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (p:Place)
    RETURN p.name, st_astext(p.geom)
    ORDER BY p.name
$$) AS (name agtype, geom_wkt agtype);

-- Registered spatial column names are reserved from properties.
SELECT *
FROM cypher('spatial_api_test', $$
    CREATE (:Place {geom: 'shadow'})
    RETURN count(*)
$$) AS (created agtype);

SELECT *
FROM cypher('spatial_api_test', $$
    CREATE (:Place {name: 'C'})-[:ROAD {geom: 'shadow'}]->(:Place {name: 'D'})
    RETURN count(*)
$$) AS (created agtype);

SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (p:Place {geom: 'shadow'})
    RETURN count(p)
$$) AS (matched agtype);

SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (p:Place)
    SET p.geom = 'shadow'
    RETURN count(p)
$$) AS (updated agtype);

SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (:Place)-[r:ROAD]->(:Place)
    SET r.geom = 'shadow'
    RETURN count(r)
$$) AS (updated agtype);

SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (p:Place)
    SET p = {geom: 'shadow'}
    RETURN count(p)
$$) AS (updated agtype);

SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (p:Place)
    SET p = properties(p)
    RETURN count(p)
$$) AS (updated agtype);

SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (p:Place)
    REMOVE p.geom
    RETURN count(p)
$$) AS (updated agtype);

-- Re-setting a spatial value is the update path for spatial columns.
SELECT set_geometry_value(
    'spatial_api_test',
    'Place',
    'geom',
    id,
    public.ST_SetSRID(public.ST_MakePoint(128.0, 38.0), 4326)
)
FROM spatial_api_ids
WHERE kind = 'place_a';

-- Cypher SET must preserve user-managed geometry/geography columns.
SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (p:Place)
    SET p.checked = true
    RETURN count(p)
$$) AS (updated agtype);

SELECT *
FROM cypher('spatial_api_test', $$
    MATCH (:Place)-[r:ROAD]->(:Place)
    SET r.checked = true
    RETURN count(r)
$$) AS (updated agtype);

SELECT (properties::text)::jsonb ->> 'name' AS name,
       ((properties::text)::jsonb ->> 'checked')::boolean AS checked,
       public.ST_AsText(geom) AS geom_wkt,
       public.ST_AsText(geog::public.geometry) AS geog_wkt
FROM spatial_api_test."Place"
ORDER BY name;

SELECT count(*) AS road_count,
       count(geom) AS road_geom_count,
       count(geog) AS road_geog_count,
       bool_and(((properties::text)::jsonb ->> 'checked')::boolean) AS road_checked
FROM spatial_api_test."ROAD";

-- clear_spatial_value() should set the target value to NULL without touching
-- the other spatial column.
SELECT clear_spatial_value('spatial_api_test', 'Place', 'geog', id)
FROM spatial_api_ids
WHERE kind = 'place_b';

SELECT count(geom) AS place_geom_count,
       count(geog) AS place_geog_count
FROM spatial_api_test."Place";

-- Validate normal SQL DROP COLUMN is allowed for non-AGE columns, then use the
-- API to remove the stale metadata left by direct SQL.
ALTER TABLE spatial_api_test."Place" DROP COLUMN sql_drop_geom;
SELECT drop_spatial_column('spatial_api_test', 'Place', 'sql_drop_geom');

SELECT drop_spatial_column('spatial_api_test', 'Place', 'geog');
SELECT drop_spatial_column('spatial_api_test', 'Place', 'geom');
SELECT drop_spatial_column('spatial_api_test', 'ROAD', 'geog');
SELECT drop_spatial_column('spatial_api_test', 'ROAD', 'geom');

SELECT count(*) AS remaining_spatial_columns
FROM ag_spatial_columns
WHERE graph_name = 'spatial_api_test';

SELECT table_name,
       string_agg(column_name, ',' ORDER BY ordinal_position) AS columns
FROM information_schema.columns
WHERE table_schema = 'spatial_api_test'
  AND table_name IN ('Place', 'ROAD')
GROUP BY table_name
ORDER BY table_name;

DROP TABLE spatial_api_ids;
SELECT drop_graph('spatial_api_test', true);
