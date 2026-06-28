/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.
 */

#include "postgres.h"

#include <errno.h>
#include <limits.h>

#include "access/table.h"
#include "catalog/namespace.h"
#include "catalog/pg_type.h"
#include "commands/copy.h"
#include "executor/executor.h"
#include "executor/spi.h"
#include "fmgr.h"
#include "nodes/makefuncs.h"
#include "parser/parse_node.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"

#include "utils/load/ag_load_spatial.h"

static List *create_copy_options(void);
static bool *bind_spatial_columns(char **header, int header_count,
                                  char *label_name, Oid graph_oid,
                                  spatial_load_column *columns,
                                  int num_columns,
                                  bool id_field_exists,
                                  bool is_edge);
static void clear_slot_values(TupleTableSlot *slot);
static int64 parse_int64_field(char *raw_value, const char *field_name,
                               int64 row_number);
static Datum parse_spatial_datum(spatial_load_column *column, char *raw_value,
                                 int64 row_number, bool *isnull);
static void process_vertex_spatial_row(char **fields, int nfields,
                                       char **header, int header_count,
                                       bool *skip, int label_id,
                                       Oid label_seq_relid,
                                       bool id_field_exists,
                                       bool load_as_agtype,
                                       int64 *curr_seq_num,
                                       batch_insert_state *batch_state,
                                       spatial_load_column *columns,
                                       int num_columns,
                                       int64 row_number);
static void process_edge_spatial_row(char **fields, int nfields,
                                     char **header, int header_count,
                                     bool *skip, int label_id,
                                     Oid label_seq_relid,
                                     Oid graph_oid,
                                     bool load_as_agtype,
                                     batch_insert_state *batch_state,
                                     spatial_load_column *columns,
                                     int num_columns,
                                     int64 row_number);

