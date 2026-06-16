# AGE Spatial 개발 README

이 문서는 Apache AGE fork에 PostGIS 기반 spatial column API를 추가한 현재 구현 상태를 정리한다. 현재 목표는 AGE graph label table에 `geometry`/`geography` physical column을 안전하게 추가하고, 해당 column을 PostGIS 함수와 GiST index로 사용할 수 있게 만드는 것이다.

현재 extension 이름은 `age_spatial`이고, C shared library는 기존 AGE와 동일하게 `age`를 사용한다.

```sql
CREATE EXTENSION postgis;
CREATE EXTENSION age_spatial;

LOAD 'age';
SET search_path = ag_catalog, public;
```

## 구현 방향

AGE vertex/edge의 일반 property는 기존처럼 `properties agtype` column에 저장된다. 공간 데이터는 `properties` 안에 넣지 않고, AGE label table의 physical column으로 분리한다.

예를 들어 `test_kg` graph의 `SGG` vertex label에 `geom` column을 추가하면 실제 PostgreSQL table은 다음과 비슷한 구조가 된다.

```text
test_kg."SGG"
  id          graphid
  properties  agtype
  geom        geometry(...)
```

edge label의 경우도 마찬가지다.

```text
test_kg."ROAD"
  id          graphid
  start_id    graphid
  end_id      graphid
  properties  agtype
  geom        geometry(...)
```

AGE가 소유하는 기본 column은 그대로 보호한다.

```text
vertex core columns: id, properties
edge core columns:   id, start_id, end_id, properties
```

공식적으로 지원하는 extra column은 `age_spatial` API로 생성한 `geometry`/`geography` column이다.

## 추가된 Metadata Table

공간 column 등록 정보는 `ag_catalog.ag_spatial_columns`에 저장된다.

```sql
SELECT *
FROM ag_catalog.ag_spatial_columns
ORDER BY graph_name, label_name, column_name;
```

주요 column은 다음과 같다.

```text
graph_name     graph 이름
label_name     vertex/edge label 이름
label_kind     vertex는 'v', edge는 'e'
relation_oid   실제 label table oid
column_name    spatial column 이름
spatial_type   geometry 또는 geography
geometry_type  Point, MultiPolygon, LineString 등 PostGIS subtype
srid           SRID
index_name     자동 생성된 GiST index 이름
created_at     등록 시각
```

이 metadata는 다음 API들이 column 존재 여부, 타입, SRID, index 정리를 판단하는 기준으로 사용한다.

## Public API

### `add_geometry_column`

AGE vertex/edge label table에 PostGIS `geometry` column을 추가한다.

```sql
SELECT ag_catalog.add_geometry_column(
    p_graph_name,
    p_label_name,
    p_column_name,
    p_geometry_type,
    p_srid,
    p_create_index
);
```

Signature:

```sql
ag_catalog.add_geometry_column(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_geometry_type text DEFAULT 'Geometry',
    p_srid integer DEFAULT 4326,
    p_create_index boolean DEFAULT true
) RETURNS void
```

예시:

```sql
SELECT ag_catalog.add_geometry_column(
    'spatial_api_test',
    'Place',
    'geom',
    'Point',
    4326,
    true
);
```

수행 내용:

```text
1. graph/label 존재 여부 확인
2. AGE reserved column 이름인지 확인
3. 같은 이름의 physical column 존재 여부 확인
4. geometry column 추가
5. create_index=true이면 GiST index 생성
6. ag_catalog.ag_spatial_columns에 metadata 등록
```

실제 생성되는 SQL 형태는 내부적으로 다음과 같다.

```sql
ALTER TABLE spatial_api_test."Place"
ADD COLUMN geom geometry(Point, 4326);

CREATE INDEX place_geom_gix
ON spatial_api_test."Place"
USING gist (geom);
```

### `add_geography_column`

AGE vertex/edge label table에 PostGIS `geography` column을 추가한다.

