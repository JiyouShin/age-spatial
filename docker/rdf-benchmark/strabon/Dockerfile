# syntax=docker/dockerfile:1.6

FROM maven:3.6.3-jdk-8 AS build

ARG STRABON_REPO=https://github.com/AI-team-UoA/Strabon.git
ARG STRABON_REF=main

COPY settings.xml /tmp/maven-settings.xml
COPY patches/ /tmp/patches/

RUN git clone --depth 1 --branch "${STRABON_REF}" "${STRABON_REPO}" /build/Strabon

WORKDIR /build/Strabon

RUN git apply /tmp/patches/*.patch

RUN --mount=type=cache,target=/root/.m2 \
    sed -i \
        -e 's/^hostname=.*/hostname=strabon-postgis/' \
        -e 's/^port=.*/port=5432/' \
        -e 's/^dbengine=.*/dbengine=postgis/' \
        -e 's/^password=.*/password=postgres/' \
        -e 's/^dbname=.*/dbname=endpoint/' \
        -e 's/^username=.*/username=postgres/' \
        endpoint/WebContent/WEB-INF/connection.properties \
 && mvn -s /tmp/maven-settings.xml -DskipTests clean package \
 && cp endpoint/target/strabon-endpoint-*.war /tmp/Strabon.war

FROM tomcat:9.0.71-jdk8-temurin

ENV CATALINA_OPTS="-Xms2g -Xmx24g"

RUN rm -rf /usr/local/tomcat/webapps/*

COPY --from=build /tmp/Strabon.war /usr/local/tomcat/webapps/Strabon.war
COPY --from=build /build/Strabon/runtime /opt/strabon/runtime
COPY --from=build /build/Strabon/scripts/strabon-cmd /opt/strabon/scripts/strabon-cmd

RUN chmod +x /opt/strabon/scripts/strabon-cmd

EXPOSE 8080