spatial_load_column *parse_spatial_load_spec(Jsonb *spatial_spec,
                                             int *num_columns)
{
    spatial_load_column *columns = NULL;
    MemoryContext caller_context = CurrentMemoryContext;
    uint64 processed;
    uint64 i;

    *num_columns = 0;

    if (spatial_spec == NULL)
    {
        return NULL;
    }

    if (!JB_ROOT_IS_OBJECT(spatial_spec))
    {
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                        errmsg("spatial_spec must be a JSON object")));
    }

    if (SPI_connect() != SPI_OK_CONNECT)
    {
        elog(ERROR, "SPI_connect failed");
    }

    {
        Oid argtypes[1] = {JSONBOID};
        Datum values[1];
        int spi_rc;

        values[0] = JsonbPGetDatum(spatial_spec);

        spi_rc = SPI_execute_with_args(
            "SELECT key::text, "
            "       jsonb_typeof(value)::text, "
            "       lower(coalesce(nullif(value->>'type', ''), 'geometry')), "
            "       coalesce(nullif(value->>'geometry_type', ''), "
            "                nullif(value->>'subtype', ''), 'Geometry'), "
            "       coalesce((value->>'srid')::int, 4326), "
            "       coalesce((value->>'create_if_missing')::boolean, true), "
            "       coalesce((value->>'create_index')::boolean, true), "
            "       lower(coalesce(nullif(value->>'format', ''), 'wkt')) "
            "FROM jsonb_each($1)",
            1, argtypes, values, NULL, true, 0);

        if (spi_rc != SPI_OK_SELECT)
        {
            elog(ERROR, "failed to parse spatial_spec");
        }
    }

    processed = SPI_processed;
    if (processed == 0)
    {
        SPI_finish();
        return NULL;
    }

    if (processed > INT_MAX)
    {
        SPI_finish();
        ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
                        errmsg("too many spatial columns in spatial_spec")));
    }

    columns = MemoryContextAllocZero(caller_context,
                                     sizeof(spatial_load_column) * processed);

    for (i = 0; i < processed; i++)
    {
        HeapTuple tuple = SPI_tuptable->vals[i];
        TupleDesc tupdesc = SPI_tuptable->tupdesc;
        bool isnull;
        char *value_type;
        char *tmp;
        Datum datum;

        tmp = SPI_getvalue(tuple, tupdesc, 1);
        columns[i].column_name = MemoryContextStrdup(caller_context, tmp);

        value_type = SPI_getvalue(tuple, tupdesc, 2);
        if (value_type == NULL || strcmp(value_type, "object") != 0)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("spatial_spec entry \"%s\" must be a JSON object",
                                   columns[i].column_name)));
        }

        tmp = SPI_getvalue(tuple, tupdesc, 3);
        columns[i].spatial_type = MemoryContextStrdup(caller_context, tmp);
        if (strcmp(columns[i].spatial_type, "geometry") != 0 &&
            strcmp(columns[i].spatial_type, "geography") != 0)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("spatial type for column \"%s\" must be geometry or geography",
                                   columns[i].column_name)));
        }

        tmp = SPI_getvalue(tuple, tupdesc, 4);
        columns[i].geometry_type = MemoryContextStrdup(caller_context, tmp);

        datum = SPI_getbinval(tuple, tupdesc, 5, &isnull);
        if (isnull || DatumGetInt32(datum) < 0)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("SRID for column \"%s\" must be a non-negative integer",
                                   columns[i].column_name)));
        }
        columns[i].srid = DatumGetInt32(datum);

        datum = SPI_getbinval(tuple, tupdesc, 6, &isnull);
        columns[i].create_if_missing = isnull ? true : DatumGetBool(datum);

        datum = SPI_getbinval(tuple, tupdesc, 7, &isnull);
        columns[i].create_index = isnull ? true : DatumGetBool(datum);

        tmp = SPI_getvalue(tuple, tupdesc, 8);
        columns[i].format = MemoryContextStrdup(caller_context, tmp);
        if (strcmp(columns[i].format, "wkt") != 0 &&
            strcmp(columns[i].format, "ewkt") != 0)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("spatial format for column \"%s\" must be wkt or ewkt",
                                   columns[i].column_name)));
        }

        columns[i].header_index = -1;
        columns[i].attnum = InvalidAttrNumber;
    }

    SPI_finish();
    *num_columns = (int) processed;
    return columns;
}

void ensure_spatial_columns_for_load(char *graph_name,
                                     char *label_name,
                                     char label_kind,
                                     spatial_load_column *columns,
                                     int num_columns)
{
    int i;
    char label_kind_str[2];

    if (num_columns <= 0)
    {
        return;
    }

    label_kind_str[0] = label_kind;
    label_kind_str[1] = '\0';

    if (SPI_connect() != SPI_OK_CONNECT)
    {
        elog(ERROR, "SPI_connect failed");
    }

    for (i = 0; i < num_columns; i++)
    {
        Oid argtypes[9] = {
            TEXTOID, TEXTOID, TEXTOID, TEXTOID, TEXTOID,
            TEXTOID, INT4OID, BOOLOID, BOOLOID
        };
        Datum values[9];
        int spi_rc;

        values[0] = CStringGetTextDatum(graph_name);
        values[1] = CStringGetTextDatum(label_name);
        values[2] = CStringGetTextDatum(label_kind_str);
        values[3] = CStringGetTextDatum(columns[i].column_name);
        values[4] = CStringGetTextDatum(columns[i].spatial_type);
        values[5] = CStringGetTextDatum(columns[i].geometry_type);
        values[6] = Int32GetDatum(columns[i].srid);
        values[7] = BoolGetDatum(columns[i].create_if_missing);
        values[8] = BoolGetDatum(columns[i].create_index);

        spi_rc = SPI_execute_with_args(
            "SELECT ag_catalog._ensure_spatial_column_for_load("
            "  $1::name, $2::name, $3::text, $4::name, $5::text, "
            "  $6::text, $7::integer, $8::boolean, $9::boolean)",
            9, argtypes, values, NULL, false, 0);

        if (spi_rc != SPI_OK_SELECT)
        {
            elog(ERROR, "failed to ensure spatial column for CSV load");
        }
    }

    SPI_finish();
}

