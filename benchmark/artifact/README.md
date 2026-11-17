# AGE-Spatial Paper Artifact

This branch preserves the data, queries, reference answers, scripts, runtime
configuration, and measured results used for the AGE-Spatial paper. The final
evaluation workload contains 222 UK-only GeoQuestions1089 questions that
require explicit spatial computation.

## Artifact contents

| Path | Contents |
|---|---|
| `benchmark/artifact/data/yago2geo-uk-rdf.tar.gz` | YAGO2Geo UK RDF subset loaded by Jena, Virtuoso, and Strabon |
| `benchmark/artifact/data/yago2geo-uk-lpg-csv.tar.gz` | Transformed LPG CSV and AGE loading files |
| `benchmark/queries/GeoQuestions1089.json` | Original GeoQuestions1089 question and query definitions |
| `benchmark/queries/GeoQuestions1089Answers.json` | Original benchmark reference answers |
| `benchmark/results/geoquestions/age_spatial_full/geoquestions_age_spatial_full_uk_only_questions.csv` | Final 222 questions, source GeoSPARQL, translated AGE Cypher, spatial-function classes, and reference answers |
| `benchmark/results/geoquestions/virtuoso_bif_uk/` | Virtuoso GeoSPARQL/BIF query variants |
| `benchmark/results/geoquestions/uk_indexed_benchmark/` | Measured results for AGE-Spatial, Jena, Virtuoso, and Strabon |
| `benchmark/results/system_metrics/` | Loading, query, CPU, memory, storage, and buffer measurements |
| `benchmark/results/relationship_spatial_depth/` | Relationship-depth and spatial-predicate experiment |
| `benchmark/scripts/` | Transformation, loading, execution, comparison, and summarization scripts |
| `docker/benchmark/` | AGE-Spatial benchmark container |
| `docker/rdf-benchmark/` | Jena, Virtuoso, and Strabon containers |

The selected-question CSV is the compact manifest for the main evaluation. Its
`id`, `question`, `sparql`, `age_cypher`, and `answer` columns identify the
question set and the reference answer used for each comparison.

## Software environment

- AGE-Spatial extension version 1.7.0, based on Apache AGE 1.7.0
- PostgreSQL 16.14 and PostGIS 3.6.4 for AGE-Spatial
- Apache Jena 5.1.0 with TDB2 and Fuseki
- Virtuoso Open Source 7.2.17
- Strabon 3.3.3-SNAPSHOT with PostgreSQL 16 and PostGIS 3.4

The Docker definitions pin the software configurations used by the benchmark.

## Clone the artifact

The two dataset archives and the reference-answer file use Git LFS.

```bash
git lfs install
git clone --branch paper-artifact-r1 \
  https://github.com/JiyouShin/age-spatial.git
cd age-spatial
git lfs pull
```

Verify the downloaded inputs and reported result files:

```bash
sha256sum -c benchmark/artifact/SHA256SUMS
```

## Restore the UK datasets

```bash
mkdir -p benchmark/results/rdfdb benchmark/results/load

tar -xzf benchmark/artifact/data/yago2geo-uk-rdf.tar.gz \
  -C benchmark/results/rdfdb

tar -xzf benchmark/artifact/data/yago2geo-uk-lpg-csv.tar.gz \
  -C benchmark/results/load
```

The extracted paths are:

```text
benchmark/results/rdfdb/uk_subset_input/
benchmark/results/load/yago2geo_uk_subset/
```

## Start and load AGE-Spatial

```bash
docker compose -f docker/benchmark/docker-compose.yml up -d --build
benchmark/scripts/load-yago2geo-uk-subset-age.sh
benchmark/scripts/ensure-yago2geo-uk-subset-age-uri-indexes.sh
```

## Start and load the RDF systems

```bash
docker compose -f docker/rdf-benchmark/docker-compose.yml build

benchmark/scripts/load-yago2geo-uk-subset-jena.sh
benchmark/scripts/load-yago2geo-uk-subset-virtuoso.sh
benchmark/scripts/load-yago2geo-uk-subset-strabon.sh
```

Virtuoso geometry materialization and the Strabon/Jena index checks are
implemented by the corresponding scripts under `benchmark/scripts/`.

## Run the 222-question comparison

The isolated benchmark runner executes one engine at a time with a 60-second
query timeout and writes engine-specific CSV files under
`benchmark/results/geoquestions/uk_indexed_benchmark/`.

```bash
benchmark/scripts/run-geoquestions-uk-indexed-isolated-benchmark.sh
```

The reusable Python runner is:

```text
benchmark/scripts/run-geoquestions-full-benchmark.py
```

Each measured result row records the question ID, executed query, reference
answer, returned value, execution time, execution status, answer-match status,
normalization method, and error message.

## Additional experiments

System-level measurements can be reproduced with:

```bash
python3 benchmark/scripts/run-age-spatial-system-metrics.py
python3 benchmark/scripts/run-rdf-system-query-metrics.py
```

The relationship-depth experiment can be reproduced with:

```bash
python3 benchmark/scripts/run-relationship-spatial-depth-benchmark.py
```

## Data provenance

The UK inputs are a derived subset of YAGO2Geo. The archives are provided to
fix the exact experimental input and do not replace the attribution or license
terms of YAGO2Geo and its upstream geospatial sources. See the paper for the
YAGO2Geo and GeoQuestions1089 citations.
