LOAD 'age';
SET search_path TO ag_catalog, public;
DO $$
DECLARE
    r record;
    v_index_name name;
BEGIN
    FOR r IN
        SELECT graph_name, label_name, relation_oid::regclass AS relation_name,
               column_name
        FROM ag_catalog.ag_spatial_columns
        WHERE graph_name = 'age_spatial_system_metrics'
        ORDER BY label_name, column_name
    LOOP
        v_index_name := left(lower(r.label_name::text || '_' ||
                                   r.column_name::text || '_gix'), 63)::name;
        EXECUTE format('CREATE INDEX %I ON %s USING gist (%I)',
                       v_index_name, r.relation_name, r.column_name);
        UPDATE ag_catalog.ag_spatial_columns
        SET index_name = v_index_name
        WHERE graph_name = r.graph_name
          AND label_name = r.label_name
          AND column_name = r.column_name;
    END LOOP;
END
$$;
