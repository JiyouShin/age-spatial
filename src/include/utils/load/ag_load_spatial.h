/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.
 */

#ifndef AG_LOAD_SPATIAL_H
#define AG_LOAD_SPATIAL_H

#include "postgres.h"

#include "fmgr.h"
#include "utils/jsonb.h"

#include "utils/load/age_load.h"

typedef struct spatial_load_column
{
    char *column_name;
    char *spatial_type;
    char *geometry_type;
    char *format;
    int32 srid;
    bool create_if_missing;
    bool create_index;

    int header_index;
    AttrNumber attnum;
    Oid atttypid;
    Oid typinput;
    Oid typioparam;
    int32 atttypmod;
} spatial_load_column;

spatial_load_column *parse_spatial_load_spec(Jsonb *spatial_spec,
                                             int *num_columns);

void ensure_spatial_columns_for_load(char *graph_name,
                                     char *label_name,
                                     char label_kind,
                                     spatial_load_column *columns,
                                     int num_columns);

int create_labels_with_spatial_from_csv_file(char *file_path,
                                             char *graph_name,
                                             Oid graph_oid,
                                             char *label_name,
                                             int label_id,
                                             bool id_field_exists,
                                             bool load_as_agtype,
                                             spatial_load_column *columns,
                                             int num_columns);

int create_edges_with_spatial_from_csv_file(char *file_path,
                                            char *graph_name,
                                            Oid graph_oid,
                                            char *label_name,
                                            int label_id,
                                            bool load_as_agtype,
                                            spatial_load_column *columns,
                                            int num_columns);

#endif /* AG_LOAD_SPATIAL_H */
