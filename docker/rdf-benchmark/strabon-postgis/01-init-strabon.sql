CREATE EXTENSION IF NOT EXISTS postgis;

GRANT ALL ON geometry_columns TO PUBLIC;
GRANT ALL ON geography_columns TO PUBLIC;
GRANT ALL ON spatial_ref_sys TO PUBLIC;