Signature:

```sql
ag_catalog.add_geography_column(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_geography_type text DEFAULT 'Geometry',
    p_srid integer DEFAULT 4326,
    p_create_index boolean DEFAULT true
) RETURNS void
```

예시:

```sql
SELECT ag_catalog.add_geography_column(
    'spatial_api_test',
    'Place',
    'geog',
    'Point',
    4326,
    true
);
```

### `set_geometry_value`

등록된 `geometry` column에 값을 입력하거나 갱신한다.

Signature:

```sql
ag_catalog.set_geometry_value(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_entity_id ag_catalog.graphid,
    p_value geometry
) RETURNS void
```

예시:

```sql
SELECT ag_catalog.set_geometry_value(
    'spatial_api_test',
    'Place',
    'geom',
    (vertex_id::text)::ag_catalog.graphid,
    public.ST_SetSRID(public.ST_MakePoint(127.0, 37.0), 4326)
);
```

수행 내용:

```text
1. ag_spatial_columns에서 graph/label/column 조회
2. spatial_type='geometry'인지 확인
3. 입력 geometry의 SRID가 등록 SRID와 같은지 확인
4. 실제 label table을 UPDATE
5. 대상 graphid row가 정확히 1개인지 확인
```

SRID가 맞지 않으면 error를 발생시킨다.

### `set_geography_value`

등록된 `geography` column에 값을 입력하거나 갱신한다.

Signature:

```sql
ag_catalog.set_geography_value(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_entity_id ag_catalog.graphid,
    p_value geography
) RETURNS void
```

예시:

```sql
SELECT ag_catalog.set_geography_value(
    'spatial_api_test',
    'Place',
    'geog',
    (vertex_id::text)::ag_catalog.graphid,
    public.ST_SetSRID(public.ST_MakePoint(127.0, 37.0), 4326)::public.geography
);
```

### `set_spatial_value`

`set_spatial_value`는 `set_geometry_value`와 `set_geography_value`를 하나의 이름으로 사용하기 위한 overloaded wrapper이다.

Geometry overload:

```sql
ag_catalog.set_spatial_value(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_entity_id ag_catalog.graphid,
    p_value geometry
) RETURNS void
```

Geography overload:

```sql
ag_catalog.set_spatial_value(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_entity_id ag_catalog.graphid,
    p_value geography
) RETURNS void
```

PostgreSQL이 마지막 인자의 타입을 보고 어떤 overload를 호출할지 결정한다.

Geometry 예시:

```sql
SELECT ag_catalog.set_spatial_value(
    'spatial_api_test',
    'Place',
    'geom',
    (vertex_id::text)::ag_catalog.graphid,
    public.ST_SetSRID(public.ST_MakePoint(127.0, 37.0), 4326)
);
```

Geography 예시:

```sql
SELECT ag_catalog.set_spatial_value(
    'spatial_api_test',
    'Place',
    'geog',
    (vertex_id::text)::ag_catalog.graphid,
    public.ST_SetSRID(public.ST_MakePoint(127.0, 37.0), 4326)::public.geography
);
```

주의할 점:

```text
geometry 값은 geometry overload로 들어간다.
geography 값은 명시적으로 ::geography casting을 해주는 것이 안전하다.
```

### `clear_spatial_value`

등록된 spatial column의 특정 row 값을 `NULL`로 만든다.

Signature:

```sql
ag_catalog.clear_spatial_value(
    p_graph_name name,
    p_label_name name,
    p_column_name name,
    p_entity_id ag_catalog.graphid
) RETURNS void
```

예시:

```sql
SELECT ag_catalog.clear_spatial_value(
    'spatial_api_test',
    'Place',
    'geom',
    (vertex_id::text)::ag_catalog.graphid
);
```

이 함수는 column 자체를 삭제하지 않고 값만 지운다.

### `drop_spatial_column`

등록된 spatial column을 제거한다.

