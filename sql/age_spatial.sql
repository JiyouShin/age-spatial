/*
 * Spatial column support for AGE label tables.
 *
 * These APIs manage PostGIS geometry/geography columns on AGE vertex and edge
 * label tables. AGE still owns its core storage columns, while registered
 * spatial columns are user-managed physical columns.
 */

CREATE TABLE ag_catalog.ag_spatial_columns (
    graph_name name NOT NULL,
    label_name name NOT NULL,
    label_kind char(1) NOT NULL CHECK (label_kind IN ('v', 'e')),
    relation_oid oid NOT NULL,
    column_name name NOT NULL,
    spatial_type text NOT NULL CHECK (spatial_type IN ('geometry', 'geography')),
    geometry_type text,
    srid integer,
    index_name name,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_name, label_name, column_name)
);

SELECT pg_catalog.pg_extension_config_dump('ag_spatial_columns', '');

CREATE FUNCTION ag_catalog._validate_spatial_column_args(
    p_column_name name,
    p_spatial_type text,
    p_geometry_type text,
    p_srid integer
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_column_name IS NULL OR p_column_name::text = '' THEN
        RAISE EXCEPTION 'spatial column name must not be empty';
    END IF;

    IF p_column_name IN ('id', 'properties', 'start_id', 'end_id') THEN
        RAISE EXCEPTION 'column name "%" is reserved by AGE', p_column_name;
    END IF;

    IF p_spatial_type NOT IN ('geometry', 'geography') THEN
        RAISE EXCEPTION 'spatial type must be geometry or geography: %',
            p_spatial_type;
    END IF;

    IF p_geometry_type IS NULL OR p_geometry_type !~ '^[A-Za-z][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'invalid geometry/geography subtype: %',
            p_geometry_type;
    END IF;

    IF p_srid IS NULL OR p_srid < 0 THEN
        RAISE EXCEPTION 'SRID must be a non-negative integer: %', p_srid;
    END IF;
END;
$$;

CREATE FUNCTION ag_catalog._get_label_relation(
    p_graph_name name,
    p_label_name name,
    OUT relation_oid oid,
    OUT label_kind char(1)
)
RETURNS record
LANGUAGE plpgsql
AS $$
BEGIN
    SELECT l.relation::oid, l.kind
    INTO relation_oid, label_kind
    FROM ag_catalog.ag_label l
    JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
    WHERE g.name = p_graph_name
      AND l.name = p_label_name;

    IF relation_oid IS NULL THEN
        RAISE EXCEPTION 'AGE label "%.%" does not exist',
            p_graph_name, p_label_name;
    END IF;
END;
$$;

CREATE FUNCTION ag_catalog._add_spatial_column(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_spatial_type text,
    p_geometry_type text,
    p_srid integer,
    p_create_index boolean
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_relation_oid oid;
    v_label_kind char(1);
    v_relation regclass;
    v_index_name name;
BEGIN
    PERFORM ag_catalog._validate_spatial_column_args(
        p_column_name,
        p_spatial_type,
        p_geometry_type,
        p_srid
    );

    SELECT relation_oid, label_kind
    INTO v_relation_oid, v_label_kind
    FROM ag_catalog._get_label_relation(p_graph_name, p_label_name);

    v_relation := v_relation_oid::regclass;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_attribute
        WHERE attrelid = v_relation_oid
          AND attname = p_column_name
          AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION 'column "%" already exists on %',
            p_column_name, v_relation;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM ag_catalog.ag_spatial_columns
        WHERE graph_name = p_graph_name
          AND label_name = p_label_name
          AND column_name = p_column_name
    ) THEN
        RAISE EXCEPTION 'spatial column "%.%.%" is already registered',
            p_graph_name, p_label_name, p_column_name;
    END IF;

    IF p_spatial_type = 'geometry' THEN
        EXECUTE format(
            'ALTER TABLE %s ADD COLUMN %I @extschema:postgis@.geometry(%s, %s)',
            v_relation,
            p_column_name,
            p_geometry_type,
            p_srid
        );
    ELSE
        EXECUTE format(
            'ALTER TABLE %s ADD COLUMN %I @extschema:postgis@.geography(%s, %s)',
            v_relation,
            p_column_name,
            p_geometry_type,
            p_srid
        );
    END IF;

    IF p_create_index THEN
        v_index_name := left(
            lower(p_label_name::text || '_' || p_column_name::text || '_gix'),
            63
        )::name;

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %s USING gist (%I)',
            v_index_name,
            v_relation,
            p_column_name
        );
    END IF;

    INSERT INTO ag_catalog.ag_spatial_columns (
        graph_name,
        label_name,
        label_kind,
        relation_oid,
        column_name,
        spatial_type,
        geometry_type,
        srid,
        index_name
    )
    VALUES (
        p_graph_name,
        p_label_name,
        v_label_kind,
        v_relation_oid,
        p_column_name,
        p_spatial_type,
        p_geometry_type,
        p_srid,
        v_index_name
    );
END;
$$;

