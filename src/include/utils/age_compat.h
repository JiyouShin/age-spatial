/*
 * PostgreSQL server API compatibility helpers used by AGE.
 */

#ifndef AGE_COMPAT_H
#define AGE_COMPAT_H

#include "postgres.h"

#include "access/genam.h"

#ifndef pg_noreturn
#define pg_noreturn _Noreturn
#endif

#if PG_VERSION_NUM >= 160000
#define age_index_beginscan(heap_relation, index_relation, snapshot, scan_keys, nkeys, norderbys) \
    index_beginscan((heap_relation), (index_relation), (snapshot), (nkeys), (norderbys))
#else
#define age_index_beginscan(heap_relation, index_relation, snapshot, scan_keys, nkeys, norderbys) \
    index_beginscan((heap_relation), (index_relation), (snapshot), (scan_keys), (nkeys), (norderbys))
#endif

#endif