Signature:

```sql
ag_catalog.drop_spatial_column(
    p_graph_name name,
    p_label_name name,
    p_column_name name
) RETURNS void
```

예시:

```sql
SELECT ag_catalog.drop_spatial_column(
    'spatial_api_test',
    'Place',
    'geom'
);
```

수행 내용:

```text
1. ag_spatial_columns에서 등록 정보 조회
2. 등록된 GiST index가 있으면 DROP INDEX IF EXISTS
3. ALTER TABLE ... DROP COLUMN IF EXISTS
4. ag_spatial_columns metadata 삭제
```

일반 SQL로 먼저 column을 지운 경우에도 metadata가 남아 있다면 cleanup 용도로 사용할 수 있다.

```sql
ALTER TABLE spatial_api_test."Place" DROP COLUMN geom;
SELECT ag_catalog.drop_spatial_column('spatial_api_test', 'Place', 'geom');
```

두 번째 호출 시 metadata도 없으면 error가 발생한다.

## Internal Helper Functions

다음 함수들은 public API 내부에서 사용하는 helper이다. 일반 사용자가 직접 호출하는 용도는 아니다.

```text
_validate_spatial_column_args
  column 이름, spatial type, subtype, SRID 유효성 검사

_get_label_relation
  graph/label 이름으로 실제 label table oid와 label kind 조회

_add_spatial_column
  geometry/geography 공통 ADD COLUMN, GiST index 생성, metadata 등록 처리
```

## 사용 예시: Vertex Geometry

```sql
CREATE EXTENSION postgis;
CREATE EXTENSION age_spatial;
LOAD 'age';
SET search_path = ag_catalog, public;

SELECT ag_catalog.create_graph('spatial_api_test');
SELECT ag_catalog.create_vlabel('spatial_api_test', 'Place');

SELECT ag_catalog.add_geometry_column(
    'spatial_api_test',
    'Place',
    'geom',
    'Point',
    4326,
    true
);

WITH created AS (
    SELECT *
    FROM ag_catalog.cypher('spatial_api_test'::name, $$
        CREATE (p:Place {name: 'A'})
        RETURN id(p)
    $$) AS (id agtype)
)
SELECT ag_catalog.set_spatial_value(
    'spatial_api_test',
    'Place',
    'geom',
    (id::text)::ag_catalog.graphid,
    public.ST_SetSRID(public.ST_MakePoint(127.0, 37.0), 4326)
)
FROM created;

SELECT
    id,
    properties,
    public.ST_AsText(geom) AS geom_wkt,
    public.ST_SRID(geom) AS srid
FROM spatial_api_test."Place";
```

## 사용 예시: Edge Geometry

```sql
SELECT ag_catalog.create_elabel('spatial_api_test', 'ROAD');

SELECT ag_catalog.add_geometry_column(
    'spatial_api_test',
    'ROAD',
    'geom',
    'LineString',
    4326,
    true
);

WITH created AS (
    SELECT *
    FROM ag_catalog.cypher('spatial_api_test'::name, $$
        MATCH (a:Place {name: 'A'})
        CREATE (a)-[r:ROAD {name: 'A road'}]->(:Place {name: 'B'})
        RETURN id(r)
    $$) AS (id agtype)
)
SELECT ag_catalog.set_spatial_value(
    'spatial_api_test',
    'ROAD',
    'geom',
    (id::text)::ag_catalog.graphid,
    public.ST_SetSRID(
        public.ST_MakeLine(
            public.ST_MakePoint(127.0, 37.0),
            public.ST_MakePoint(127.1, 37.1)
        ),
        4326
    )
)
FROM created;
```

## Metadata와 Index 확인

등록된 spatial column 확인:

```sql
SELECT graph_name, label_name, label_kind, column_name,
       spatial_type, geometry_type, srid, index_name
FROM ag_catalog.ag_spatial_columns
WHERE graph_name = 'spatial_api_test'
ORDER BY label_name, column_name;
```

