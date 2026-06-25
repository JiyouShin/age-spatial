LOAD 'age';
SET search_path = ag_catalog, public;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON)
SELECT *
FROM ag_catalog.cypher('test_kg'::name, $$
    MATCH
        (c1:Celebrity)-[:GRADUATED_FROM]->(h:HighSchool),
        (e:ElementarySchool)<-[:GRADUATED_FROM]-(c2:Celebrity)
    WHERE c1.name = 'REPLACE_WITH_CELEBRITY_NAME'
      AND id(c1) <> id(c2)
      AND e.geom && ST_Expand(h.geom, 1000.0 / 111320.0)
      AND ST_DWithin(h.geom, e.geom, 1000.0 / 111320.0)
    RETURN
        c1.name,
        h.place_name,
        c2.name,
        e.place_name,
        ST_Distance(h.geom, e.geom)
$$::cstring)
AS (
    celebrity1 agtype,
    high_school agtype,
    celebrity2 agtype,
    elementary_school agtype,
    distance_degree agtype
);