static List *create_copy_options(void)
{
    List *options = NIL;

    options = lappend(options,
                      makeDefElem("format",
                                  (Node *) makeString("csv"),
                                  -1));
    options = lappend(options,
                      makeDefElem("header",
                                  (Node *) makeBoolean(false),
                                  -1));

    return options;
}

static bool *bind_spatial_columns(char **header, int header_count,
                                  char *label_name, Oid graph_oid,
                                  spatial_load_column *columns,
                                  int num_columns,
                                  bool id_field_exists,
                                  bool is_edge)
{
    bool *skip = palloc0(sizeof(bool) * header_count);
    Oid relid = get_label_relation(label_name, graph_oid);
    Relation relation;
    TupleDesc tupdesc;
    int i;

    relation = table_open(relid, AccessShareLock);
    tupdesc = RelationGetDescr(relation);

    for (i = 0; i < num_columns; i++)
    {
        int j;
        int match_count = 0;
        int matched_index = -1;
        Form_pg_attribute attr;

        for (j = 0; j < header_count; j++)
        {
            if (header[j] != NULL &&
                strcmp(header[j], columns[i].column_name) == 0)
            {
                match_count++;
                matched_index = j;
            }
        }

        if (match_count == 0)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("CSV header does not contain spatial column \"%s\"",
                                   columns[i].column_name)));
        }

        if (match_count > 1)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("CSV header contains spatial column \"%s\" more than once",
                                   columns[i].column_name)));
        }

        if (!is_edge && id_field_exists && matched_index == 0)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("spatial column \"%s\" cannot be the vertex id column",
                                   columns[i].column_name)));
        }

        if (is_edge && matched_index < 4)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("spatial column \"%s\" cannot be one of the edge endpoint columns",
                                   columns[i].column_name)));
        }

        columns[i].header_index = matched_index;
        skip[matched_index] = true;

        columns[i].attnum = get_attnum(relid, columns[i].column_name);
        if (columns[i].attnum == InvalidAttrNumber)
        {
            ereport(ERROR, (errcode(ERRCODE_UNDEFINED_COLUMN),
                            errmsg("physical spatial column \"%s\" does not exist on label \"%s\"",
                                   columns[i].column_name, label_name)));
        }

        attr = TupleDescAttr(tupdesc, columns[i].attnum - 1);
        columns[i].atttypid = attr->atttypid;
        columns[i].atttypmod = attr->atttypmod;
        getTypeInputInfo(columns[i].atttypid,
                         &columns[i].typinput,
                         &columns[i].typioparam);
    }

    table_close(relation, AccessShareLock);
    return skip;
}

static void clear_slot_values(TupleTableSlot *slot)
{
    TupleDesc tupdesc = slot->tts_tupleDescriptor;

    ExecClearTuple(slot);
    memset(slot->tts_values, 0, sizeof(Datum) * tupdesc->natts);
    memset(slot->tts_isnull, true, sizeof(bool) * tupdesc->natts);
}

static int64 parse_int64_field(char *raw_value, const char *field_name,
                               int64 row_number)
{
    char *value = trim_whitespace(raw_value);
    char *endptr;
    int64 result;

    if (value[0] == '\0')
    {
        ereport(ERROR, (errcode(ERRCODE_INVALID_TEXT_REPRESENTATION),
                        errmsg("empty %s at CSV row %ld",
                               field_name, row_number)));
    }

    errno = 0;
    result = strtoll(value, &endptr, 10);
    if (errno != 0 || endptr == value || *endptr != '\0')
    {
        ereport(ERROR, (errcode(ERRCODE_INVALID_TEXT_REPRESENTATION),
                        errmsg("invalid %s at CSV row %ld: \"%s\"",
                               field_name, row_number, value)));
    }

    return result;
}