실제 table column 확인:

```sql
SELECT column_name, udt_name
FROM information_schema.columns
WHERE table_schema = 'spatial_api_test'
  AND table_name = 'Place'
ORDER BY ordinal_position;
```

GiST index 확인:

```sql
SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'spatial_api_test'
ORDER BY tablename, indexname;
```

## 일반 SQL DROP COLUMN 동작

AGE label table 자체는 보호되지만, 현재 구현에서는 AGE core storage column이 아닌 user-added physical column은 일반 SQL로도 삭제할 수 있다.

```sql
ALTER TABLE spatial_api_test."Place" DROP COLUMN geom;
```

하지만 일반 SQL drop은 `ag_catalog.ag_spatial_columns` metadata를 자동 삭제하지 않는다. 따라서 API 기준으로 정리하려면 다음을 호출한다.

```sql
SELECT ag_catalog.drop_spatial_column('spatial_api_test', 'Place', 'geom');
```

권장 방식은 항상 `drop_spatial_column`을 사용하는 것이다.

## AGE CRUD와 Spatial Column 보존

AGE Cypher executor는 원래 label table의 fixed storage column만 다루도록 설계되어 있었다. physical extra column이 있으면 `CREATE` 또는 `SET` 과정에서 tuple slot 값이 비거나 column 값이 날아갈 수 있다.

이를 위해 C 코드에 다음 보존 로직을 추가했다.

```text
src/backend/executor/cypher_utils.c
  fill_entity_slot_extra_attrs(...)

src/backend/executor/cypher_set.c
  Cypher SET 시 old tuple의 extra column 값을 보존
```

동작:

```text
Cypher CREATE:
  AGE core column이 아닌 extra column은 NULL로 채운다.

Cypher SET:
  AGE core column이 아닌 extra column은 기존 tuple 값을 유지한다.
```

따라서 다음 작업 후에도 `geom`/`geog` 값은 유지되어야 한다.

```sql
SELECT *
FROM ag_catalog.cypher('spatial_api_test'::name, $$
    MATCH (p:Place)
    SET p.checked = true
    RETURN p.name
$$) AS (name agtype);
```

## Regression Test

AGE Spatial API 전용 regression test는 다음 파일에 있다.

```text
regress/sql/age_spatial.sql
regress/expected/age_spatial.out
```

테스트 graph 이름은 `spatial_api_test`이다.

현재 테스트하는 항목:

```text
1. graph 생성
2. vertex label 생성
3. edge label 생성
4. vertex label에 geometry/geography column 추가
5. edge label에 geometry/geography column 추가
6. GiST index metadata 확인
7. Cypher CREATE로 vertex/edge 생성
8. set_spatial_value geometry overload 확인
9. set_spatial_value geography overload 확인
10. set_geometry_value / set_geography_value 기존 함수 확인
11. Cypher SET 이후 spatial column 보존 확인
12. clear_spatial_value 확인
13. 일반 SQL DROP COLUMN 허용 확인
14. drop_spatial_column metadata cleanup 확인
15. 모든 spatial column 제거 후 metadata count = 0 확인
```

실행:

```bash
cd /Users/gu/Documents/Codex/2026-06-15/postgresql-extension-apacheage-postgis-2-extension/work/age

make PG_CONFIG=/Users/gu/Documents/Codex/2026-06-15/postgresql-extension-apacheage-postgis-2-extension/work/dev/pg16-debug/bin/pg_config installcheck REGRESS=age_spatial
```

성공 예시:

```text
ok 1 - age_spatial
1..1
# All 1 tests passed.
```

## Build와 Install

수정 후 개발용 PostgreSQL prefix에 설치하려면 다음을 실행한다.

