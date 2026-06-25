CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS age_spatial;

ALTER DATABASE age_spatial_bench SET search_path = ag_catalog, public;

LOAD 'age';
SET search_path = ag_catalog, public;

SELECT
    current_database() AS database_name,
    current_setting('server_version') AS postgres_version,
    postgis_full_version() AS postgis_version;