static Datum parse_spatial_datum(spatial_load_column *column, char *raw_value,
                                 int64 row_number, bool *isnull)
{
    char *value;
    char *ewkt;
    Datum result = (Datum) 0;

    *isnull = true;

    if (raw_value == NULL)
    {
        return result;
    }

    value = trim_whitespace(raw_value);
    if (value[0] == '\0')
    {
        return result;
    }

    if (pg_strncasecmp(value, "SRID=", 5) == 0)
    {
        char *semi = strchr(value + 5, ';');
        char *srid_text;
        char *endptr;
        long srid;

        if (semi == NULL)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_TEXT_REPRESENTATION),
                            errmsg("invalid EWKT SRID prefix at CSV row %ld, column \"%s\"",
                                   row_number, column->column_name)));
        }

        srid_text = pnstrdup(value + 5, semi - (value + 5));
        errno = 0;
        srid = strtol(srid_text, &endptr, 10);
        if (errno != 0 || endptr == srid_text || *endptr != '\0' || srid < 0)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_TEXT_REPRESENTATION),
                            errmsg("invalid EWKT SRID at CSV row %ld, column \"%s\"",
                                   row_number, column->column_name)));
        }

        if ((int32) srid != column->srid)
        {
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("SRID mismatch at CSV row %ld, column \"%s\": expected %d, got %ld",
                                   row_number, column->column_name,
                                   column->srid, srid)));
        }

        ewkt = value;
    }
    else
    {
        ewkt = psprintf("SRID=%d;%s", column->srid, value);
    }

    PG_TRY();
    {
        result = OidInputFunctionCall(column->typinput,
                                      ewkt,
                                      column->typioparam,
                                      column->atttypmod);
        *isnull = false;
    }
    PG_CATCH();
    {
        ErrorData *edata;
        MemoryContext oldcontext;

        oldcontext = MemoryContextSwitchTo(ErrorContext);
        edata = CopyErrorData();
        MemoryContextSwitchTo(oldcontext);
        FlushErrorState();

        ereport(ERROR, (errcode(ERRCODE_INVALID_TEXT_REPRESENTATION),
                        errmsg("invalid spatial value at CSV row %ld, column \"%s\": %s",
                               row_number, column->column_name,
                               edata->message)));
    }
    PG_END_TRY();

    return result;
}

static void process_vertex_spatial_row(char **fields, int nfields,
                                       char **header, int header_count,
                                       bool *skip, int label_id,
                                       Oid label_seq_relid,
                                       bool id_field_exists,
                                       bool load_as_agtype,
                                       int64 *curr_seq_num,
                                       batch_insert_state *batch_state,
                                       spatial_load_column *columns,
                                       int num_columns,
                                       int64 row_number)
{
    graphid vertex_id;
    int64 entry_id;
    TupleTableSlot *slot;
    agtype *vertex_properties;
    int i;

    if (nfields != header_count)
    {
        ereport(ERROR, (errcode(ERRCODE_BAD_COPY_FILE_FORMAT),
                        errmsg("CSV row %ld has %d fields, expected %d",
                               row_number, nfields, header_count)));
    }

    if (id_field_exists)
    {
        entry_id = parse_int64_field(fields[0], "vertex id", row_number);
        if (entry_id > *curr_seq_num)
        {
            DirectFunctionCall2(setval_oid,
                                ObjectIdGetDatum(label_seq_relid),
                                Int64GetDatum(entry_id));
            *curr_seq_num = entry_id;
        }
    }
    else
    {
        entry_id = nextval_internal(label_seq_relid, true);
    }

    vertex_id = make_graphid(label_id, entry_id);

    slot = batch_state->slots[batch_state->num_tuples];
    clear_slot_values(slot);

    vertex_properties = create_agtype_from_list_skip(header, fields,
                                                     nfields, entry_id,
                                                     load_as_agtype, skip);

    slot->tts_values[vertex_tuple_id] = GRAPHID_GET_DATUM(vertex_id);
    slot->tts_values[vertex_tuple_properties] =
        AGTYPE_P_GET_DATUM(vertex_properties);
    slot->tts_isnull[vertex_tuple_id] = false;
    slot->tts_isnull[vertex_tuple_properties] = false;

    for (i = 0; i < num_columns; i++)
    {
        bool spatial_isnull;
        Datum spatial_value;

        spatial_value = parse_spatial_datum(&columns[i],
                                            fields[columns[i].header_index],
                                            row_number,
                                            &spatial_isnull);
        if (!spatial_isnull)
        {
            slot->tts_values[columns[i].attnum - 1] = spatial_value;
            slot->tts_isnull[columns[i].attnum - 1] = false;
        }
    }

    ExecStoreVirtualTuple(slot);

    batch_state->buffered_bytes += VARSIZE(vertex_properties);
    batch_state->num_tuples++;

    if (batch_state->num_tuples >= BATCH_SIZE ||
        batch_state->buffered_bytes >= MAX_BUFFERED_BYTES)
    {
        insert_batch(batch_state);
        batch_state->num_tuples = 0;
        batch_state->buffered_bytes = 0;
    }
}