```bash
cd /Users/gu/Documents/Codex/2026-06-15/postgresql-extension-apacheage-postgis-2-extension/work/age

make PG_CONFIG=/Users/gu/Documents/Codex/2026-06-15/postgresql-extension-apacheage-postgis-2-extension/work/dev/pg16-debug/bin/pg_config

make PG_CONFIG=/Users/gu/Documents/Codex/2026-06-15/postgresql-extension-apacheage-postgis-2-extension/work/dev/pg16-debug/bin/pg_config install
```

기존 DB에 이미 `age_spatial` extension이 생성되어 있으면 새 SQL 함수가 자동으로 반영되지 않을 수 있다. 개발 중에는 fresh DB를 만들거나 extension을 재생성하는 방식이 가장 단순하다.

## 현재 한계

현재 구현은 다음을 지원한다.

```text
AGE label table에 PostGIS geometry/geography physical column 추가
GiST index 생성
graphid 기반 spatial value 입력/수정
Cypher CREATE/SET/DELETE와 spatial column 공존
spatial column drop과 metadata cleanup
```

아직 지원하지 않는 것:

```text
Cypher 내부에서 g.geom을 physical column으로 직접 해석
Cypher 내부에서 ST_Touches(g1.geom, g2.geom)를 바로 실행
Cypher spatial predicate를 PostGIS GiST index가 타도록 자동 최적화
properties.geom과 physical geom 이름 충돌 정책 정교화
기존 설치 DB를 위한 upgrade script 정리
```

현재 spatial query는 다음처럼 SQL join을 통해 사용할 수 있다.

```sql
WITH graph_result AS (
    SELECT *
    FROM ag_catalog.cypher('test_kg'::name, $$
        MATCH (g1:SGG), (g2:SGG)
        WHERE id(g1) <> id(g2)
        RETURN id(g1), id(g2)
    $$) AS (g1_id agtype, g2_id agtype)
)
SELECT *
FROM graph_result gr
JOIN test_kg."SGG" g1 ON g1.id = (gr.g1_id::text)::ag_catalog.graphid
JOIN test_kg."SGG" g2 ON g2.id = (gr.g2_id::text)::ag_catalog.graphid
WHERE public.ST_Touches(g1.geom, g2.geom);
```

최종 목표는 다음과 같은 Cypher 내부 spatial query를 지원하는 것이다.

```sql
SELECT *
FROM ag_catalog.cypher('test_kg'::name, $$
    MATCH (g1:SGG), (g2:SGG)
    WHERE id(g1) <> id(g2)
      AND ST_Touches(g1.geom, g2.geom)
    RETURN id(g1), id(g2)
$$) AS (g1_id agtype, g2_id agtype);
```

## 주요 구현 파일

```text
sql/age_spatial.sql
  spatial metadata table과 public API 함수 정의

sql/sql_files
  age_spatial.sql을 extension install SQL에 포함

age_spatial.control
  extension 이름, schema, PostGIS dependency 정의

Makefile
  extension 이름 age_spatial 설정
  age_spatial regression test 등록

src/backend/executor/cypher_utils.c
  extra physical column slot 값 처리

src/backend/executor/cypher_set.c
  Cypher SET 시 extra physical column 보존

src/backend/catalog/ag_catalog.c
  AGE label table 보호 hook에서 non-core DROP COLUMN 허용

src/backend/parser/cypher_expr.c
  age_spatial extension 소유 함수를 AGE 내부 함수로 인식

regress/sql/age_spatial.sql
  AGE Spatial API regression test

regress/expected/age_spatial.out
  regression expected output
```

## 다음 개발 단계

추천 순서:

```text
1. Cypher에서 PostGIS external function 호출 경로 확인
2. Cypher에서 g.geom physical column 해석 구현
3. ST_Touches(g1.geom, g2.geom) MVP 성공
4. GiST index 사용 가능하도록 spatial predicate 최적화
5. 실험 SQL, EXPLAIN ANALYZE, README 정리
6. upgrade script와 배포 packaging 정리
```

