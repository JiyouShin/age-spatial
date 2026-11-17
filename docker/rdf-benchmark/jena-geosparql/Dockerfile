FROM eclipse-temurin:21-jre

ARG JENA_VERSION=5.1.0
ARG JENA_FUSEKI_GEOSPARQL_JAR=https://repo1.maven.org/maven2/org/apache/jena/jena-fuseki-geosparql/${JENA_VERSION}/jena-fuseki-geosparql-${JENA_VERSION}.jar

RUN set -eux; \
    useradd --system --uid 100 --gid 0 --home-dir /fuseki --create-home fuseki; \
    mkdir -p /opt/jena /fuseki/databases/yago2geo; \
    curl -fsSL "${JENA_FUSEKI_GEOSPARQL_JAR}" -o /opt/jena/jena-fuseki-geosparql.jar; \
    chown -R 100:0 /fuseki /opt/jena; \
    chmod -R g=u /fuseki /opt/jena

USER 100
WORKDIR /fuseki
EXPOSE 3030

ENTRYPOINT ["sh", "-c", "exec java ${JVM_ARGS:-} -jar /opt/jena/jena-fuseki-geosparql.jar \"$@\"", "jena-fuseki-geosparql"]
CMD ["--loc=/fuseki/databases/yago2geo", "/yago2geo"]