static void process_edge_spatial_row(char **fields, int nfields,
                                     char **header, int header_count,
                                     bool *skip, int label_id,
                                     Oid label_seq_relid,
                                     Oid graph_oid,
                                     bool load_as_agtype,
                                     batch_insert_state *batch_state,
                                     spatial_load_column *columns,
                                     int num_columns,
                                     int64 row_number)
{
    int64 start_id_int;
    graphid start_vertex_graph_id;
    int start_vertex_type_id;
    int64 end_id_int;
    graphid end_vertex_graph_id;
    int end_vertex_type_id;
    graphid edge_id;
    int64 entry_id;
    TupleTableSlot *slot;
    char *start_vertex_type;
    char *end_vertex_type;
    agtype *edge_properties;
    int i;

    if (nfields != header_count)
    {
        ereport(ERROR, (errcode(ERRCODE_BAD_COPY_FILE_FORMAT),
                        errmsg("CSV row %ld has %d fields, expected %d",
                               row_number, nfields, header_count)));
    }

    if (nfields < 4)
    {
        ereport(ERROR, (errcode(ERRCODE_BAD_COPY_FILE_FORMAT),
                        errmsg("CSV row %ld has fewer than 4 edge endpoint fields",
                               row_number)));
    }

    entry_id = nextval_internal(label_seq_relid, true);
    edge_id = make_graphid(label_id, entry_id);

    start_vertex_type = trim_whitespace(fields[1]);
    end_vertex_type = trim_whitespace(fields[3]);

    start_id_int = parse_int64_field(fields[0], "edge start id", row_number);
    start_vertex_type_id = get_label_id(start_vertex_type, graph_oid);
    if (!label_id_is_valid(start_vertex_type_id))
    {
        ereport(ERROR, (errcode(ERRCODE_UNDEFINED_TABLE),
                        errmsg("start vertex label \"%s\" at CSV row %ld does not exist",
                               start_vertex_type, row_number)));
    }

    end_id_int = parse_int64_field(fields[2], "edge end id", row_number);
    end_vertex_type_id = get_label_id(end_vertex_type, graph_oid);
    if (!label_id_is_valid(end_vertex_type_id))
    {
        ereport(ERROR, (errcode(ERRCODE_UNDEFINED_TABLE),
                        errmsg("end vertex label \"%s\" at CSV row %ld does not exist",
                               end_vertex_type, row_number)));
    }

    start_vertex_graph_id = make_graphid(start_vertex_type_id, start_id_int);
    end_vertex_graph_id = make_graphid(end_vertex_type_id, end_id_int);

    slot = batch_state->slots[batch_state->num_tuples];
    clear_slot_values(slot);

    edge_properties = create_agtype_from_list_i_skip(header, fields,
                                                     nfields, 4,
                                                     load_as_agtype, skip);

    slot->tts_values[edge_tuple_id] = GRAPHID_GET_DATUM(edge_id);
    slot->tts_values[edge_tuple_start_id] =
        GRAPHID_GET_DATUM(start_vertex_graph_id);
    slot->tts_values[edge_tuple_end_id] =
        GRAPHID_GET_DATUM(end_vertex_graph_id);
    slot->tts_values[edge_tuple_properties] =
        AGTYPE_P_GET_DATUM(edge_properties);
    slot->tts_isnull[edge_tuple_id] = false;
    slot->tts_isnull[edge_tuple_start_id] = false;
    slot->tts_isnull[edge_tuple_end_id] = false;
    slot->tts_isnull[edge_tuple_properties] = false;

    for (i = 0; i < num_columns; i++)
    {
        bool spatial_isnull;
        Datum spatial_value;

        spatial_value = parse_spatial_datum(&columns[i],
                                            fields[columns[i].header_index],
                                            row_number,
                                            &spatial_isnull);
        if (!spatial_isnull)
        {
            slot->tts_values[columns[i].attnum - 1] = spatial_value;
            slot->tts_isnull[columns[i].attnum - 1] = false;
        }
    }

    ExecStoreVirtualTuple(slot);

    batch_state->buffered_bytes += VARSIZE(edge_properties);
    batch_state->num_tuples++;

    if (batch_state->num_tuples >= BATCH_SIZE ||
        batch_state->buffered_bytes >= MAX_BUFFERED_BYTES)
    {
        insert_batch(batch_state);
        batch_state->num_tuples = 0;
        batch_state->buffered_bytes = 0;
    }
}

