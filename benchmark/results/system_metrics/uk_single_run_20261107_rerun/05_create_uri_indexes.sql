LOAD 'age';
SET search_path TO ag_catalog, public;
DO $$
DECLARE
    r record;
    uri_idx text;
    yago_uri_idx text;
BEGIN
    FOR r IN
        SELECT l.name AS relname
        FROM ag_catalog.ag_label l
        JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
        WHERE g.name = 'age_spatial_system_metrics' AND l.kind = 'v'
        ORDER BY l.name
    LOOP
        uri_idx := 'age_uri_' || substr(md5('age_spatial_system_metrics' || '.' || r.relname || '.uri'), 1, 24);
        yago_uri_idx := 'age_yago_uri_' || substr(md5('age_spatial_system_metrics' || '.' || r.relname || '.yago_uri'), 1, 19);
        EXECUTE format(
            'CREATE INDEX %I ON %I.%I (ag_catalog.agtype_access_operator(properties, %L::agtype))',
            uri_idx, 'age_spatial_system_metrics', r.relname, '"uri"'
        );
        EXECUTE format(
            'CREATE INDEX %I ON %I.%I (ag_catalog.agtype_access_operator(properties, %L::agtype))',
            yago_uri_idx, 'age_spatial_system_metrics', r.relname, '"yago_uri"'
        );
    END LOOP;
END
$$;
