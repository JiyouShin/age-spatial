LOAD 'age';
SET search_path TO ag_catalog, public;
SELECT ag_catalog.load_edges_from_file('age_spatial_system_metrics', 'TOUCHES', 'age_spatial_system_metrics/edges/TOUCHES.csv', true);
SELECT ag_catalog.load_edges_from_file('age_spatial_system_metrics', 'WITHIN', 'age_spatial_system_metrics/edges/WITHIN.csv', true);
