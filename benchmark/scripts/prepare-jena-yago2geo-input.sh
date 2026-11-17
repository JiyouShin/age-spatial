#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_ROOT="${SRC_ROOT:-${ROOT_DIR}/benchmark/data/extracted/yago2geo}"
OUT_ROOT="${OUT_ROOT:-${ROOT_DIR}/benchmark/results/rdfdb/jena_input}"

if [[ ! -d "${SRC_ROOT}" ]]; then
    echo "Source RDF root does not exist: ${SRC_ROOT}" >&2
    exit 1
fi

echo "Preparing Jena-safe RDF input"
echo "Source: ${SRC_ROOT}"
echo "Output: ${OUT_ROOT}"

rm -rf "${OUT_ROOT}"
mkdir -p "${OUT_ROOT}"

while IFS= read -r -d '' src; do
    rel="${src#"${SRC_ROOT}/"}"
    dst="${OUT_ROOT}/${rel}"
    mkdir -p "$(dirname "${dst}")"

    case "${rel}" in
        mat/mat_ascii.nt|mat/yago2geo_mat_area_ascii.nt)
            echo "Sanitizing ${rel}: replacing \\\" in IRIs with %22"
            perl -pe 's/\\"/%22/g' "${src}" > "${dst}"
            ;;
        greece/GAG_*.ttl)
            echo "Sanitizing ${rel}: converting stRDF WKT literals to geo:wktLiteral"
            perl -pe 's@"([^"]*);(http://www\.opengis\.net/def/crs/[^"]+)"\^\^<http://strdf\.di\.uoa\.gr/ontology#WKT>@"<$2> $1"^^<http://www.opengis.net/ont/geosparql#wktLiteral>@g' "${src}" > "${dst}"
            ;;
        usa/*_geom.nt)
            echo "Sanitizing ${rel}: typing plain WKT literals as geo:wktLiteral"
            perl -pe 's@(<http://kr\.di\.uoa\.gr/yago2geo/ontology/the_geom>\s+"(?:POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|GEOMETRYCOLLECTION)[^"]*")\s*\.@$1^^<http://www.opengis.net/ont/geosparql#wktLiteral> .@; s@(<http://www\.opengis\.net/ont/geosparql#asWKT>\s+"(?:POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|GEOMETRYCOLLECTION)[^"]*")\s*\.@$1^^<http://www.opengis.net/ont/geosparql#wktLiteral> .@' "${src}" > "${dst}"
            ;;
        *)
            ln "${src}" "${dst}" 2>/dev/null || cp -p "${src}" "${dst}"
            ;;
    esac
done < <(
    find "${SRC_ROOT}" -type f \( -name '*.nt' -o -name '*.ttl' -o -name '*.rdf' \) -print0
)

echo "Prepared files:"
find "${OUT_ROOT}" -type f \( -name '*.nt' -o -name '*.ttl' -o -name '*.rdf' \) | wc -l
du -sh "${OUT_ROOT}"