int create_labels_with_spatial_from_csv_file(char *file_path,
                                             char *graph_name,
                                             Oid graph_oid,
                                             char *label_name,
                                             int label_id,
                                             bool id_field_exists,
                                             bool load_as_agtype,
                                             spatial_load_column *columns,
                                             int num_columns)
{
    Relation        label_rel;
    Oid             label_relid;
    CopyFromState   cstate;
    List           *copy_options;
    ParseState     *pstate;
    char          **fields;
    int             nfields;
    char          **header = NULL;
    int             header_count = 0;
    bool           *skip = NULL;
    bool            is_first_row = true;
    char           *label_seq_name;
    Oid             label_seq_relid;
    int64           curr_seq_num = 0;
    int64           row_number = 0;
    batch_insert_state *batch_state = NULL;
    MemoryContext   batch_context;
    MemoryContext   old_context;

    (void) graph_name;

    batch_context = AllocSetContextCreate(CurrentMemoryContext,
                                          "AGE Spatial CSV Load Batch Context",
                                          ALLOCSET_DEFAULT_SIZES);

    label_relid = get_label_relation(label_name, graph_oid);
    label_rel = table_open(label_relid, RowExclusiveLock);
    label_seq_name = get_label_seq_relation_name(label_name);
    label_seq_relid = get_relname_relid(label_seq_name, graph_oid);

    if (id_field_exists)
    {
        curr_seq_num = nextval_internal(label_seq_relid, true);
    }

    init_batch_insert(&batch_state, label_name, graph_oid);
    copy_options = create_copy_options();
    pstate = make_parsestate(NULL);

    PG_TRY();
    {
        cstate = BeginCopyFrom(pstate, label_rel, NULL, file_path, false,
                               NULL, NIL, copy_options);

        while (NextCopyFromRawFields(cstate, &fields, &nfields))
        {
            if (is_first_row)
            {
                int i;

                header_count = nfields;
                header = (char **) palloc(sizeof(char *) * nfields);
                for (i = 0; i < nfields; i++)
                {
                    header[i] = trim_whitespace(fields[i]);
                }
                skip = bind_spatial_columns(header, header_count, label_name,
                                            graph_oid, columns, num_columns,
                                            id_field_exists, false);
                is_first_row = false;
            }
            else
            {
                row_number++;
                old_context = MemoryContextSwitchTo(batch_context);
                process_vertex_spatial_row(fields, nfields, header,
                                           header_count, skip, label_id,
                                           label_seq_relid, id_field_exists,
                                           load_as_agtype, &curr_seq_num,
                                           batch_state, columns, num_columns,
                                           row_number);
                MemoryContextSwitchTo(old_context);

                if (batch_state->num_tuples == 0)
                {
                    MemoryContextReset(batch_context);
                }
            }
        }

        finish_batch_insert(&batch_state);
        MemoryContextReset(batch_context);
        EndCopyFrom(cstate);
    }
    PG_FINALLY();
    {
        if (header != NULL)
        {
            int i;
            for (i = 0; i < header_count; i++)
            {
                pfree(header[i]);
            }
            pfree(header);
        }

        table_close(label_rel, RowExclusiveLock);
        MemoryContextDelete(batch_context);
        free_parsestate(pstate);
    }
    PG_END_TRY();

    return EXIT_SUCCESS;
}

