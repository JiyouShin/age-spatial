# RDF Benchmark Environment

This compose stack runs RDF-native stores for the YAGO2Geo / GeoQuestions
benchmark comparison:

- Apache Jena Fuseki GeoSPARQL at `http://127.0.0.1:13030/yago2geo`
- OpenLink Virtuoso at `http://127.0.0.1:18890/sparql`

The RDF files are mounted read-only from:

`benchmark/data/extracted/yago2geo`

## Start Services

```bash
docker compose -f docker/rdf-benchmark/docker-compose.yml up -d
```

Jena is built from `docker/rdf-benchmark/jena-geosparql/Dockerfile`, using
the Apache Jena `jena-fuseki-geosparql` executable jar. The service entrypoint
uses the local `NoSpatialIndexFuseki` launcher to register GeoSPARQL functions
such as `geof:sfWithin` while skipping the wrapper's full startup spatial-index
build.

The YAGO2Geo geometry literals are large, so the Jena loader defaults to
`JVM_ARGS=-Xmx96g`. Reduce this only if loading a smaller subset. The custom
Dockerfile passes `JVM_ARGS` directly to the Java entrypoint.

The stock `jena-fuseki-geosparql` wrapper calls
`GeoSPARQLConfig.setupSpatialIndex(dataset, tdb/spatial.index)` whenever the
dataset is non-empty, even with `--index false`. On full YAGO2Geo this startup
index build is very expensive and can fail on legacy geometry literal datatypes.
The local no-index launcher calls `GeoSPARQLConfig.setupNoIndex(...)` and then
starts Fuseki directly over the loaded TDB2 dataset. GeoSPARQL functions remain
available, but large spatial queries do not benefit from Jena's startup spatial
index.

## Load YAGO2Geo Into Jena

Jena TDB2 bulk loading must run while the Fuseki server is stopped:

```bash
benchmark/scripts/load-yago2geo-jena.sh
```

Some original YAGO2Geo materialized triples contain invalid quote escapes
inside IRIs, such as `\"` in `<...>`. Jena's RIOT parser treats these as fatal.
Prepare a Jena-safe input directory first if needed:

```bash
benchmark/scripts/prepare-jena-yago2geo-input.sh
JENA_RDF_ROOT=/rdf/yago2geo-jena-input benchmark/scripts/load-yago2geo-jena.sh
```

The preparation script writes hardlinks/copies under
`benchmark/results/rdfdb/jena_input` and percent-encodes those quote escapes as
`%22`; it does not modify the original RDF files. It also normalizes Jena-facing
geometry literals:

- USA `geo:asWKT` and `yago2geo:the_geom` plain WKT literals are typed as
  `geo:wktLiteral`.
- Greece `strdf:WKT` literals are converted to GeoSPARQL WKT lexical form and
  typed as `geo:wktLiteral`.

The loader writes into the `jena_data` bind mount, builds the no-index launcher
under `benchmark/results/rdfdb/jena_noindex`, and then starts Fuseki.
For a dry run:

```bash
docker compose -f docker/rdf-benchmark/docker-compose.yml run --rm -e DRY_RUN=1 jena-loader
```

## Load YAGO2Geo Into Virtuoso

Virtuoso loads through `ld_dir(...)` plus `rdf_loader_run()`:

```bash
benchmark/scripts/load-yago2geo-virtuoso.sh
```

Triples are loaded into the named graph:

`http://yago-knowledge.org/graph/yago2geo`

For a dry run:

```bash
docker compose -f docker/rdf-benchmark/docker-compose.yml run --rm -e DRY_RUN=1 --entrypoint /scripts/load-virtuoso-yago2geo.sh virtuoso
```

The Virtuoso service uses the OpenLink `virtuoso-opensource-7:latest` image,
which includes GeoSPARQL topology support through the `geos` plugin and metric
distance support through the `proj4` plugin.

The loader reads RDF files from `/database/rdf/yago2geo`, so that path must be
included in Virtuoso's `DirsAllowed` setting. The compose file sets:

```yaml
VIRT_Parameters_DirsAllowed: "., ../vad, /usr/share/proj, /database/rdf/yago2geo"
```

If an existing Docker volume was first created with an older Virtuoso image,
check `/database/virtuoso.ini` inside the container and make sure the
`[Plugins]` section contains:

```ini
Load4    = plain, proj4
```

Fresh volumes created from the current image include the required plugin
support. The current local volume was upgraded in-place and backed up as
`/database/virtuoso.ini.before-proj4` before adding the `proj4` load entry.

## GeoSPARQL Smoke Test

```bash
benchmark/scripts/rdf-geosparql-smoke.sh
```

Current support observed in this environment:

| Store | GeoSPARQL topology functions | `geof:distance(..., uom:metre)` | `strdf:*` stSPARQL functions |
|---|---|---|---|
| Jena Fuseki GeoSPARQL | works | returns unbound in the current CRS/unit smoke test | not registered |
| Virtuoso OpenLink latest | works | works with the bundled `proj4` plugin/data | not registered |

GeoQuestions1089 contains both GeoSPARQL functions such as `geof:sfWithin`
and stSPARQL functions such as `strdf:area`, `strdf:below`, `strdf:above`,
`strdf:left`, and `strdf:right`. The topology-function queries can run through
these RDF stores after loading, but distance/area/directional stSPARQL queries
need engine-specific rewrites or additional extension work.

## Ports

| Store | Endpoint | User |
|---|---|---|
| Jena Fuseki GeoSPARQL | `http://127.0.0.1:13030/yago2geo/sparql` | no auth |
| Virtuoso | `http://127.0.0.1:18890/sparql` | `dba` / `dba` |

## Stop

```bash
docker compose -f docker/rdf-benchmark/docker-compose.yml down
```

To remove RDF database volumes:

```bash
docker compose -f docker/rdf-benchmark/docker-compose.yml down -v
```
