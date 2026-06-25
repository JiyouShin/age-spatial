FROM postgres:16.4-bookworm

ARG POSTGIS_VERSION=3.6.4
ARG PG_MAJOR=16

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=en_US.UTF-8
ENV LC_COLLATE=en_US.UTF-8
ENV LC_CTYPE=en_US.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        autoconf \
        automake \
        bison \
        build-essential \
        ca-certificates \
        curl \
        flex \
        gettext \
        libgdal-dev \
        libgeos-dev \
        libjson-c-dev \
        libproj-dev \
        libprotobuf-c-dev \
        libtool \
        libxml2-dev \
        libxml2-utils \
        locales \
        pkg-config \
        postgresql-server-dev-${PG_MAJOR} \
        protobuf-c-compiler \
        xsltproc \
    && echo "en_US.UTF-8 UTF-8" > /etc/locale.gen \
    && locale-gen \
    && update-locale LANG=en_US.UTF-8

RUN curl -fsSL "https://download.osgeo.org/postgis/source/postgis-${POSTGIS_VERSION}.tar.gz" \
        -o /tmp/postgis.tar.gz \
    && mkdir -p /tmp/postgis-src \
    && tar -xzf /tmp/postgis.tar.gz -C /tmp/postgis-src --strip-components=1 \
    && cd /tmp/postgis-src \
    && ./configure --without-raster --without-topology \
    && make -j"$(nproc)" \
    && make install \
    && rm -rf /tmp/postgis.tar.gz /tmp/postgis-src

COPY . /tmp/age-spatial-src

WORKDIR /tmp/age-spatial-src

RUN make clean || true \
    && make PG_CONFIG="/usr/lib/postgresql/${PG_MAJOR}/bin/pg_config" -j"$(nproc)" \
    && make PG_CONFIG="/usr/lib/postgresql/${PG_MAJOR}/bin/pg_config" install \
    && mkdir -p /opt/age-spatial \
    && cp README_AGE_SPATIAL.md /opt/age-spatial/ \
    && rm -rf /tmp/age-spatial-src

COPY docker/benchmark/initdb/00-init-age-spatial.sql /docker-entrypoint-initdb.d/00-init-age-spatial.sql

CMD ["postgres", "-c", "shared_preload_libraries=age"]