int create_edges_with_spatial_from_csv_file(char *file_path,
                                            char *graph_name,
                                            Oid graph_oid,
                                            char *label_name,
                                            int label_id,
                                            bool load_as_agtype,
                                            spatial_load_column *columns,
                                            int num_columns)
{
    Relation        label_rel;
    Oid             label_relid;
    CopyFromState   cstate;
    List           *copy_options;
    ParseState     *pstate;
    char          **fields;
    int             nfields;
    char          **header = NULL;
    int             header_count = 0;
    bool           *skip = NULL;
    bool            is_first_row = true;
    char           *label_seq_name;
    Oid             label_seq_relid;
    int64           row_number = 0;
    batch_insert_state *batch_state = NULL;
    MemoryContext   batch_context;
    MemoryContext   old_context;

    (void) graph_name;

    batch_context = AllocSetContextCreate(CurrentMemoryContext,
                                          "AGE Spatial CSV Edge Load Batch Context",
                                          ALLOCSET_DEFAULT_SIZES);

    label_relid = get_label_relation(label_name, graph_oid);
    label_rel = table_open(label_relid, RowExclusiveLock);
    label_seq_name = get_label_seq_relation_name(label_name);
    label_seq_relid = get_relname_relid(label_seq_name, graph_oid);

    init_batch_insert(&batch_state, label_name, graph_oid);
    copy_options = create_copy_options();
    pstate = make_parsestate(NULL);

    PG_TRY();
    {
        cstate = BeginCopyFrom(pstate, label_rel, NULL, file_path, false,
                               NULL, NIL, copy_options);

        while (NextCopyFromRawFields(cstate, &fields, &nfields))
        {
            if (is_first_row)
            {
                int i;

                header_count = nfields;
                header = (char **) palloc(sizeof(char *) * nfields);
                for (i = 0; i < nfields; i++)
                {
                    header[i] = trim_whitespace(fields[i]);
                }
                skip = bind_spatial_columns(header, header_count, label_name,
                                            graph_oid, columns, num_columns,
                                            false, true);
                is_first_row = false;
            }
            else
            {
                row_number++;
                old_context = MemoryContextSwitchTo(batch_context);
                process_edge_spatial_row(fields, nfields, header,
                                         header_count, skip, label_id,
                                         label_seq_relid, graph_oid,
                                         load_as_agtype, batch_state,
                                         columns, num_columns, row_number);
                MemoryContextSwitchTo(old_context);

                if (batch_state->num_tuples == 0)
                {
                    MemoryContextReset(batch_context);
                }
            }
        }

        finish_batch_insert(&batch_state);
        MemoryContextReset(batch_context);
        EndCopyFrom(cstate);
    }
    PG_FINALLY();
    {
        if (header != NULL)
        {
            int i;
            for (i = 0; i < header_count; i++)
            {
                pfree(header[i]);
            }
            pfree(header);
        }

        table_close(label_rel, RowExclusiveLock);
        MemoryContextDelete(batch_context);
        free_parsestate(pstate);
    }
    PG_END_TRY();

    return EXIT_SUCCESS;
}