CREATE FUNCTION ag_catalog.add_geometry_column(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_geometry_type text DEFAULT 'Geometry',
    p_srid integer DEFAULT 4326,
    p_create_index boolean DEFAULT true
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM ag_catalog._add_spatial_column(
        p_graph_name,
        p_label_name,
        p_column_name,
        'geometry',
        p_geometry_type,
        p_srid,
        p_create_index
    );
END;
$$;

CREATE FUNCTION ag_catalog.add_geography_column(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_geography_type text DEFAULT 'Geometry',
    p_srid integer DEFAULT 4326,
    p_create_index boolean DEFAULT true
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM ag_catalog._add_spatial_column(
        p_graph_name,
        p_label_name,
        p_column_name,
        'geography',
        p_geography_type,
        p_srid,
        p_create_index
    );
END;
$$;

CREATE FUNCTION ag_catalog.set_geometry_value(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_entity_id ag_catalog.graphid,
    p_value @extschema:postgis@.geometry
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_relation regclass;
    v_srid integer;
    v_affected integer;
BEGIN
    SELECT relation_oid::regclass, srid
    INTO v_relation, v_srid
    FROM ag_catalog.ag_spatial_columns
    WHERE graph_name = p_graph_name
      AND label_name = p_label_name
      AND column_name = p_column_name
      AND spatial_type = 'geometry';

    IF v_relation IS NULL THEN
        RAISE EXCEPTION 'registered geometry column "%.%.%" not found',
            p_graph_name, p_label_name, p_column_name;
    END IF;

    IF p_value IS NOT NULL AND @extschema:postgis@.ST_SRID(p_value) <> v_srid THEN
        RAISE EXCEPTION 'SRID mismatch for %.%.%: expected %, got %',
            p_graph_name,
            p_label_name,
            p_column_name,
            v_srid,
            @extschema:postgis@.ST_SRID(p_value);
    END IF;

    EXECUTE format('UPDATE %s SET %I = $1 WHERE id = $2', v_relation, p_column_name)
    USING p_value, p_entity_id;

    GET DIAGNOSTICS v_affected = ROW_COUNT;

    IF v_affected <> 1 THEN
        RAISE EXCEPTION 'target AGE entity not found: %', p_entity_id;
    END IF;
END;
$$;

CREATE FUNCTION ag_catalog.set_geography_value(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_entity_id ag_catalog.graphid,
    p_value @extschema:postgis@.geography
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_relation regclass;
    v_srid integer;
    v_affected integer;
BEGIN
    SELECT relation_oid::regclass, srid
    INTO v_relation, v_srid
    FROM ag_catalog.ag_spatial_columns
    WHERE graph_name = p_graph_name
      AND label_name = p_label_name
      AND column_name = p_column_name
      AND spatial_type = 'geography';

    IF v_relation IS NULL THEN
        RAISE EXCEPTION 'registered geography column "%.%.%" not found',
            p_graph_name, p_label_name, p_column_name;
    END IF;

    IF p_value IS NOT NULL AND @extschema:postgis@.ST_SRID(p_value) <> v_srid THEN
        RAISE EXCEPTION 'SRID mismatch for %.%.%: expected %, got %',
            p_graph_name,
            p_label_name,
            p_column_name,
            v_srid,
            @extschema:postgis@.ST_SRID(p_value);
    END IF;

    EXECUTE format('UPDATE %s SET %I = $1 WHERE id = $2', v_relation, p_column_name)
    USING p_value, p_entity_id;

    GET DIAGNOSTICS v_affected = ROW_COUNT;

    IF v_affected <> 1 THEN
        RAISE EXCEPTION 'target AGE entity not found: %', p_entity_id;
    END IF;
END;
$$;

CREATE FUNCTION ag_catalog.clear_spatial_value(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_entity_id ag_catalog.graphid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_relation regclass;
    v_affected integer;
BEGIN
    SELECT relation_oid::regclass
    INTO v_relation
    FROM ag_catalog.ag_spatial_columns
    WHERE graph_name = p_graph_name
      AND label_name = p_label_name
      AND column_name = p_column_name;

    IF v_relation IS NULL THEN
        RAISE EXCEPTION 'registered spatial column "%.%.%" not found',
            p_graph_name, p_label_name, p_column_name;
    END IF;

    EXECUTE format('UPDATE %s SET %I = NULL WHERE id = $1', v_relation, p_column_name)
    USING p_entity_id;

    GET DIAGNOSTICS v_affected = ROW_COUNT;

    IF v_affected <> 1 THEN
        RAISE EXCEPTION 'target AGE entity not found: %', p_entity_id;
    END IF;
END;
$$;

CREATE FUNCTION ag_catalog.drop_spatial_column(
    p_graph_name name,
    p_label_name name,
    p_column_name name
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_relation regclass;
    v_index_name name;
    v_schema_name name;
BEGIN
    SELECT relation_oid::regclass, index_name
    INTO v_relation, v_index_name
    FROM ag_catalog.ag_spatial_columns
    WHERE graph_name = p_graph_name
      AND label_name = p_label_name
      AND column_name = p_column_name;

    IF v_relation IS NULL THEN
        RAISE EXCEPTION 'registered spatial column "%.%.%" not found',
            p_graph_name, p_label_name, p_column_name;
    END IF;

    SELECT n.nspname
    INTO v_schema_name
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.oid = v_relation::oid;

    IF v_index_name IS NOT NULL THEN
        EXECUTE format('DROP INDEX IF EXISTS %I.%I', v_schema_name, v_index_name);
    END IF;

    EXECUTE format('ALTER TABLE %s DROP COLUMN IF EXISTS %I', v_relation, p_column_name);

    DELETE FROM ag_catalog.ag_spatial_columns
    WHERE graph_name = p_graph_name
      AND label_name = p_label_name
      AND column_name = p_column_name;
END;
$$;
